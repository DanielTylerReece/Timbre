# artist_label_widget.py
#
# Copyright 2024 Nokse
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
from typing import Any, List

from gi.repository import Gtk


class HTLinkLabelWidget(Gtk.Label):
    """Displays multiple artists or an album as markup links.

    Phase 0: tidalapi.Artist / tidalapi.Album replaced with duck-typed Any.
    Jellyfin data objects wired in Phase 1.
    """

    __gtype_name__ = "HTLinkLabelWidget"

    def __init__(self) -> None:
        super().__init__()

        self.xalign = 0.0
        self.add_css_class("artist-link")

    def set_artists(self, artists: List[Any]) -> None:
        """Set artists. Each artist must have .id and .name attributes."""
        if not isinstance(artists, list) or not artists:
            return

        label: str = ""
        for index, artist in enumerate(artists):
            if index >= 1:
                label += ", "
            artist_id = getattr(artist, "id", "")
            artist_name = getattr(artist, "name", str(artist))
            label += "<a href='artist:{}'>{}</a>".format(
                artist_id, html.escape(artist_name)
            )
        self.set_markup(label)

    def set_album(self, album: Any) -> None:
        """Set album. Must have .id and .name attributes."""
        album_id = getattr(album, "id", "")
        album_name = getattr(album, "name", str(album))
        label: str = f"""<a href="album:{album_id}">{html.escape(album_name)}</a>"""
        self.set_markup(label)
