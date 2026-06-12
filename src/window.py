# window.py
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

# Phase 4: full app shell. Startup flow (restore/onboard), live sidebar player
# pane, lyrics + queue tabs, background audio, preferences/logout.

import logging
from gettext import gettext as _
from typing import Callable

from gi.repository import Adw, Gio, GLib, GObject, Gtk

from .lib import PlayerObject, SecretStore, utils
from .lib.db import Database, default_db_path
from .lib.deep_link import parse_jellyfin_uri
from .lib.radio_session import RadioSession
from .lib.jellyfin.client import JellyfinClient, JellyfinError
from .lib.jellyfin.sync import LibrarySync
from .mpris import MPRIS
from .widgets import (HTGenericTrackWidget, HTLinkLabelWidget,
                      HTLyricsWidget, HTQueueWidget, JTQueueRow)
from .widgets.player_pane import JTPlayerPane

logger = logging.getLogger(__name__)

# Register custom widget GTypes before the template is loaded
GObject.type_register(HTGenericTrackWidget)
GObject.type_register(HTLinkLabelWidget)
GObject.type_register(HTQueueWidget)
GObject.type_register(HTLyricsWidget)
GObject.type_register(JTQueueRow)


@Gtk.Template(resource_path="/io/github/tylerreece/timbre/ui/window.ui")
class TimbreWindow(Adw.ApplicationWindow):
    __gtype_name__ = "TimbreWindow"

    progress_bar = Gtk.Template.Child()
    duration_label = Gtk.Template.Child()
    time_played_label = Gtk.Template.Child()
    shuffle_button = Gtk.Template.Child()
    navigation_view = Gtk.Template.Child()
    play_button = Gtk.Template.Child()
    small_progress_bar = Gtk.Template.Child()
    song_title_label = Gtk.Template.Child()
    playing_track_picture = Gtk.Template.Child()
    playing_track_artist_picture = Gtk.Template.Child()
    playing_track_art_stack = Gtk.Template.Child()
    playing_track_image = Gtk.Template.Child()
    artist_label = Gtk.Template.Child()
    miniplayer_artist_label = Gtk.Template.Child()
    volume_button = Gtk.Template.Child()
    in_my_collection_button = Gtk.Template.Child()
    explicit_label = Gtk.Template.Child()
    queue_widget = Gtk.Template.Child()
    lyrics_widget = Gtk.Template.Child()
    repeat_button = Gtk.Template.Child()
    home_button = Gtk.Template.Child()
    explore_button = Gtk.Template.Child()
    collection_button = Gtk.Template.Child()
    player_lyrics_queue = Gtk.Template.Child()
    navigation_buttons = Gtk.Template.Child()
    buffer_spinner = Gtk.Template.Child()
    quality_label = Gtk.Template.Child()
    toast_overlay = Gtk.Template.Child()
    playing_track_widget = Gtk.Template.Child()
    sidebar_stack = Gtk.Template.Child()
    go_next_button = Gtk.Template.Child()
    go_prev_button = Gtk.Template.Child()
    track_radio_button = Gtk.Template.Child()
    album_button = Gtk.Template.Child()
    copy_share_link = Gtk.Template.Child()

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

        self.settings = Gio.Settings.new("io.github.tylerreece.timbre")

        self.settings.bind(
            "window-width", self, "default-width", Gio.SettingsBindFlags.DEFAULT
        )
        self.settings.bind(
            "window-height", self, "default-height", Gio.SettingsBindFlags.DEFAULT
        )

        # Shared services.
        self.secret_store = SecretStore()
        self.db = Database(default_db_path())
        self.client: JellyfinClient | None = None
        self.is_logged_in = False
        self._alive = True  # cleared on unrealize; gates run_async callbacks
        self._held = False  # app.hold() balance guard
        self._refreshing = False  # reentrancy guard for on_refresh_home (F5)
        self._cache_evicted = False  # once-per-session disk-cache eviction guard
        self._radio_session = RadioSession()  # queue-low AI radio extender
        # Deep link (jellyfin://) received before login completes is stashed
        # here and drained once the runtime is live (see _enter_logged_in).
        self._pending_deep_link: str | None = None

        # Navigation push actions (Phase 5 — real browse pages).
        self.create_action_with_target(
            "push-artist-page", GLib.VariantType.new("s"), self.on_push_artist_page
        )
        self.create_action_with_target(
            "push-album-page", GLib.VariantType.new("s"), self.on_push_album_page
        )
        self.create_action_with_target(
            "push-playlist-page", GLib.VariantType.new("s"),
            self.on_push_playlist_page,
        )
        # Track-radio button (blp action: win.push-track-radio-page) plays an
        # instant mix.
        self.create_action_with_target(
            "push-track-radio-page", GLib.VariantType.new("s"), self.on_track_radio
        )

        # Primary-menu Logout. The ported menu item targeted High Tide's
        # never-registered "app.log-out", so GTK rendered it insensitive.
        logout_action = Gio.SimpleAction.new("log-out", None)
        logout_action.connect("activate", lambda *_a: self.logout())
        self.add_action(logout_action)

        # F5 / refresh: re-run an incremental sync, then rebuild the Home page.
        refresh_action = Gio.SimpleAction.new("refresh-home", None)
        refresh_action.connect("activate", self.on_refresh_home)
        self.add_action(refresh_action)

        # Ctrl+F: jump to the Explore page and focus its search entry.
        search_action = Gio.SimpleAction.new("focus-search", None)
        search_action.connect("activate", self.on_focus_search)
        self.add_action(search_action)

        # Transport shortcuts (accels bound in main.py). These delegate to the
        # player pane so the keyboard path matches the button path exactly.
        # (play-pause has no GAction: Space is handled by the bubble-phase
        # ShortcutController below, and the play button by its own callback.)
        for name, handler in (
            ("next-track", lambda *_a: self.player_pane.on_skip_forward(None)),
            ("prev-track", lambda *_a: self.player_pane.on_skip_backward(None)),
        ):
            act = Gio.SimpleAction.new(name, None)
            act.connect("activate", handler)
            self.add_action(act)

        # Space → play/pause via a BUBBLE-phase shortcut controller on the
        # window (not a global accel). A global accel from
        # set_accels_for_action fires at the capture phase, ahead of the focused
        # widget, so typing a space into the Explore search entry would toggle
        # play/pause instead of inserting a space. At the bubble phase, a focused
        # Gtk.Text/SearchEntry handles (and consumes) the keypress first; the
        # shortcut only fires when the event bubbles back up unhandled — i.e.
        # nothing editable was focused.
        space_shortcuts = Gtk.ShortcutController()
        space_shortcuts.set_propagation_phase(Gtk.PropagationPhase.BUBBLE)
        space_shortcuts.add_shortcut(
            Gtk.Shortcut.new(
                Gtk.ShortcutTrigger.parse_string("space"),
                Gtk.CallbackAction.new(self._on_space_play_pause),
            )
        )
        self.add_controller(space_shortcuts)

        # Player object (client/db injected; settings provides max-bitrate).
        self.player_object = PlayerObject(
            self.settings.get_int("preferred-sink"),
            self.settings.get_string("alsa-device"),
            self.settings.get_boolean("normalize"),
            self.settings.get_boolean("quadratic-volume"),
            client=None,
            db=self.db,
            settings=self.settings,
        )
        utils.player_object = self.player_object
        utils.navigation_view = self.navigation_view
        utils.toast_overlay = self.toast_overlay
        utils.settings = self.settings
        utils.db = self.db

        # Player pane controller — owns the cohesive player-pane cluster while
        # the window keeps startup/nav/queue/background-audio plumbing. The .blp
        # tree is unchanged; the pane wraps the template children passed here.
        self.player_pane = JTPlayerPane(
            player=self.player_object,
            client=None,
            db=self.db,
            settings=self.settings,
            send_toast=utils.send_toast,
            owner=self,
            widgets={
                "progress_bar": self.progress_bar,
                "duration_label": self.duration_label,
                "time_played_label": self.time_played_label,
                "shuffle_button": self.shuffle_button,
                "play_button": self.play_button,
                "small_progress_bar": self.small_progress_bar,
                "song_title_label": self.song_title_label,
                "playing_track_picture": self.playing_track_picture,
                "playing_track_artist_picture": self.playing_track_artist_picture,
                "playing_track_art_stack": self.playing_track_art_stack,
                "playing_track_image": self.playing_track_image,
                "artist_label": self.artist_label,
                "miniplayer_artist_label": self.miniplayer_artist_label,
                "volume_button": self.volume_button,
                "in_my_collection_button": self.in_my_collection_button,
                "lyrics_widget": self.lyrics_widget,
                "repeat_button": self.repeat_button,
                "buffer_spinner": self.buffer_spinner,
                "quality_label": self.quality_label,
            },
        )

        self._connect_player_signals()
        self.player_pane.init_repeat_and_volume()

        # MPRIS (works headless; harmless before login).
        try:
            MPRIS(self.player_object, client=None)
        except Exception:
            logger.debug("MPRIS unavailable", exc_info=True)

        self._show_loading_placeholder()

        # Kick off startup once the window is realised.
        GLib.idle_add(self._startup)

        logger.info("TimbreWindow initialised")

    # ------------------------------------------------------------------ #
    # Signal wiring                                                      #
    # ------------------------------------------------------------------ #

    def _connect_player_signals(self):
        # Window-level signal wiring only (background-audio hold, queue refresh,
        # transport-sensitivity). The player-pane cluster wires its own signals
        # in JTPlayerPane. song-changed also refreshes the queue tab here.
        po = self.player_object
        po.connect("notify::playing", self._on_playing_for_background)
        po.connect("song-changed", self._on_queue_changed)
        po.connect("song-changed", self._on_song_changed_for_radio)
        po.connect("song-added-to-queue", self._on_queue_changed)
        # Queue mutations (remove/move/clear from the queue tab) refresh rows.
        po.connect("songs-list-changed", self._on_queue_changed)
        po.connect(
            "notify::can-go-next",
            lambda *_: self.go_next_button.set_sensitive(po.can_go_next),
        )
        po.connect(
            "notify::can-go-prev",
            lambda *_: self.go_prev_button.set_sensitive(po.can_go_prev),
        )

    # ------------------------------------------------------------------ #
    # Startup flow                                                       #
    # ------------------------------------------------------------------ #

    def _startup(self):
        creds = self.secret_store.load()
        server_url = creds.get("server-url") or self.settings.get_string("server-url")
        token = creds.get("token")
        user_id = creds.get("user-id")
        server_id = creds.get("server-id")
        device_id = self.settings.get_string("device-id")

        if server_url and token and device_id:
            client = JellyfinClient(server_url, device_id=device_id)

            def work():
                try:
                    return client.restore(token, user_id, server_id)
                except JellyfinError:
                    return False

            def done(ok):
                if ok:
                    self._enter_logged_in(client)
                else:
                    self._show_onboarding()

            utils.run_async(work, on_done=done, owner=self)
        else:
            self._show_onboarding()
        return False

    def _show_onboarding(self):
        from .onboarding import JTOnboarding

        dialog = JTOnboarding(
            self.secret_store, self.settings, self.db, self._enter_logged_in
        )
        # If the user dismisses the dialog without finishing sign-in, the
        # window must not sit on "Loading your library…" forever — swap to an
        # explicit sign-in prompt instead. One-shot dialog: the handler does
        # not need to live in self.signals.
        dialog.connect("closed", self._on_onboarding_closed)
        dialog.present(self)
        return False

    def _on_onboarding_closed(self, dialog):
        if not getattr(dialog, "completed", False) and not self.is_logged_in:
            self._show_signin_placeholder()

    def _show_signin_placeholder(self):
        """StatusPage shown when onboarding was dismissed without signing in."""
        page = Adw.NavigationPage()
        page.set_title(_("Timbre"))
        page.set_tag("home")
        tb = Adw.ToolbarView()
        tb.add_top_bar(Adw.HeaderBar())
        status = Adw.StatusPage(
            icon_name="system-users-symbolic",
            title=_("Please sign in"),
            description=_("Connect to your Jellyfin server to start listening."),
        )
        button = Gtk.Button(label=_("Sign In…"), halign=Gtk.Align.CENTER)
        button.add_css_class("suggested-action")
        button.add_css_class("pill")
        button.connect("clicked", lambda *_a: self._show_onboarding())
        status.set_child(button)
        tb.set_content(status)
        page.set_child(tb)
        self.navigation_view.replace([page])
        return False

    def _enter_logged_in(self, client):
        """Transition the window into the authenticated runtime."""
        self.client = client
        self.player_pane.client = client
        self.player_object._client = client  # player builds stream URLs
        self.is_logged_in = True

        utils.init_runtime(
            client, self.db, self.player_object, self.settings,
            secret_store=self.secret_store,
        )

        self.navigation_buttons.set_sensitive(True)
        self.player_lyrics_queue.set_sensitive(True)

        self._show_library_status()

        # Drain any deep link (jellyfin://) that arrived before login finished.
        # The navigation_view now hosts the real Home page, so pushing on top of
        # it is safe.
        if self._pending_deep_link is not None:
            uri, self._pending_deep_link = self._pending_deep_link, None
            self.handle_deep_link(uri)

        # Background incremental sync of the selected libraries.
        library_ids = list(self.settings.get_strv("selected-libraries"))
        if library_ids:
            utils.run_async(
                lambda: self._run_incremental_sync(client, library_ids),
                on_done=self._on_startup_sync_done,
                owner=self,
            )
        else:
            # No libraries to sync — still run the once-per-session cache
            # eviction at this quiet startup moment.
            self._maybe_evict_cache()
        return False

    def _on_startup_sync_done(self, _result):
        """Startup incremental sync completed — refresh Home, then trim cache.

        The disk image-cache eviction is deferred to here (the first quiet
        moment after the initial sync) so it never competes with login/sync
        for I/O, and runs at most once per session.
        """
        self._show_library_status()
        self._maybe_evict_cache()

    def _maybe_evict_cache(self):
        """Trim the disk image cache to its budget once per app session.

        Idempotent within a session: the ``_cache_evicted`` guard means a
        later F5 / Collection-button re-sync won't re-run it. Eviction is pure
        filesystem work, so it runs on a worker thread (NEVER the main thread)
        via ``utils.run_async``; a nonzero result is logged.
        """
        if self._cache_evicted:
            return
        self._cache_evicted = True

        img_dir = getattr(utils, "IMG_DIR", None)
        if img_dir is None:
            return
        max_gb = utils.IMAGE_CACHE_MAX_BYTES / 1024 ** 3

        def work():
            return utils.evict_cache(img_dir, max_gb)

        def done(result):
            files, freed = result if result else (0, 0)
            if files:
                logger.info(
                    "Disk image cache eviction: removed %d files, freed %d bytes",
                    files, freed,
                )

        utils.run_async(work, on_done=done, owner=self)

    def _run_incremental_sync(self, client, library_ids):
        sync = LibrarySync(client, self.db)
        try:
            sync.incremental_sync(library_ids)
        except Exception:
            logger.exception("incremental sync failed")

    # ------------------------------------------------------------------ #
    # Placeholder pages                                                  #
    # ------------------------------------------------------------------ #

    def _show_loading_placeholder(self):
        self.navigation_view.replace([self._status_page(
            "home", _("Timbre"), "emblem-music-symbolic",
            _("Loading your library…"),
        )])

    def _show_library_status(self):
        """Install the real Home page (or an empty-library StatusPage).

        The Home page is the post-login landing surface; when the synced library
        is genuinely empty (nothing to render) we fall back to the original
        StatusPage so the user isn't shown a blank page.
        """
        if self.db.track_count() == 0:
            page = self._status_page(
                "home", _("Timbre"), "emblem-music-symbolic",
                _("Your library is empty — it will appear here once synced."),
            )
            self.navigation_view.replace([page])
            self.home_button.set_active(True)
            return False

        from .pages import HTHomePage

        home = HTHomePage()
        home.set_tag("home")
        self.navigation_view.replace([home])
        home.load()
        self.home_button.set_active(True)
        return False

    def _status_page(self, tag, title, icon, description):
        page = Adw.NavigationPage()
        page.set_title(title)
        page.set_tag(tag)
        tb = Adw.ToolbarView()
        tb.add_top_bar(Adw.HeaderBar())
        tb.set_content(Adw.StatusPage(
            icon_name=icon, title=title, description=description
        ))
        page.set_child(tb)
        return page

    # ------------------------------------------------------------------ #
    # Browse page push actions (Phase 5)                                 #
    # ------------------------------------------------------------------ #

    def on_push_artist_page(self, action, parameter):
        item_id = parameter.get_string()
        if not item_id:
            return
        from .pages import HTArtistPage
        self.navigation_view.push(HTArtistPage.new_from_id(item_id).load())

    def on_push_album_page(self, action, parameter):
        item_id = parameter.get_string()
        if not item_id:
            return
        from .pages import HTAlbumPage
        self.navigation_view.push(HTAlbumPage.new_from_id(item_id).load())

    def on_push_playlist_page(self, action, parameter):
        item_id = parameter.get_string()
        if not item_id:
            return
        from .pages import HTPlaylistPage
        self.navigation_view.push(HTPlaylistPage.new_from_id(item_id).load())

    # ------------------------------------------------------------------ #
    # Deep links (jellyfin:// scheme handler)                            #
    # ------------------------------------------------------------------ #

    def handle_deep_link(self, uri):
        """Resolve a jellyfin:// (or web) URI and navigate to the item.

        Entry point called by the application's ``do_open``. The window is
        already presented by the caller. Behaviour:

        * Parse the URI to a bare item id (gi-free grammar). Unparseable URIs
          are logged and dropped — no toast (nothing to act on).
        * If the runtime isn't ready yet (mid-onboarding / pre-login), stash the
          URI; ``_enter_logged_in`` drains it once the db/session are live.
        * Otherwise look the id up in the LOCAL db off the main thread and push
          the matching page via the existing win.push-*-page actions. A track id
          resolves to its album page. An unknown id toasts
          "Item not in your library".
        """
        item_id = parse_jellyfin_uri(uri)
        if not item_id:
            logger.info("deep link had no resolvable item id: %s", uri)
            return

        # Runtime not ready (still onboarding / restoring): stash and bail. The
        # window is already presented by the caller; no crash, just defer.
        if not self.is_logged_in or self.client is None:
            logger.info("deep link before login; stashing: %s", uri)
            self._pending_deep_link = uri
            return

        def work():
            return self._classify_deep_link_id(item_id)

        def done(result):
            self._navigate_deep_link(result)

        utils.run_async(work, on_done=done, owner=self)

    def _classify_deep_link_id(self, item_id):
        """Resolve an item id to (kind, nav_id) by querying the local db.

        Runs OFF the main thread (called inside run_async work). Returns a
        ``(kind, nav_id)`` tuple where kind is one of
        ``"album"/"artist"/"playlist"`` and nav_id is the id to push; a track id
        resolves to ``("album", <album_id>)``. Returns ``None`` when the id is
        not present in the local library (or is a track whose album is unknown).

        The id grammar is hex; the db stores packed ids. Match
        case-insensitively against each table by id.
        """
        def query(conn):
            # Tracks first: a track id resolves to its album page.
            row = conn.execute(
                "SELECT album_id FROM tracks WHERE id=? COLLATE NOCASE",
                (item_id,),
            ).fetchone()
            if row is not None:
                album_id = row["album_id"]
                return ("album", album_id) if album_id else None
            if conn.execute(
                "SELECT 1 FROM albums WHERE id=? COLLATE NOCASE", (item_id,)
            ).fetchone() is not None:
                return ("album", item_id)
            if conn.execute(
                "SELECT 1 FROM artists WHERE id=? COLLATE NOCASE", (item_id,)
            ).fetchone() is not None:
                return ("artist", item_id)
            if conn.execute(
                "SELECT 1 FROM playlists WHERE id=? COLLATE NOCASE", (item_id,)
            ).fetchone() is not None:
                return ("playlist", item_id)
            return None

        return self.db.read(query)

    def _navigate_deep_link(self, resolved):
        """Push the resolved deep-link page (main thread; from run_async).

        ``resolved`` is the ``(kind, nav_id)`` tuple from
        ``_classify_deep_link_id`` or ``None`` for a not-in-library id. Reuses
        the existing win.push-*-page actions rather than building pages by hand.
        """
        if not resolved:
            utils.send_toast(_("Item not in your library"), 3)
            return
        kind, nav_id = resolved
        action = {
            "album": "win.push-album-page",
            "artist": "win.push-artist-page",
            "playlist": "win.push-playlist-page",
        }.get(kind)
        if action is None or not nav_id:
            utils.send_toast(_("Item not in your library"), 3)
            return
        self.activate_action(action, GLib.Variant("s", nav_id))

    # ------------------------------------------------------------------ #
    # Home refresh (F5 / win.refresh-home)                               #
    # ------------------------------------------------------------------ #

    def on_refresh_home(self, _action, _parameter):
        """Re-run an incremental sync on a worker, then rebuild the Home page.

        Guarded against reentrancy: spamming F5 while a refresh is in flight
        would otherwise launch overlapping incremental syncs. ``_refreshing`` is
        set on entry, early-returns subsequent calls, and is cleared in both the
        success (``on_done``) and failure (``on_error``) callbacks.
        """
        if not self.is_logged_in or self.client is None:
            return
        if self._refreshing:
            return
        self._refreshing = True
        library_ids = list(self.settings.get_strv("selected-libraries"))

        def work():
            if library_ids:
                self._run_incremental_sync(self.client, library_ids)
            return True

        def done(_r):
            self._refreshing = False
            self._show_library_status()

        def error(_exc):
            self._refreshing = False

        utils.run_async(work, on_done=done, on_error=error, owner=self)

    # ------------------------------------------------------------------ #
    # Search shortcut (Ctrl+F / win.focus-search)                        #
    # ------------------------------------------------------------------ #

    def on_focus_search(self, _action, _parameter):
        """Show the Explore page and focus its search entry.

        Reuses the Explore button handler so the page is built/restored exactly
        as a click would; then asks the visible Explore page to focus its search
        entry (a no-op on any other page).
        """
        if not self.is_logged_in:
            return
        self.on_explore_button_clicked_func(None)
        page = self.navigation_view.find_page("explore")
        focus = getattr(page, "focus_search", None) if page is not None else None
        if callable(focus):
            GLib.idle_add(focus)

    def _on_space_play_pause(self, _widget, _args) -> bool:
        """Bubble-phase Space shortcut callback → toggle play/pause.

        Returns True to mark the key handled. Because the controller runs at the
        bubble phase, a focused editable (Gtk.Text/SearchEntry) has already
        consumed Space before this is reached, so reaching here means nothing
        editable was focused and toggling transport is the correct behavior.
        """
        self.player_pane.on_play_pause(None)
        return True

    # ------------------------------------------------------------------ #
    # Queue tab refresh                                                  #
    # ------------------------------------------------------------------ #

    def _on_queue_changed(self, *args):
        if self.queue_widget.get_mapped():
            self.queue_widget.update_all(self.player_object)
        # Queue emptied (cleared / last track removed): back to the idle art.
        if not self.player_object.queue.tracks:
            self.player_pane.show_placeholder_art()

    # ------------------------------------------------------------------ #
    # Queue-low AI radio extender                                        #
    # ------------------------------------------------------------------ #

    def begin_radio_session(self, seed_id, served_ids):
        """Record a newly-started radio so the queue-low extender can run.

        Called by the player pane when a track radio begins. ``seed_id`` is the
        user's chosen seed; ``served_ids`` are the ids of the first batch.
        """
        self._radio_session.begin(seed_id, served_ids)

    def _on_song_changed_for_radio(self, *args):
        """On each song change, extend an active radio when the queue runs low.

        If the now-playing track is not part of the active radio lineage, the
        user started something else — clear the session. Otherwise, when fewer
        than the threshold of tracks remain, kick a worker ``extend_radio`` and
        append the results (re-entrancy guarded by the session).
        """
        session = self._radio_session
        if not session.active:
            return
        track = self.player_object.playing_track
        track_id = getattr(track, "id", None) if track is not None else None
        # A track outside the radio lineage means the user navigated away to a
        # different playlist/album — stop extending this radio.
        if track_id is not None and track_id not in session.served_ids:
            session.clear()
            return

        tracks = self.player_object.queue.tracks
        idx = self.player_object.queue.current_index
        remaining = max(0, len(tracks) - 1 - idx)
        if not session.should_extend(remaining):
            return
        if utils.discovery is None:
            return

        session.mark_extending()
        seed_id = session.seed_id
        exclude = set(session.served_ids)

        def work():
            return utils.discovery.extend_radio(seed_id, exclude_ids=exclude)

        def done(result):
            new_tracks = result.get("tracks") if isinstance(result, dict) else []
            new_ids = []
            for t in new_tracks or []:
                tid = getattr(t, "id", None)
                if tid and tid not in session.served_ids:
                    self.player_object.queue.append(t)
                    new_ids.append(tid)
            session.finish_extending(new_ids)

        def on_error(_exc):
            session.finish_extending([])

        utils.run_async(work, on_done=done, on_error=on_error, owner=self)

    # ------------------------------------------------------------------ #
    # Background audio (hold/release + hide-on-close)                   #
    # ------------------------------------------------------------------ #

    def _on_playing_for_background(self, *args):
        app = self.get_application()
        if app is None:
            return
        if self.player_object.playing and not self._held:
            app.hold()
            self._held = True
        elif not self.player_object.playing and self._held:
            app.release()
            self._held = False

    def do_unrealize(self):
        # Once the window is torn down, gate any in-flight run_async callbacks.
        self._alive = False
        Adw.ApplicationWindow.do_unrealize(self)

    def do_close_request(self, *args):
        # Background-play: hide instead of quit while music is playing.
        if (
            self.settings.get_boolean("background-play")
            and self.player_object.playing
        ):
            self.set_visible(False)
            return True  # stop the default close (don't quit)
        return False

    # ------------------------------------------------------------------ #
    # Template callbacks — player pane (delegated to JTPlayerPane)       #
    # ------------------------------------------------------------------ #
    # These remain on the window because Gtk.Template.Callback handlers must
    # be methods of the template class; each forwards to the pane controller.

    @Gtk.Template.Callback("on_play_button_clicked")
    def on_play_button_clicked(self, btn):
        self.player_pane.on_play_pause(btn)

    @Gtk.Template.Callback("on_skip_forward_button_clicked")
    def on_skip_forward_button_clicked_func(self, widget):
        self.player_pane.on_skip_forward(widget)

    @Gtk.Template.Callback("on_skip_backward_button_clicked")
    def on_skip_backward_button_clicked_func(self, widget):
        self.player_pane.on_skip_backward(widget)

    @Gtk.Template.Callback("on_shuffle_button_toggled")
    def on_shuffle_button_toggled(self, btn):
        self.player_pane.on_shuffle_toggled(btn)

    @Gtk.Template.Callback("on_repeat_clicked")
    def on_repeat_clicked(self, *args):
        self.player_pane.on_repeat_clicked(*args)

    @Gtk.Template.Callback("on_volume_changed")
    def on_volume_changed_func(self, widget, value):
        self.player_pane.on_volume_changed(widget, value)

    @Gtk.Template.Callback("on_slider_seek")
    def on_slider_seek(self, *args):
        self.player_pane.on_slider_seek(*args)

    @Gtk.Template.Callback("on_seek_from_lyrics")
    def on_seek_from_lyrics(self, lyrics_widget, time_ms):
        self.player_pane.on_seek_from_lyrics(lyrics_widget, time_ms)

    @Gtk.Template.Callback("on_in_my_collection_button_clicked")
    def on_in_my_collection_button_clicked(self, btn):
        self.player_pane.on_favorite_clicked(btn)

    @Gtk.Template.Callback("on_share_clicked")
    def on_share_clicked(self, *args):
        self.player_pane.on_share_clicked(*args)

    def on_track_radio(self, *args):
        # win.push-track-radio-page action target.
        self.player_pane.on_track_radio(*args)

    @Gtk.Template.Callback("on_album_jump")
    def on_album_jump(self, *args):
        # album_button clicked -> jump to the playing track's album page.
        self.player_pane.on_album_jump(*args)

    # ------------------------------------------------------------------ #
    # Template callbacks — nav                                          #
    # ------------------------------------------------------------------ #

    @Gtk.Template.Callback("on_home_button_clicked")
    def on_home_button_clicked_func(self, widget):
        self.navigation_view.pop_to_tag("home")

    @Gtk.Template.Callback("on_explore_button_clicked")
    def on_explore_button_clicked_func(self, widget):
        if self.navigation_view.find_page("explore"):
            self.navigation_view.pop_to_tag("explore")
            return
        from .pages import HTExplorePage
        page = HTExplorePage()
        page.set_tag("explore")
        self.navigation_view.push(page.load())

    @Gtk.Template.Callback("on_collection_button_clicked")
    def on_collection_button_clicked_func(self, widget):
        if self.navigation_view.find_page("collection"):
            self.navigation_view.pop_to_tag("collection")
            return
        from .pages import HTCollectionPage
        page = HTCollectionPage()
        page.set_tag("collection")
        self.navigation_view.push(page.load())

    @Gtk.Template.Callback("on_queue_widget_mapped")
    def on_queue_widget_mapped(self, *args):
        self.queue_widget.update_all(self.player_object)

    @Gtk.Template.Callback("on_navigation_view_page_popped")
    def on_navigation_view_page_popped_func(self, nav_view, nav_page):
        if hasattr(nav_page, "disconnect_all"):
            nav_page.disconnect_all()

    @Gtk.Template.Callback("on_visible_page_changed")
    def on_visible_page_changed(self, nav_view, *args):
        visible = self.navigation_view.get_visible_page()
        if not visible:
            return
        match visible.get_tag():
            case "home":
                self.home_button.set_active(True)
            case "explore" | "search":
                self.explore_button.set_active(True)
            case "collection":
                self.collection_button.set_active(True)

    @Gtk.Template.Callback("on_sidebar_page_changed")
    def on_sidebar_page_changed(self, *args):
        if self.sidebar_stack.get_visible_child_name() == "player":
            self.playing_track_widget.set_visible(False)
        else:
            self.playing_track_widget.set_visible(True)

    # app_id_dialog callbacks — safe no-ops if dialog not shown
    @Gtk.Template.Callback("on_app_id_response_cb")
    def on_app_id_response_cb(self, dialog, response):
        dialog.close()

    @Gtk.Template.Callback("on_app_id_check_toggled_cb")
    def on_app_id_check_toggled_cb(self, check_btn):
        pass

    @Gtk.Template.Callback("on_app_id_closed_cb")
    def on_app_id_closed_cb(self, dialog):
        pass

    # ------------------------------------------------------------------ #
    # Audio sink helpers (called from preferences)                      #
    # ------------------------------------------------------------------ #

    def change_audio_sink(self, sink):
        if self.settings.get_int("preferred-sink") != sink:
            self.player_object.change_audio_sink(sink)
            self.settings.set_int("preferred-sink", sink)

    def change_alsa_device(self, device):
        self.player_object.alsa_device = device
        self.player_object.change_audio_sink(self.settings.get_int("preferred-sink"))

    def change_normalization(self, state):
        if self.player_object.normalize != state:
            self.player_object.normalize = state
            self.settings.set_boolean("normalize", state)
            self.player_object.change_audio_sink(
                self.settings.get_int("preferred-sink")
            )

    def change_quadratic_volume(self, state):
        if self.settings.get_boolean("quadratic-volume") != state:
            self.player_object.quadratic_volume = state
            self.settings.set_boolean("quadratic-volume", state)

    def logout(self):
        """Clear creds and return to onboarding (called from preferences)."""
        self.secret_store.clear()
        self.settings.set_string("server-url", "")
        self.client = None
        self.player_pane.client = None
        self.player_object._client = None
        self.is_logged_in = False
        self.navigation_buttons.set_sensitive(False)
        self.player_lyrics_queue.set_sensitive(False)
        self._show_onboarding()

    # ------------------------------------------------------------------ #
    # Utility                                                            #
    # ------------------------------------------------------------------ #

    def create_action_with_target(
        self, name: str, target_type: GLib.VariantType, callback: Callable
    ):
        action = Gio.SimpleAction.new(name, target_type)
        action.connect("activate", callback)
        self.add_action(action)
        return action
