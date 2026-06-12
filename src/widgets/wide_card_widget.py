# wide_card_widget.py
#
# Copyright (C) 2026 Tyler Reece
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Wide (compact, horizontal) browse card for the Home recents grid.

Small art on the left, title + subtitle on the right (the design mockup's
"recents" tiles). Consumes the same Phase 5 browse-item convention as
``HTCardWidget`` and dispatches the same navigation actions on click. Used only
for albums/playlists in the Home recents grid, so it has no track-play path.
"""

import logging
from gettext import gettext as _

from gi.repository import GLib, Gtk

from ..disconnectable_iface import IDisconnectable
from ..lib import utils
from ..lib.browse_item import KIND_ACTION, item_get, item_kind

logger = logging.getLogger(__name__)


@Gtk.Template(
    resource_path="/io/github/tylerreece/timbre/ui/widgets/wide_card_widget.ui"
)
class HTWideCardWidget(Gtk.Box, IDisconnectable):
    """Compact horizontal card: small art + title/subtitle, click to navigate."""

    __gtype_name__ = "HTWideCardWidget"

    image = Gtk.Template.Child()
    click_gesture = Gtk.Template.Child()
    title_label = Gtk.Template.Child()
    detail_label = Gtk.Template.Child()

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

        self._populate()

    def _populate(self):
        title = item_get(self.item, "name") or ""
        self.title_label.set_label(title)
        self.title_label.set_tooltip_text(title)

        if self.kind == "playlist":
            n = item_get(self.item, "track_count")
            self.detail_label.set_label(
                _("{} tracks").format(n) if n is not None else _("Playlist")
            )
        elif self.kind == "album":
            self.detail_label.set_label(
                item_get(self.item, "album_artist_name") or _("Album")
            )
        else:
            self.detail_label.set_visible(False)

        item_id = item_get(self.item, "id")
        tag = item_get(self.item, "image_tag")
        if item_id:
            utils.run_async(
                lambda: utils.add_image_from_tag(self.image, item_id, tag, 160),
                pool=True,
            )

    def _on_click(self, *args) -> None:
        if self.action:
            item_id = item_get(self.item, "id") or ""
            self.activate_action(self.action, GLib.Variant("s", str(item_id)))
