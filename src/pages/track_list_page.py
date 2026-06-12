# track_list_page.py
#
# Copyright 2023 p0ryae
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

from gettext import gettext as _

from gi.repository import Gtk

from ..lib import utils
from ..widgets.card_widget import item_get
from .page import Page


def _ticks_to_seconds(ticks):
    if not ticks:
        return 0
    return int(ticks // 10_000_000)


class TrackListPage(Page):
    """Base for album/playlist pages: header + action row + sorted track rows.

    Subclasses populate ``self.item`` (album/playlist dict) and ``self.tracks``
    (list of track dicts) in ``_load_async`` then call ``_setup_ui`` in
    ``_load_finish``. The header art, title, subtitles, Play/Shuffle/Favorite/
    Share controls, and the track list are built here.
    """

    __gtype_name__ = "TrackListPage"

    item = None
    tracks = []
    # favorite kind for the in-collection button ("album" | None).
    favorite_kind = None

    def _setup_ui(self, title, subtitle, tracks, hide_share=False):
        builder = Gtk.Builder.new_from_resource(
            "/io/github/tylerreece/timbre/ui/pages_ui/tracks_list_template.ui"
        )
        self._main = builder.get_object("_main")
        self.append(self._main)

        self.auto_load = builder.get_object("_auto_load")
        self.auto_load.item_type = "track"
        self.auto_load.set_scrolled_window(self.scrolled_window)
        self.auto_load.set_items(tracks)
        # The auto-load widget comes from the template (not Page.append), so it
        # is NOT auto-registered for teardown. Register it explicitly so the
        # page's disconnect_all recurses into it and disconnects every track
        # row's song-changed/notify::playing handler on the GLOBAL player.
        # Without this the player pins every HTGenericTrackWidget (+ its link
        # labels) for the life of the process — a slow leak the per-page
        # weakref gate misses because GTK holds the rows C-side, not via Python
        # referrers on the (collected) page.
        if self.auto_load not in self.disconnectables:
            self.disconnectables.append(self.auto_load)

        self.set_title(title)
        builder.get_object("_title_label").set_label(title)
        builder.get_object("_first_subtitle_label").set_label(subtitle)

        total_duration = sum(
            _ticks_to_seconds(item_get(t, "duration_ticks")) for t in tracks
        )
        builder.get_object("_second_subtitle_label").set_label(
            _("{} tracks ({})").format(
                len(tracks), utils.pretty_duration(total_duration)
            )
        )

        play_btn = builder.get_object("_play_button")
        shuffle_btn = builder.get_object("_shuffle_button")
        fav_btn = builder.get_object("_in_my_collection_button")
        share_btn = builder.get_object("_share_button")
        # Sort dropdown is not wired in Phase 5 (rows arrive pre-sorted); hide.
        builder.get_object("_sort_by_dropdown").set_visible(False)

        self.signals.extend([
            (play_btn, play_btn.connect("clicked", self.on_play_button_clicked)),
            (shuffle_btn,
             shuffle_btn.connect("clicked", self.on_shuffle_button_clicked)),
        ])

        if self.favorite_kind is not None:
            self._fav_button = fav_btn
            self._fav_state = bool(item_get(self.item, "is_favorite"))
            self._update_fav_icon(self._fav_state)
            self.signals.append(
                (fav_btn, fav_btn.connect("clicked", self._on_favorite_clicked))
            )
        else:
            fav_btn.set_visible(False)

        if hide_share:
            share_btn.set_visible(False)
        else:
            self.signals.append(
                (share_btn, share_btn.connect("clicked", self._on_share_clicked))
            )

        image = builder.get_object("_image")
        item_id = item_get(self.item, "id")
        tag = item_get(self.item, "image_tag")
        if item_id:
            utils.run_async(
                lambda: utils.add_image_from_tag(image, item_id, tag, 320),
                owner=self,
            )

    def disconnect_all(self, *args):
        """Tear down the page, then unparent the builder-created ``_main``
        subtree so GTK can finalize it.

        ``Page.disconnect_all`` recurses into ``self.auto_load`` (which unparents
        its own rows), but the ``tracks_list_template.ui`` ``_main`` tree —
        AdwBreakpointBin, the auto-load's content ScrolledWindow + ListBox, and
        its Spinner — stays parented in ``self.content``. Because the page keeps
        a Python ref to ``self.auto_load``, that C-side subtree is pinned for the
        life of the process (measured: album + playlist each leak one
        ScrolledWindow / ListBox / Spinner / Bin per push/pop). Removing ``_main``
        from ``self.content`` breaks the chain so the whole subtree disposes,
        mirroring how ``HTAutoLoadWidget.disconnect_all`` unparents its rows.
        """
        super().disconnect_all(*args)
        main = getattr(self, "_main", None)
        if main is not None and main.get_parent() is not None:
            self.content.remove(main)
        self._main = None

    # ------------------------------------------------------------------ #
    # Actions                                                            #
    # ------------------------------------------------------------------ #

    def on_play_button_clicked(self, btn):
        utils.player_object.play_this(self.tracks, 0)

    def on_shuffle_button_clicked(self, btn):
        utils.player_object.shuffle_this(self.tracks)

    def _update_fav_icon(self, state):
        self._fav_button.set_icon_name(
            "heart-filled-symbolic" if state else "heart-outline-thick-symbolic"
        )

    def _on_favorite_clicked(self, btn):
        item_id = item_get(self.item, "id")
        if item_id is None or utils.client is None:
            return
        # Optimistic flip, revert via on_applied on error.
        new_state = utils.toggle_favorite(
            self.favorite_kind, item_id, self._fav_state,
            owner=self, on_applied=self._fav_applied,
        )
        self._fav_state = new_state
        self._update_fav_icon(new_state)

    def _fav_applied(self, state):
        self._fav_state = bool(state)
        self._update_fav_icon(self._fav_state)

    def _on_share_clicked(self, *args):
        item_id = item_get(self.item, "id") or ""
        server_url = ""
        server_id = ""
        if utils.settings is not None:
            server_url = utils.settings.get_string("server-url")
        if utils.client is not None:
            server_id = getattr(utils.client, "server_id", "") or ""
        link = "%s/web/#/details?id=%s&serverId=%s" % (
            server_url, item_id, server_id
        )
        from gi.repository import Gdk
        Gdk.Display.get_default().get_clipboard().set(link)
        utils.send_toast(_("Link copied"), 2)
