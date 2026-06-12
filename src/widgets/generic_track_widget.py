# generic_track_widget.py
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

import html
import logging
from gettext import gettext as _

from gi.repository import Gdk, Gio, GLib, GObject, Gtk

from ..disconnectable_iface import IDisconnectable
from ..lib import utils
from .card_widget import item_get

logger = logging.getLogger(__name__)


def _ticks_to_seconds(ticks):
    """Jellyfin RunTimeTicks (100ns units) -> integer seconds."""
    if not ticks:
        return 0
    return int(ticks // 10_000_000)


@Gtk.Template(
    resource_path="/io/github/tylerreece/timbre/ui/widgets/generic_track_widget.ui"
)
class HTGenericTrackWidget(Gtk.ListBoxRow, IDisconnectable):
    """A full track row: art, title, artist link, duration, 3-dot menu.

    Consumes a Phase 5 browse *track* item (db row dict or ``Track`` model).
    Menu: Play next, Add to queue, Go to album, Go to artist, Favorite toggle,
    Share. Artist/album labels are clickable links that push the artist/album
    page via window actions.
    """

    __gtype_name__ = "HTGenericTrackWidget"

    image = Gtk.Template.Child()
    now_playing_indicator = Gtk.Template.Child()
    album_stack = Gtk.Template.Child()
    track_title_label = Gtk.Template.Child()
    track_duration_label = Gtk.Template.Child()
    playlists_submenu = Gtk.Template.Child()
    _grid = Gtk.Template.Child()
    explicit_label = Gtk.Template.Child()

    artist_label = Gtk.Template.Child()
    artist_label_2 = Gtk.Template.Child()
    track_album_label = Gtk.Template.Child()

    menu_button = Gtk.Template.Child()
    track_menu = Gtk.Template.Child()

    index = GObject.Property(type=int, default=0)

    def __init__(self, track):
        IDisconnectable.__init__(self)
        super().__init__()

        self.menu_activated = False
        self.track = track

        self.track_id = item_get(track, "id")
        self.album_id = item_get(track, "album_id")
        self.artist_id = item_get(track, "artist_id")
        self.is_favorite = bool(item_get(track, "is_favorite"))

        name = item_get(track, "name") or _("Unknown")
        artist_name = item_get(track, "artist_name") or ""
        album_name = item_get(track, "album_name") or ""

        self.track_title_label.set_label(name)

        # Clickable artist / album links -> push pages via win actions.
        self._set_link(self.artist_label, "artist", self.artist_id, artist_name)
        self._set_link(self.artist_label_2, "artist", self.artist_id, artist_name)
        self._set_link(self.track_album_label, "album", self.album_id, album_name)

        for label in (self.artist_label, self.artist_label_2,
                      self.track_album_label):
            self.signals.append(
                (label, label.connect("activate-link", self._on_link))
            )

        self.explicit_label.set_visible(False)

        duration = _ticks_to_seconds(item_get(track, "duration_ticks"))
        self.track_duration_label.set_label(utils.pretty_duration(duration))

        self.signals.append((
            self.menu_button,
            self.menu_button.connect("notify::active", self._on_menu_activate),
        ))

        self._update_now_playing()
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

        if self.album_id:
            utils.run_async(
                lambda: utils.add_image_from_tag(self.image, self.album_id, None, 80),
                pool=True,
            )

        self.action_group = Gio.SimpleActionGroup()
        self.insert_action_group("trackwidget", self.action_group)

    # ------------------------------------------------------------------ #
    # Link labels                                                        #
    # ------------------------------------------------------------------ #

    def _set_link(self, label, kind, item_id, text):
        if not text:
            label.set_visible(False)
            return
        if item_id:
            label.set_markup(
                "<a href='%s:%s'>%s</a>" % (kind, item_id, html.escape(text))
            )
        else:
            label.set_text(text)

    def _on_link(self, label, uri):
        # uri form "artist:<id>" / "album:<id>".
        kind, _, item_id = uri.partition(":")
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
        is_now = cur_id is not None and self.track_id == cur_id
        if is_now and utils.player_object.playing:
            self.album_stack.set_visible_child(self.now_playing_indicator)
        else:
            self.album_stack.set_visible_child(self.image)

    # ------------------------------------------------------------------ #
    # Context menu                                                       #
    # ------------------------------------------------------------------ #

    def _on_menu_activate(self, menu_button, *args):
        # Repopulate the playlists submenu every time the menu opens so a
        # playlist created since the last open shows up. The static items +
        # actions are installed exactly once (menu_activated guard) — only the
        # dynamic playlist submenu is rebuilt each open.
        if menu_button.get_active():
            self._refresh_playlists_submenu()

        if self.menu_activated:
            return
        self.menu_activated = True

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

        # "Add to a playlist" -> a playlist id (the "s" variant target carries
        # which playlist). One parametrized action backs every dynamic submenu
        # item; the id arrives as the activation parameter.
        add_action = Gio.SimpleAction.new(
            "add-to-playlist", GLib.VariantType.new("s")
        )
        self.signals.append((
            add_action, add_action.connect("activate", self._on_add_to_playlist)
        ))
        self.action_group.add_action(add_action)

    def _refresh_playlists_submenu(self):
        """Repopulate the playlists submenu from the LOCAL db, off the main
        thread.

        The db read runs via run_async (owner-guarded) so the main loop is
        never blocked — CLAUDE.md forbids db access on the main thread. The
        submenu is rebuilt on the idle callback once the (tiny, indexed)
        playlist list lands. The "New playlist…" entry is always first; each
        existing playlist becomes a Gio.MenuItem whose action target is the
        playlist id (built in python, never hardcoded in the .blp).
        """
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
        """Main-loop: rebuild the playlists submenu from a playlist-dict list."""
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
        # Cache id->name so the action handler can name the toast without
        # another db read.
        self._playlist_names = {
            pl.get("id"): (pl.get("name") or _("Unknown")) for pl in playlists
        }

    def _play_next(self, *args):
        utils.player_object.add_next(self.track)

    def _add_to_queue(self, *args):
        utils.player_object.add_to_queue(self.track)

    def _on_add_to_playlist(self, _action, target):
        """Add this track to the playlist named by the "s" target variant."""
        if target is None:
            return
        playlist_id = target.get_string()
        name = getattr(self, "_playlist_names", {}).get(
            playlist_id, _("playlist")
        )
        utils.add_track_to_playlist(
            playlist_id, name, self.track_id, owner=self.get_root()
        )

    def _on_new_playlist(self, *args):
        """Open the new-playlist dialog; on confirm, create it WITH this track."""
        from ..new_playlist import NewPlaylistWindow

        dialog = NewPlaylistWindow()

        def on_create(_dlg, title, _description):
            title = (title or "").strip()
            if not title:
                return
            utils.create_playlist_with_track(
                title, self.track_id, owner=self.get_root()
            )

        # The dialog is short-lived and self-owned (Adw.Dialog presented on the
        # root); its create-playlist handler is disconnected when the dialog is
        # finalized after close, so it is not tracked in self.signals (which is
        # for the long-lived row's own signals).
        dialog.connect("create-playlist", on_create)
        dialog.connect("create-playlist", lambda *a: dialog.close())
        root = self.get_root()
        dialog.present(root)

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
        clipboard = Gdk.Display.get_default().get_clipboard()
        clipboard.set(link)
        utils.send_toast(_("Link copied"), 2)
