# card_widget.py
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

"""Polymorphic browse card.

ITEM CONVENTION (Phase 5 — used by the whole widget kit)
========================================================
Every browse widget (cards, carousels, track rows, auto-load) consumes a
*browse item* which is EITHER:

  * a **db row dict** tagged with a ``kind`` key — this is what the
    ``Database`` paged helpers return (``albums_page``, ``album_tracks``, …).
    ``kind`` is one of ``"album" | "artist" | "playlist" | "track" | "genre"``;
    field names match the SQLite columns (``id``, ``name``, ``image_tag``,
    ``album_artist_name``, ``artist_name``, ``album_id``, ``duration_ticks``,
    ``is_favorite`` …).  OR
  * a **model dataclass** (``Track`` / ``Album`` / ``Artist`` / ``Playlist`` /
    ``Genre`` from ``lib.jellyfin.models``); its ``kind`` is derived from the
    class name.

The kit NEVER uses ``isinstance`` on tidalapi types. It dispatches on
``item_kind(item)`` and reads fields with ``item_get(item, attr, default)`` —
both work uniformly on dicts and dataclasses. Pages pass db dicts straight
through (no conversion layer); the dataclasses are accepted for the few call
sites that already hold a model (e.g. the player's track-radio Track list).
"""

import logging
from gettext import gettext as _

from gi.repository import Adw, GLib, Gtk

from ..disconnectable_iface import IDisconnectable
from ..lib import utils
# Re-exported so widgets can `from .card_widget import item_kind, item_get`.
from ..lib.browse_item import KIND_ACTION, item_get, item_kind  # noqa: F401

logger = logging.getLogger(__name__)


@Gtk.Template(resource_path="/io/github/tylerreece/timbre/ui/widgets/card_widget.ui")
class HTCardWidget(Adw.BreakpointBin, IDisconnectable):
    """A card displaying one browse item (album/artist/playlist/track/genre).

    Configures itself from ``item_kind(item)``: non-track kinds get a navigation
    action (clicking pushes the detail page by id); tracks play on click.
    Artists render with a rounded-square image style per the design.
    """

    __gtype_name__ = "HTCardWidget"

    image = Gtk.Template.Child()
    click_gesture = Gtk.Template.Child()
    title_label = Gtk.Template.Child()
    detail_label = Gtk.Template.Child()
    track_artist_label = Gtk.Template.Child()

    def __init__(self, item) -> None:
        IDisconnectable.__init__(self)
        super().__init__()

        self.item = item
        self.kind = item_kind(item)
        self.action = KIND_ACTION.get(self.kind)

        self.signals.append((
            self.click_gesture,
            self.click_gesture.connect("released", self._on_click),
        ))

        # track_artist_label is only used for tracks; hide by default.
        self.track_artist_label.set_visible(False)

        self._populate()

    def _populate(self):
        title = item_get(self.item, "name") or ""
        self.title_label.set_label(title)
        self.title_label.set_tooltip_text(title)

        if self.kind == "track":
            self.detail_label.set_label(
                _("Track by {}").format(item_get(self.item, "artist_name") or "")
            )
        elif self.kind == "album":
            self.detail_label.set_label(
                item_get(self.item, "album_artist_name") or _("Album")
            )
        elif self.kind == "artist":
            self.detail_label.set_label(_("Artist"))
            # Artists get the rounded-square image treatment.
            self.image.add_css_class("rounded-image")
        elif self.kind == "playlist":
            n = item_get(self.item, "track_count")
            self.detail_label.set_label(
                _("{} tracks").format(n) if n is not None else _("Playlist")
            )
        elif self.kind == "genre":
            self.detail_label.set_label(_("Genre"))
        else:
            self.detail_label.set_visible(False)

        self._load_image()

    def _load_image(self):
        # For a track the art lives on its album; everything else carries its
        # own image_tag/id. Genres have no image.
        if self.kind == "genre":
            return
        if self.kind == "track":
            item_id = item_get(self.item, "album_id") or item_get(self.item, "id")
            tag = None
        else:
            item_id = item_get(self.item, "id")
            tag = item_get(self.item, "image_tag")
        if not item_id:
            return
        utils.run_async(
            lambda: utils.add_image_from_tag(self.image, item_id, tag, 320),
            pool=True,
        )

    def _on_click(self, *args) -> None:
        if self.action:
            item_id = item_get(self.item, "id") or ""
            self.activate_action(self.action, GLib.Variant("s", str(item_id)))
        elif self.kind == "track":
            utils.player_object.play_this([self.item], 0)
