# player_pane.py
#
# Copyright 2023 Nokse
# Copyright (C) 2026 Tyler Reece
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""JTPlayerPane — the cohesive player-pane controller.

Extracted from ``window.py`` (Phase 4 post-review fix #4). The window's
``window.blp`` widget tree is kept intact (lower-risk path: the mobile
bottom-bar and the sidebar player cross-bind to each other via ``bind`` in the
.blp, and every control uses ``$on_*`` template callbacks resolved against the
window template). Rather than split the template, this controller *wraps* the
existing template children passed in from the window and owns all player-pane
logic — song-change handling, slider/seek, codec chip, favorite icon+action,
lyrics loading (with the cache-poisoning fix), cancellable album-art fetch,
transport callbacks, volume, share, track-radio, and album/artist navigation
(pushing real album/artist pages via win actions).

Composition: the window constructs one ``JTPlayerPane``, handing it the player,
client, db, settings, a ``send_toast`` callable, and the relevant template
child widgets. The window's ``@Gtk.Template.Callback`` handlers and player
signal handlers are thin delegations to this controller. The window updates
``pane.client`` on login/logout transitions.

No behavior changes versus the pre-extraction window.
"""

import logging
from gettext import gettext as _

from gi.repository import Gdk, Gio, GLib, GObject, Gst, Gtk

from ..lib import RepeatType, utils
from ..lib import lyrics_cache
from ..lib import lyrics_providers
from ..lib.jellyfin.client import JellyfinError

logger = logging.getLogger(__name__)


def lyrics_to_text(lines):
    """Render ``[[text, start_ticks], ...]`` into the lyrics-widget text format.

    Timed lines become ``[MM:SS.xx]text`` so the widget's synced highlight path
    activates; plain lines are emitted as-is. (Pure — module-level so it stays
    trivially testable.)
    """
    if not lines:
        return ""
    out = []
    for entry in lines:
        text = entry[0] if entry else ""
        start = entry[1] if len(entry) > 1 else None
        if start is not None:
            total = start / 10_000_000  # ticks -> seconds
            minutes = int(total // 60)
            seconds = total - minutes * 60
            out.append("[%02d:%05.2f]%s" % (minutes, seconds, text))
        else:
            out.append(text)
    return "\n".join(out)


class JTPlayerPane(GObject.Object):
    """Controller for the sidebar player pane widgets.

    Holds the player-pane template children (no widget tree of its own — the
    .blp owns the tree) and all player-pane behavior. ``client`` is mutable:
    the window assigns it on login and clears it on logout.
    """

    def __init__(self, *, player, client, db, settings, send_toast, owner,
                 widgets):
        super().__init__()
        self.player_object = player
        self.client = client
        self.db = db
        self.settings = settings
        self._send_toast = send_toast
        self._owner = owner  # widget passed as run_async owner-guard

        # Player-pane template children (bound by name from the window).
        self.progress_bar = widgets["progress_bar"]
        self.duration_label = widgets["duration_label"]
        self.time_played_label = widgets["time_played_label"]
        self.shuffle_button = widgets["shuffle_button"]
        self.play_button = widgets["play_button"]
        self.small_progress_bar = widgets["small_progress_bar"]
        self.song_title_label = widgets["song_title_label"]
        self.playing_track_picture = widgets["playing_track_picture"]
        self.playing_track_artist_picture = widgets[
            "playing_track_artist_picture"
        ]
        self.playing_track_art_stack = widgets["playing_track_art_stack"]
        self.playing_track_image = widgets["playing_track_image"]
        self.artist_label = widgets["artist_label"]
        self.miniplayer_artist_label = widgets["miniplayer_artist_label"]
        self.volume_button = widgets["volume_button"]
        self.in_my_collection_button = widgets["in_my_collection_button"]
        self.lyrics_widget = widgets["lyrics_widget"]
        self.repeat_button = widgets["repeat_button"]
        self.buffer_spinner = widgets["buffer_spinner"]
        self.quality_label = widgets["quality_label"]

        self.duration = 0
        self.previous_fraction = 0.0
        self.image_canc = None
        # Separate cancellable for the artist-side art so an in-flight artist
        # fetch from a previous song can't land on the wrong track.
        self.artist_image_canc = None

        self._connect_player_signals()
        # Idle vinyl-record art until something plays.
        self.show_placeholder_art()
        self.artist_label.connect("activate-link", self._on_artist_link)
        self.miniplayer_artist_label.connect(
            "activate-link", self._on_artist_link
        )

        # Click the big album art to flip between album cover and the artist's
        # image (rotate transition). The art previously had no click behavior;
        # this gesture is local to the stack and toggles its visible child.
        self._art_flip_gesture = Gtk.GestureClick.new()
        self._art_flip_gesture.connect("released", self._on_art_clicked)
        self.playing_track_art_stack.add_controller(self._art_flip_gesture)

    # ------------------------------------------------------------------ #
    # Signal wiring                                                      #
    # ------------------------------------------------------------------ #

    def _connect_player_signals(self):
        po = self.player_object
        po.connect("notify::shuffle", self._on_shuffle_changed)
        po.connect("notify::playing", self._update_controls)
        po.connect("notify::repeat-type", self._update_repeat_button)
        po.connect("update-slider", self._update_slider)
        po.connect("song-changed", self._on_song_changed)
        po.connect("buffering", self._on_buffering)

    def init_repeat_and_volume(self):
        """Apply persisted repeat + volume to the controls (called by window)."""
        repeat = self.settings.get_int("repeat")
        self.player_object.repeat_type = repeat
        self._update_repeat_button(self.player_object, None)
        vol = self.settings.get_int("last-volume") / 10
        self.volume_button.get_adjustment().set_value(vol)

    # ------------------------------------------------------------------ #
    # Song change                                                        #
    # ------------------------------------------------------------------ #

    _PLACEHOLDER_RESOURCE = (
        "/io/github/tylerreece/timbre/record-placeholder.svg"
    )

    def show_placeholder_art(self):
        """Show the idle vinyl-record art (app start / empty queue).

        Both flip sides get the vinyl placeholder so the artist face shows the
        record when an artist has no image. The miniplayer ``playing_track_image``
        is a Gtk.Image whose ``file`` property other widgets bind to, and the
        mini bar already reads "No Song" when idle, so it is left untouched.
        """
        self.playing_track_picture.set_resource(self._PLACEHOLDER_RESOURCE)
        self.playing_track_artist_picture.set_resource(self._PLACEHOLDER_RESOURCE)
        self._reset_art_flip()

    def _reset_art_flip(self):
        """Snap the art back to the album side (no animation)."""
        prev = self.playing_track_art_stack.get_transition_type()
        self.playing_track_art_stack.set_transition_type(
            Gtk.StackTransitionType.NONE
        )
        self.playing_track_art_stack.set_visible_child_name("album")
        self.playing_track_art_stack.set_transition_type(prev)

    def _on_art_clicked(self, _gesture, _n_press, _x, _y):
        """Flip the player art between album cover and the artist image."""
        stack = self.playing_track_art_stack
        target = (
            "artist"
            if stack.get_visible_child_name() == "album"
            else "album"
        )
        stack.set_visible_child_name(target)

    def _on_song_changed(self, *args):
        track = self.player_object.playing_track
        if track is None:
            self.show_placeholder_art()
            return
        name = getattr(track, "name", "") or _("Unknown")
        self.song_title_label.set_label(name)
        self.song_title_label.set_tooltip_text(name)
        artist = getattr(track, "artist_name", "") or _("Unknown artist")
        artist_id = getattr(track, "artist_id", None)
        # Render the artist as a clickable link so the label's ``activate-link``
        # signal actually fires (a plain ``set_label`` has no <a href> to click,
        # which is why artist navigation silently no-op'd). Falls back to plain
        # text when the track has no artist id (e.g. some radio tracks).
        if artist_id:
            import html
            self.artist_label.set_markup(
                "<a href='artist:%s'>%s</a>" % (artist_id, html.escape(artist))
            )
        else:
            self.artist_label.set_text(artist)

        self._update_codec_chip(track)
        self._update_favorite_icon(track)

        # New song always resets the flip back to the album face.
        self._reset_art_flip()

        # Album art via the authenticated image cache.
        if self.image_canc:
            self.image_canc.cancel()
        self.image_canc = Gio.Cancellable.new()
        album_id = getattr(track, "album_id", None) or getattr(track, "id", None)
        if album_id and self.client is not None:
            canc = self.image_canc
            utils.run_async(
                lambda: utils.add_picture_from_tag(
                    self.playing_track_picture, album_id, None, 640, canc
                ),
                owner=self._owner,
            )
            utils.run_async(
                lambda: utils.add_image_from_tag(
                    self.playing_track_image, album_id, None, 80, canc
                ),
                owner=self._owner,
            )

        # Artist art for the flip side. Default to the vinyl placeholder, then
        # load the artist's primary image if they have one (cancellable so a
        # late fetch from a prior song can't paint over the current artist).
        if self.artist_image_canc:
            self.artist_image_canc.cancel()
        self.artist_image_canc = Gio.Cancellable.new()
        self.playing_track_artist_picture.set_resource(self._PLACEHOLDER_RESOURCE)
        if artist_id and self.client is not None:
            artist_canc = self.artist_image_canc
            utils.run_async(
                lambda: self._load_artist_art(artist_id, artist_canc),
                owner=self._owner,
            )

        # Lyrics.
        utils.run_async(lambda: self._load_lyrics(track), owner=self._owner)

        self._update_slider()

    def _load_artist_art(self, artist_id, cancellable):
        """Worker: resolve the artist's primary image and paint the flip side.

        Reads the artist's ``image_tag`` from the local db (same lookup the
        artist page uses) and routes through the pooled, disk-cached image
        fetch. An artist with no row / no image simply keeps the vinyl
        placeholder already set on the picture.
        """
        image_tag = None
        try:
            row = self.db.read(
                lambda c: c.execute(
                    "SELECT image_tag FROM artists WHERE id=?", (artist_id,)
                ).fetchone()
            )
            if row is not None:
                image_tag = row["image_tag"]
        except Exception:
            logger.debug("artist image_tag lookup failed", exc_info=True)
        utils.add_picture_from_tag(
            self.playing_track_artist_picture,
            artist_id,
            image_tag,
            640,
            cancellable,
        )

    def _update_codec_chip(self, track):
        codec = getattr(track, "codec", None)
        bitrate = getattr(track, "bitrate", None)
        if codec:
            text = codec.upper()
            if bitrate:
                text += " (%d kbps)" % (bitrate // 1000)
            self.quality_label.set_label(text)
            self.quality_label.set_visible(True)
        else:
            self.quality_label.set_visible(False)

    def _update_favorite_icon(self, track):
        if getattr(track, "is_favorite", False):
            self.in_my_collection_button.set_icon_name("heart-filled-symbolic")
        else:
            self.in_my_collection_button.set_icon_name(
                "heart-outline-thick-symbolic"
            )

    def _load_lyrics(self, track):
        track_id = getattr(track, "id", None)
        if not track_id or self.client is None:
            GLib.idle_add(self.lyrics_widget.clear)
            return
        # db ai_cache first. A cached empty list ([]) is treated as a miss so
        # previously-poisoned rows self-heal (see lib.lyrics_cache).
        cached = None
        try:
            cached = self.db.ai_cache_get("lyrics", track_id)
        except Exception:
            cached = None
        if lyrics_cache.should_use_cache(cached):
            lines = cached
        else:
            fetch_ok = False
            try:
                fetched = self.client.lyrics(track_id)
                lines = [[ln.text, ln.start_ticks] for ln in fetched]
                fetch_ok = True
            except Exception:
                logger.debug("lyrics fetch failed", exc_info=True)
                lines = []
            # Jellyfin had none -> optionally fall back to LRCLIB (external).
            if not lines and self._external_lyrics_enabled():
                external = self._fetch_external_lyrics(track)
                if external:
                    lines = external
                    fetch_ok = True
            # Only cache a successful, non-empty fetch — never poison with [].
            if lyrics_cache.should_cache(lines, fetch_ok):
                try:
                    self.db.ai_cache_set("lyrics", track_id, lines)
                except Exception:
                    logger.debug("lyrics cache write failed", exc_info=True)
        text = lyrics_to_text(lines)
        if text:
            GLib.idle_add(self.lyrics_widget.set_lyrics, text)
        else:
            GLib.idle_add(self.lyrics_widget.clear)

    def _external_lyrics_enabled(self):
        try:
            return bool(self.settings.get_boolean("external-lyrics"))
        except Exception:
            return False

    def _fetch_external_lyrics(self, track):
        """Try LRCLIB for the given track; return widget-shape lines or None.

        Returns the ``[[text, start_ticks], ...]`` shape (list-of-lists) so it
        matches the ai_cache schema; ``fetch_lrclib`` yields tuples.
        """
        artist = getattr(track, "artist_name", None)
        title = getattr(track, "name", None)
        if not artist or not title:
            return None
        album = getattr(track, "album_name", None)
        duration_ticks = getattr(track, "duration_ticks", None)
        duration_secs = (
            duration_ticks // 10_000_000 if duration_ticks else None
        )
        try:
            result = lyrics_providers.fetch_lrclib(
                artist, title, album=album, duration_secs=duration_secs
            )
        except Exception:
            logger.debug("external lyrics fetch failed", exc_info=True)
            return None
        if not result:
            return None
        return [[text, ticks] for (text, ticks) in result]

    # ------------------------------------------------------------------ #
    # Slider / position                                                  #
    # ------------------------------------------------------------------ #

    def _update_slider(self, *args):
        self.duration = self.player_object.duration
        end_value = self.duration / Gst.SECOND if self.duration else 0
        self.volume_button.get_adjustment().set_value(
            self.player_object.query_volume()
        )

        position_ns = self.player_object.query_position()
        position = position_ns / Gst.SECOND
        self.time_played_label.set_label(utils.pretty_duration(position))
        remaining = max(0, end_value - position)
        self.duration_label.set_label(utils.pretty_duration(remaining))
        self.lyrics_widget.set_time(position)

        fraction = position / end_value if end_value else 0
        self.small_progress_bar.set_fraction(fraction)
        self.progress_bar.get_adjustment().set_value(fraction)
        self.previous_fraction = fraction

    def on_slider_seek(self, *args):
        seek_fraction = self.progress_bar.get_value()
        if abs(seek_fraction - self.previous_fraction) == 0.0:
            return
        self.player_object.seek(seek_fraction)
        end = self.duration / Gst.SECOND if self.duration else 0
        position = seek_fraction * end
        self.time_played_label.set_label(utils.pretty_duration(position))
        self.lyrics_widget.set_time(position)
        self.small_progress_bar.set_fraction(seek_fraction)
        self.previous_fraction = seek_fraction

    def on_seek_from_lyrics(self, lyrics_widget, time_ms):
        end = self.duration / Gst.SECOND if self.duration else 0
        if end == 0:
            return
        self.player_object.seek((time_ms / 1000) / end)

    # ------------------------------------------------------------------ #
    # Buffering                                                          #
    # ------------------------------------------------------------------ #

    def _on_buffering(self, player, percentage):
        self.buffer_spinner.set_visible(percentage != 100)

    # ------------------------------------------------------------------ #
    # Transport                                                          #
    # ------------------------------------------------------------------ #

    def on_play_pause(self, *args):
        self.player_object.play_pause()

    def on_skip_forward(self, *args):
        self.player_object.play_next()

    def on_skip_backward(self, *args):
        self.player_object.play_previous()

    def on_shuffle_toggled(self, btn):
        self.player_object.shuffle = btn.get_active()

    def on_repeat_clicked(self, *args):
        if self.player_object.repeat_type == RepeatType.NONE:
            self.player_object.repeat_type = RepeatType.LIST
        elif self.player_object.repeat_type == RepeatType.LIST:
            self.player_object.repeat_type = RepeatType.SONG
        else:
            self.player_object.repeat_type = RepeatType.NONE
        self.settings.set_int("repeat", self.player_object.repeat_type)

    def on_volume_changed(self, widget, value):
        self.player_object.change_volume(value)
        self.settings.set_int("last-volume", int(value * 10))

    # ------------------------------------------------------------------ #
    # Icon row                                                           #
    # ------------------------------------------------------------------ #

    def on_favorite_clicked(self, btn):
        track = self.player_object.playing_track
        if track is None or self.client is None:
            return
        track_id = getattr(track, "id", None)
        current = bool(getattr(track, "is_favorite", False))

        def applied(state):
            # Mirror onto the live track object + optimistic icon.
            try:
                track.is_favorite = bool(state)
            except Exception:
                pass
            self._update_favorite_icon(track)

        # Single-sourced write-through (server + db). Optimistic icon flip.
        new_state = utils.toggle_favorite(
            "track", track_id, current, owner=self._owner, on_applied=applied,
        )
        try:
            track.is_favorite = new_state
        except Exception:
            pass
        self._update_favorite_icon(track)

    def on_share_clicked(self, *args):
        track = self.player_object.playing_track
        if track is None:
            return
        track_id = getattr(track, "id", "")
        server_url = self.settings.get_string("server-url")
        server_id = getattr(self.client, "server_id", "") if self.client else ""
        link = "%s/web/#/details?id=%s&serverId=%s" % (
            server_url, track_id, server_id or ""
        )
        clipboard = Gdk.Display.get_default().get_clipboard()
        clipboard.set(link)
        self._send_toast(_("Link copied"), 2)

    def on_track_radio(self, *args):
        track = self.player_object.playing_track
        if track is None:
            return
        track_id = getattr(track, "id", None)
        if not track_id:
            return
        # Remember the user's chosen seed before playback swaps playing_track.
        self._radio_seed_id = track_id

        # Prefer the AI track radio (cardinal-rule selection w/ auto-fallback to
        # Jellyfin instant_mix). discovery.track_radio never raises and tags the
        # result with its source; the toast reflects which path was used.
        def work():
            if utils.discovery is not None:
                return utils.discovery.track_radio(track_id)
            # No runtime discovery (e.g. pre-login): direct instant_mix.
            from ..lib.jellyfin.models import Track
            items = self.client.instant_mix(track_id) if self.client else []
            return {"source": "instant_mix",
                    "tracks": [Track.from_item(it) for it in items]}

        def on_error(exc):
            if isinstance(exc, JellyfinError):
                self._send_toast(_("Could not start track radio"), 2)
                logger.info("track radio failed: %s", exc)
            else:
                logger.exception("track radio failed", exc_info=exc)

        utils.run_async(
            work, on_done=self._play_track_radio, on_error=on_error,
            owner=self._owner,
        )

    def _play_track_radio(self, result):
        tracks = result.get("tracks") if isinstance(result, dict) else result
        if not tracks:
            self._send_toast(_("Could not start track radio"), 2)
            return False
        self.player_object.play_this(tracks, 0)
        source = result.get("source") if isinstance(result, dict) else None
        if source == "ai":
            self._send_toast(_("Playing AI track radio"), 2)
        else:
            self._send_toast(_("Playing track radio"), 2)
        # Start a radio session so the queue-low extender can keep it going.
        self._begin_radio_session(result)
        return False

    def _begin_radio_session(self, result):
        """Notify the window that a radio is now active (for queue-low extend).

        The window owns the RadioSession + the song-changed extender; the pane
        just hands it the seed (the user's chosen track, captured before
        playback swapped ``playing_track``) + the served ids of this batch.
        """
        seed_id = getattr(self, "_radio_seed_id", None)
        tracks = result.get("tracks") if isinstance(result, dict) else result
        served = [getattr(t, "id", None) for t in (tracks or [])]
        served = [s for s in served if s]
        begin = getattr(self._owner, "begin_radio_session", None)
        if callable(begin) and seed_id:
            begin(seed_id, served)

    def on_album_jump(self, *args):
        """Player-pane album button -> push the current track's album page.

        Some tracks (e.g. radio/instant-mix items) have no ``album_id``; rather
        than silently no-op, surface a toast so the dead button is explained.
        """
        track = self.player_object.playing_track
        album_id = getattr(track, "album_id", None) if track is not None else None
        if not album_id:
            self._send_toast(_("No album for this track"), 2)
            return
        self._owner.activate_action(
            "win.push-album-page", GLib.Variant("s", str(album_id))
        )

    def _on_artist_link(self, label, uri):
        """Artist label link -> push the artist page.

        The link uri is ``artist:<id>`` (set in ``_on_song_changed``); prefer
        that id, falling back to the live track's ``artist_id``. Returning True
        stops GTK from trying to open the (non-http) uri itself.
        """
        artist_id = ""
        if uri and ":" in uri:
            artist_id = uri.partition(":")[2]
        if not artist_id:
            track = self.player_object.playing_track
            artist_id = (
                getattr(track, "artist_id", None) if track is not None else None
            )
        if artist_id:
            self._owner.activate_action(
                "win.push-artist-page", GLib.Variant("s", str(artist_id))
            )
        return True

    # ------------------------------------------------------------------ #
    # Control updates                                                    #
    # ------------------------------------------------------------------ #

    def _update_controls(self, *args):
        if self.player_object.playing:
            self.play_button.set_icon_name("media-playback-pause-symbolic")
        else:
            self.play_button.set_icon_name("media-playback-start-symbolic")

    def _update_repeat_button(self, player, _param):
        match player.repeat_type:
            case RepeatType.NONE:
                self.repeat_button.set_icon_name(
                    "media-playlist-consecutive-symbolic"
                )
            case RepeatType.LIST:
                self.repeat_button.set_icon_name("media-playlist-repeat-symbolic")
            case RepeatType.SONG:
                self.repeat_button.set_icon_name("playlist-repeat-song-symbolic")

    def _on_shuffle_changed(self, *args):
        self.shuffle_button.set_active(self.player_object.shuffle)
