# collection_page.py
#
# Copyright 2024 Nokse22
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

from ..lib import utils
from .page import Page

# Cards rendered inline per collection carousel before "More".
_PREVIEW = 16


class HTCollectionPage(Page):
    """The user's library: Playlists / Albums / Tracks / Artists carousels.

    All data is read from SQLite. Each carousel's More button pushes a
    from-function page driven by the matching paged db helper.
    """

    __gtype_name__ = "HTCollectionPage"

    def _load_async(self) -> None:
        db = utils.db
        self.playlists = db.playlists_page(0, _PREVIEW)
        self.albums = db.albums_page(0, _PREVIEW)
        self.tracks = db.tracks_page(0, _PREVIEW)
        self.artists = db.artists_page(0, _PREVIEW)

    def _load_finish(self) -> None:
        self.set_tag("collection")
        self.set_title(_("Collection"))

        db = utils.db
        self.new_carousel_for(
            _("Playlists"), self.playlists,
            more_function=lambda offset, limit: db.playlists_page(offset, limit),
        )
        # Albums + Artists "More" pages get the Phase 7 year-filter dropdown.
        self.new_carousel_for(
            _("Albums"), self.albums,
            year_function=lambda year, offset, limit: db.albums_page(
                offset, limit, year=year
            ),
        )
        self.new_carousel_for(
            _("Tracks"), self.tracks,
            more_function=lambda offset, limit: db.tracks_page(offset, limit),
            item_type="track",
        )
        self.new_carousel_for(
            _("Artists"), self.artists,
            year_function=lambda year, offset, limit: db.artists_page(
                offset, limit, year=year
            ),
        )
