# track_row_widget.py
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

"""Recyclable track row for the virtualized ``Gtk.ListView`` track lists.

Visually identical to :class:`HTGenericTrackWidget` (same ``.blp`` layout —
art, title, artist/album links, duration, now-playing indicator, 3-dot menu
with the playlists submenu) but built for ``GtkSignalListItemFactory`` reuse:
one instance is created per *visible* slot and rebound to a different track as
the user scrolls. The widget owns NO per-track Python state until ``bind`` and
sheds all of it in ``unbind`` so a recycled row never shows stale art, fires a
stale link, or pins a popped track's image fetch.

Lifecycle (driven by the factory in ``track_list_page``):

  * ``setup`` constructs one row, connects the row's OWN long-lived signals
    (menu open, link activate, player song-changed/notify::playing). These are
    connected ONCE and live for the row's whole life — they read the *current*
    bound track via ``self.track`` so they stay correct across rebinds.
  * ``bind(track, index)`` fills the labels, kicks off an owner+version-guarded
    art fetch, and records the index for activation. Bumping ``_bind_version``
    invalidates any in-flight art fetch from a previous bind so a fast scroll
    can't paint the wrong cover (the image-pool job checks the version it
    captured against the row's current version before touching the widget).
  * ``unbind()`` clears the art back to the placeholder, drops the track refs,
    and bumps the version so a late fetch is dropped.

CLAUDE.md GTK rules honored: the row stores NO bound *page* methods (it emits
no page callbacks directly — activation is handled by the ListView's
``activate`` signal on the page side; the row only needs the player singleton
and window actions, both global). Its own signals are tracked and disconnected
in ``teardown`` so recycled-row signal accretion stays bounded.
"""

import html
import logging
from gettext import gettext as _

from gi.repository import Gdk, Gio, GLib, GObject, Gtk

from ..disconnectable_iface import IDisconnectable
from ..lib import utils
from .card_widget import item_get

logger = logging.getLogger(__name__)

_ART_SIZE = 44  # matches the pixel-size in the .blp Image


def _ticks_to_seconds(ticks):
    if not ticks:
        return 0
    return int(ticks // 10_000_000)


@Gtk.Template(
    resource_path="/io/github/tylerreece/timbre/ui/widgets/track_row_widget.ui"
)
class HTTrackRowWidget(Gtk.Box, IDisconnectable):
    """Bindable, recyclable track row (no Gtk.ListBoxRow wrapper).

    The ``Gtk.ListView`` provides the activatable ``Gtk.ListItem`` wrapper, so
    this is a plain ``Gtk.Box`` — putting a ``ListBoxRow`` inside a ``ListView``
    item would double-wrap and break activation styling.
    """

    __gtype_name__ = "HTTrackRowWidget"

    image = Gtk.Template.Child()
    now_playing_indicator = Gtk.Template.Child()
    album_stack = Gtk.Template.Child()
    track_title_label = Gtk.Template.Child()
    track_duration_label = Gtk.Template.Child()
    playlists_submenu = Gtk.Template.Child()
    explicit_label = Gtk.Template.Child()

    artist_label = Gtk.Template.Child()
    artist_label_2 = Gtk.Template.Child()
    track_album_label = Gtk.Template.Child()

    menu_button = Gtk.Template.Child()
    track_menu = Gtk.Template.Child()

    def __init__(self):
        IDisconnectable.__init__(self)
        super().__init__()

        # Per-bind state — all None/empty until bind().
        self.track = None
        self.track_id = None
        self.album_id = None
        self.artist_id = None
        self.index = 0
        self.is_favorite = False
        self.menu_activated = False
        self._playlist_names = {}
        # Bumped on every bind/unbind; an in-flight art fetch captures the value
        # and drops its result if the row was rebound meanwhile (stale-art
        # guard, mirrors the owner-guard pattern in run_async).
        self._bind_version = 0

        self.action_group = Gio.SimpleActionGroup()
        self.insert_action_group("trackwidget", self.action_group)

        # Long-lived row signals (connected ONCE, valid across rebinds — they
        # read self.track / self.*_id which bind() refreshes). Tracked in
        # self.signals; teardown() disconnects them so a recycled-row pool can't
        # accrete handlers on the global player.
        self.signals.append((
            self.menu_button,
            self.menu_button.connect("notify::active", self._on_menu_activate),
        ))
        for label in (self.artist_label, self.artist_label_2,
                      self.track_album_label):
            self.signals.append(
                (label, label.connect("activate-link", self._on_link))
            )
        self.signals.append((
            utils.player_object,
            utils.player_object.connect(
                "song-changed", self._on_player_song_changed
            ),
        ))
        self.signals.append((
            utils.player_object,
            utils.player_object.connect(
                "notify::playing", self._on_playing_changed
            ),
        ))

        # Install the static menu actions once (they read self.track at fire
        # time, so they survive rebinds). The dynamic playlists submenu is the
        # only thing rebuilt per menu-open.
        self._install_actions()

    # ------------------------------------------------------------------ #
    # Bind / unbind                                                       #
    # ------------------------------------------------------------------ #

    def bind(self, track, index):
        """Show ``track`` at list position ``index`` (factory bind step)."""
        self._bind_version += 1
        version = self._bind_version

        self.track = track
        self.index = index
        self.track_id = item_get(track, "id")
        self.album_id = item_get(track, "album_id")
        self.artist_id = item_get(track, "artist_id")
        self.is_favorite = bool(item_get(track, "is_favorite"))
        # A rebind must re-prepend Go-to-album/artist menu items for the new
        # track, so reset the one-shot menu-built guard.
        self.menu_activated = False

        name = item_get(track, "name") or _("Unknown")
        artist_name = item_get(track, "artist_name") or ""
        album_name = item_get(track, "album_name") or ""

        self.track_title_label.set_label(name)
        self._set_link(self.artist_label, "artist", self.artist_id, artist_name)
        self._set_link(self.artist_label_2, "artist", self.artist_id, artist_name)
        self._set_link(self.track_album_label, "album", self.album_id, album_name)
        self.explicit_label.set_visible(False)

        duration = _ticks_to_seconds(item_get(track, "duration_ticks"))
        self.track_duration_label.set_label(utils.pretty_duration(duration))

        # Reset art to the placeholder immediately so a recycled row never shows
        # the previous track's cover while the new one loads.
        self.image.set_from_icon_name("emblem-music-symbolic")
        self._update_now_playing()

        if self.album_id:
            album_id = self.album_id

            def _fetch():
                # Fetch AND decode on the worker pool: the bytes->Gdk.Texture
                # decode happens here (off-main), so the main loop only assigns
                # the ready paintable. Thread-safety proven by
                # tests/manual/texture_threadsafe_probe.py.
                texture = utils.fetch_texture_from_tag(album_id, None, _ART_SIZE)

                def _apply():
                    # Stale-art guard: only paint if this row is STILL bound to
                    # the same generation. A fast scroll rebinds the row and
                    # bumps _bind_version, so a slow fetch from a prior bind is
                    # dropped here instead of painting the wrong cover.
                    if texture is not None and self._bind_version == version:
                        self.image.set_from_paintable(texture)
                    return False

                GLib.idle_add(_apply)

            utils.run_async(_fetch, owner=self, pool=True)

    def unbind(self):
        """Release the current track (factory unbind step).

        Bumps the version (drops any in-flight art fetch), clears the art back
        to the placeholder, and forgets the track so the recycled row carries
        no stale state into its next bind.
        """
        self._bind_version += 1
        self.image.set_from_icon_name("emblem-music-symbolic")
        self.track = None
        self.track_id = None
        self.album_id = None
        self.artist_id = None
        self.menu_activated = False

    def teardown(self):
        """Final teardown when the factory destroys the row (list disposed)."""
        self.unbind()
        self.disconnect_all()

    # ------------------------------------------------------------------ #
    # Link labels                                                        #
    # ------------------------------------------------------------------ #

    def _set_link(self, label, kind, item_id, text):
        if not text:
            label.set_visible(False)
            return
        label.set_visible(True)
        if item_id:
            label.set_markup(
                "<a href='%s:%s'>%s</a>" % (kind, item_id, html.escape(text))
            )
        else:
            label.set_text(text)

    def _on_link(self, label, uri):
        kind, _sep, item_id = uri.partition(":")
        action = {
            "artist": "win.push-artist-page",
            "album": "win.push-album-page",
        }.get(kind)
        if action and item_id:
            self.activate_action(action, GLib.Variant("s", item_id))
        return True

    # ------------------------------------------------------------------ #
    # Now-playing indicator                                              #
    # ------------------------------------------------------------------ #

    def _on_player_song_changed(self, *args):
        self._update_now_playing()

    def _on_playing_changed(self, *args):
        self._update_now_playing()

    def _update_now_playing(self):
        current = utils.player_object.playing_track
        cur_id = getattr(current, "id", None) if current is not None else None
        is_now = (
            cur_id is not None
            and self.track_id is not None
            and self.track_id == cur_id
        )
        if is_now and utils.player_object.playing:
            self.album_stack.set_visible_child(self.now_playing_indicator)
        else:
            self.album_stack.set_visible_child(self.image)

    # ------------------------------------------------------------------ #
    # Context menu                                                       #
    # ------------------------------------------------------------------ #

    def _install_actions(self):
        action_entries = [
            ("play-next", self._play_next),
            ("add-to-queue", self._add_to_queue),
            ("copy-share-url", self._copy_share_url),
            ("new-playlist", self._on_new_playlist),
        ]
        for name, callback in action_entries:
            action = Gio.SimpleAction.new(name, None)
            self.signals.append((action, action.connect("activate", callback)))
            self.action_group.add_action(action)

        add_action = Gio.SimpleAction.new(
            "add-to-playlist", GLib.VariantType.new("s")
        )
        self.signals.append((
            add_action, add_action.connect("activate", self._on_add_to_playlist)
        ))
        self.action_group.add_action(add_action)

    def _on_menu_activate(self, menu_button, *args):
        if menu_button.get_active():
            self._refresh_playlists_submenu()

        if self.menu_activated:
            return
        self.menu_activated = True

        # Drop any Go-to items left by a previous bind, then prepend this
        # track's. The two static "Go to ..." items live at the FRONT of the
        # menu (prepended); the trailing static items + submenu are model-fixed.
        # On rebind menu_activated is reset to False, but the menu model itself
        # persists — so strip prior Go-to entries before re-prepending.
        self._strip_goto_items()
        if self.artist_id:
            self.track_menu.prepend(
                _("Go to artist"),
                "win.push-artist-page::%s" % self.artist_id,
            )
        if self.album_id:
            self.track_menu.prepend(
                _("Go to album"),
                "win.push-album-page::%s" % self.album_id,
            )

    def _strip_goto_items(self):
        """Remove leading 'Go to album'/'Go to artist' entries from the menu.

        The menu model is reused across rebinds; without stripping, each
        menu-open after a rebind would stack a fresh pair of Go-to items.
        Identify them by their action prefix (push-album/artist-page).
        """
        i = 0
        while i < self.track_menu.get_n_items():
            action = self.track_menu.get_item_attribute_value(
                i, Gio.MENU_ATTRIBUTE_ACTION, GLib.VariantType.new("s")
            )
            val = action.get_string() if action is not None else ""
            if val.startswith("win.push-album-page") or \
                    val.startswith("win.push-artist-page"):
                self.track_menu.remove(i)
            else:
                i += 1

    def _refresh_playlists_submenu(self):
        db = utils.db
        if db is None:
            return

        def work():
            return db.all_playlists()

        utils.run_async(
            work,
            on_done=self._build_playlists_submenu,
            owner=self,
        )

    def _build_playlists_submenu(self, playlists):
        submenu = self.playlists_submenu
        submenu.remove_all()
        submenu.append(_("New playlist…"), "trackwidget.new-playlist")
        for pl in playlists:
            pid = pl.get("id")
            name = pl.get("name") or _("Unknown")
            if not pid:
                continue
            item = Gio.MenuItem.new(name, None)
            item.set_action_and_target_value(
                "trackwidget.add-to-playlist", GLib.Variant("s", pid)
            )
            submenu.append_item(item)
        self._playlist_names = {
            pl.get("id"): (pl.get("name") or _("Unknown")) for pl in playlists
        }

    def _play_next(self, *args):
        if self.track is not None:
            utils.player_object.add_next(self.track)

    def _add_to_queue(self, *args):
        if self.track is not None:
            utils.player_object.add_to_queue(self.track)

    def _on_add_to_playlist(self, _action, target):
        if target is None or self.track_id is None:
            return
        playlist_id = target.get_string()
        name = self._playlist_names.get(playlist_id, _("playlist"))
        utils.add_track_to_playlist(
            playlist_id, name, self.track_id, owner=self.get_root()
        )

    def _on_new_playlist(self, *args):
        from ..new_playlist import NewPlaylistWindow

        if self.track_id is None:
            return
        track_id = self.track_id
        dialog = NewPlaylistWindow()

        def on_create(_dlg, title, _description):
            title = (title or "").strip()
            if not title:
                return
            utils.create_playlist_with_track(
                title, track_id, owner=self.get_root()
            )

        dialog.connect("create-playlist", on_create)
        dialog.connect("create-playlist", lambda *a: dialog.close())
        dialog.present(self.get_root())

    def _copy_share_url(self, *args):
        server_url = ""
        server_id = ""
        if utils.settings is not None:
            server_url = utils.settings.get_string("server-url")
        if utils.client is not None:
            server_id = getattr(utils.client, "server_id", "") or ""
        link = "%s/web/#/details?id=%s&serverId=%s" % (
            server_url, self.track_id or "", server_id
        )
        Gdk.Display.get_default().get_clipboard().set(link)
        utils.send_toast(_("Link copied"), 2)
