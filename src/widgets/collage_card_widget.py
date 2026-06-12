# collage_card_widget.py
#
# Copyright (C) 2026 Tyler Reece
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""A generic clickable tile with a 2x2 album-art collage + title + subtitle.

Reused across the Home page wherever an item (a listening-history month, an AI
daily mix, a personal radio station, an artist radio) wants a square collage of
album covers with a bold name and a one-line caption below.

Shows a 2x2 collage of album art (like Jellyfin's auto-playlist covers) built
from the supplied albums; clicking emits ``activated`` (no args) which the owner
handles via its tracked ``signals`` list. When no supplied album carries art the
collage stays hidden and the card falls back to text-only, never an empty gray
square.

The collage sizing structure is hard-clamped to a 1:1 160px square — see the
blueprint comments.

This is a plain (non-subclassable) ``@Gtk.Template`` widget — PyGObject forbids
inheriting from a template-decorated class, so the listening-history month tiles
build this generic card directly (with month-formatted labels from
``home_rows``) rather than subclassing it.
"""

import logging

from gi.repository import GObject, Gtk

from ..disconnectable_iface import IDisconnectable
from ..lib import home_rows, utils

logger = logging.getLogger(__name__)


@Gtk.Template(
    resource_path="/io/github/tylerreece/timbre/ui/widgets/collage_card_widget.ui"
)
class HTCollageCardWidget(Gtk.Box, IDisconnectable):
    """A clickable collage card: a 2x2 album-art collage + bold title + caption.

    Emits an ``activated`` signal on click rather than holding an owner-supplied
    callback. A bound method stored on the card would create a GTK-held (card)
    -> Python (bound method) -> owner reference that gc cannot break while the
    card is in the live tree, leaking the owner on pop. The owner connects to
    this signal via its tracked ``signals`` list, which ``disconnect_all``
    severs on teardown.

    Album-cover loads run on the shared image pool, owner-guarded on ``self`` so
    a late fetch after the card is gone is dropped (no use-after-free).
    """

    __gtype_name__ = "HTCollageCardWidget"

    __gsignals__ = {
        "activated": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    title_label = Gtk.Template.Child()
    subtitle_label = Gtk.Template.Child()
    click_gesture = Gtk.Template.Child()
    collage_frame = Gtk.Template.Child()
    cell_0 = Gtk.Template.Child()
    cell_1 = Gtk.Template.Child()
    cell_2 = Gtk.Template.Child()
    cell_3 = Gtk.Template.Child()

    def __init__(self, title="", subtitle="", albums=None) -> None:
        IDisconnectable.__init__(self)
        super().__init__()

        self.title_label.set_label(title or "")
        self.subtitle_label.set_label(subtitle or "")
        self.subtitle_label.set_visible(bool(subtitle))

        self.signals.append((
            self.click_gesture,
            self.click_gesture.connect("released", self._on_click),
        ))

        self._build_collage(albums or [])

    def _build_collage(self, albums) -> None:
        """Fill the 2x2 grid from the item's albums (those that have art).

        Only albums carrying an ``image_tag`` can yield a cover; per the fill
        rules ``home_rows.collage_slots`` cycles 1-3 covers to fill the four
        cells, uses the first four when there are 4+, and returns nothing when
        none have art — in which case the collage stays hidden (text-only card).
        """
        cells = (self.cell_0, self.cell_1, self.cell_2, self.cell_3)
        covers = [a for a in albums if a.get("id") and a.get("image_tag")]
        slots = home_rows.collage_slots(covers, n=len(cells))
        if not slots:
            return  # no art -> keep the text-only card (collage stays hidden)

        self.collage_frame.set_visible(True)
        for cell, album in zip(cells, slots):
            self._load_cover(cell, album)

    def _load_cover(self, picture, album) -> None:
        """Load one album cover into a Gtk.Picture cell via the image pool.

        Owner-guarded on ``self``: if the card is torn down before the fetch
        lands, the marshalled mutation is dropped (the pool job itself is cheap
        and just returns).
        """
        utils.run_async(
            lambda: utils.add_picture_from_tag(
                picture, album["id"], album.get("image_tag"), 80
            ),
            pool=True,
            owner=self,
        )

    def _on_click(self, *args) -> None:
        self.emit("activated")
