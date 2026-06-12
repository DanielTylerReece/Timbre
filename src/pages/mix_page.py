# mix_page.py
#
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

"""Album-style detail page for an AI Custom mix / Personal radio station.

Clicking a mix/radio collage card on Home used to open a bare track list; this
page gives those the same album-page chrome: the 2x2 album-art collage as the
cover (the same art the Home card shows), the mix name as the title, the short
AI description below it, the "N tracks (H:MM:SS)" total, and Play / Shuffle /
Share controls — with the track rows beneath, exactly as before.

Lifecycle mirrors ``Page``: ``_load_async`` resolves the mix's tracks + collage
albums from SQLite only (never the network — the ids are already known), and
``_load_finish`` builds the widgets. Every cover load is owner-guarded on the
page so a late fetch after pop is dropped.
"""

import logging
from gettext import gettext as _

from gi.repository import Gtk

from ..lib import home_rows, utils
from ..widgets.card_widget import item_get
from .page import Page

logger = logging.getLogger(__name__)

_PLACEHOLDER_RESOURCE = "/io/github/tylerreece/timbre/record-placeholder.svg"


def _ticks_to_seconds(ticks):
    if not ticks:
        return 0
    return int(ticks // 10_000_000)


class HTMixPage(Page):
    """Album-style detail page for an AI mix / personal radio station."""

    __gtype_name__ = "HTMixPage"

    def __init__(self, name, description, track_ids):
        super().__init__()
        self._mix_name = name or _("Mix")
        self._mix_description = description or ""
        self._track_ids = list(track_ids or [])

    def _load_async(self) -> None:
        # SQLite only — the track ids are already known (no network resolve).
        db = utils.db
        self.tracks = db.tracks_by_ids(self._track_ids)
        # The same album set the Home card's collage shows: the albums these
        # tracks belong to, ranked by membership, capped to the four cells.
        self.collage_albums = db.albums_for_tracks(self._track_ids, 4)

    def _load_finish(self) -> None:
        builder = Gtk.Builder.new_from_resource(
            "/io/github/tylerreece/timbre/ui/pages_ui/mix_page_template.ui"
        )
        self._main = builder.get_object("_main")
        self.append(self._main)

        self.auto_load = builder.get_object("_auto_load")
        self.auto_load.item_type = "track"
        self.auto_load.set_scrolled_window(self.scrolled_window)
        self.auto_load.set_items(self.tracks)
        # The auto-load widget comes from the template (not Page.append), so it
        # is NOT auto-registered for teardown. Register it explicitly so the
        # page's disconnect_all recurses into it and disconnects every track
        # row's song-changed/notify::playing handler on the GLOBAL player —
        # same hygiene as TrackListPage.
        if self.auto_load not in self.disconnectables:
            self.disconnectables.append(self.auto_load)

        self.set_title(self._mix_name)
        builder.get_object("_title_label").set_label(self._mix_name)

        desc_label = builder.get_object("_first_subtitle_label")
        desc_label.set_label(self._mix_description)
        desc_label.set_visible(bool(self._mix_description))

        total_duration = sum(
            _ticks_to_seconds(item_get(t, "duration_ticks")) for t in self.tracks
        )
        builder.get_object("_second_subtitle_label").set_label(
            _("{} tracks ({})").format(
                len(self.tracks), utils.pretty_duration(total_duration)
            )
        )

        play_btn = builder.get_object("_play_button")
        shuffle_btn = builder.get_object("_shuffle_button")
        share_btn = builder.get_object("_share_button")
        self.signals.extend([
            (play_btn, play_btn.connect("clicked", self._on_play_clicked)),
            (shuffle_btn,
             shuffle_btn.connect("clicked", self._on_shuffle_clicked)),
            (share_btn, share_btn.connect("clicked", self._on_share_clicked)),
        ])

        self._build_collage(builder)

    # ------------------------------------------------------------------ #
    # Cover collage                                                      #
    # ------------------------------------------------------------------ #

    def _build_collage(self, builder) -> None:
        """Fill the header's 2x2 collage from the mix's albums (those with art).

        Reuses the PROVEN home-card collage structure (Clamp > AspectFrame 1:1 >
        homogeneous 80px cell grid). ``home_rows.collage_slots`` cycles 1-3
        covers to fill the four cells and returns the first four when there are
        4+. When NO album carries art the collage stays hidden and we fall back
        to the single record-placeholder Picture, so the header never shows an
        empty gray square.
        """
        frame = builder.get_object("collage_frame")
        cells = [builder.get_object(f"cell_{i}") for i in range(4)]
        covers = [
            a for a in self.collage_albums
            if a.get("id") and a.get("image_tag")
        ]
        slots = home_rows.collage_slots(covers, n=len(cells))
        if not slots:
            # No art -> single placeholder Picture filling the (now-shown) frame.
            frame.set_visible(True)
            cells[0].set_resource(_PLACEHOLDER_RESOURCE)
            for cell in cells[1:]:
                cell.set_visible(False)
            return

        frame.set_visible(True)
        for cell, album in zip(cells, slots):
            self._load_cover(cell, album)

    def _load_cover(self, picture, album) -> None:
        """Load one album cover into a cell via the shared image pool.

        Owner-guarded on ``self``: a fetch that lands after the page pops is
        dropped (no use-after-free into a finalized widget).
        """
        utils.run_async(
            lambda: utils.add_picture_from_tag(
                picture, album["id"], album.get("image_tag"), 80
            ),
            pool=True,
            owner=self,
        )

    # ------------------------------------------------------------------ #
    # Actions                                                            #
    # ------------------------------------------------------------------ #

    def _on_play_clicked(self, _btn) -> None:
        if self.tracks:
            utils.player_object.play_this(self.tracks, 0)

    def _on_shuffle_clicked(self, _btn) -> None:
        if self.tracks:
            utils.player_object.shuffle_this(self.tracks)

    def _on_share_clicked(self, *args) -> None:
        # AI mixes have NO Jellyfin URL (they're a model-generated track set,
        # not a server entity), so "Share" can't copy a deep link the way the
        # album/playlist page does. We copy a human-readable text listing
        # instead: the mix name, then numbered "Artist — Title" lines.
        lines = [self._mix_name]
        for i, t in enumerate(self.tracks, start=1):
            artist = item_get(t, "artist_name") or _("Unknown artist")
            title = item_get(t, "name") or _("Unknown track")
            lines.append("{}. {} — {}".format(i, artist, title))
        text = "\n".join(lines)

        from gi.repository import Gdk
        Gdk.Display.get_default().get_clipboard().set(text)
        utils.send_toast(_("Mix copied to clipboard"), 2)

    # ------------------------------------------------------------------ #
    # Teardown                                                           #
    # ------------------------------------------------------------------ #

    def disconnect_all(self, *args):
        """Tear down, then unparent the builder-created ``_main`` subtree.

        Same reasoning as ``TrackListPage.disconnect_all``: the page keeps a
        Python ref to ``self.auto_load``, which pins the template's ``_main``
        subtree (BreakpointBin + the auto-load's ScrolledWindow/ListBox/Spinner)
        in ``self.content`` for the life of the process. Removing ``_main``
        breaks the chain so the whole subtree disposes.
        """
        super().disconnect_all(*args)
        main = getattr(self, "_main", None)
        if main is not None and main.get_parent() is not None:
            self.content.remove(main)
        self._main = None
