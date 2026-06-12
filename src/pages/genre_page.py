# genre_page.py
#
# Copyright (C) 2026 Tyler Reece
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Genre detail page: top tracks + albums for one genre.

All data is SQLite (``genre_top_tracks`` / ``genre_albums``). Sections hide when
empty (consistent with Home). The genre name is carried on ``self.id`` via the
standard ``new_from_id`` factory.
"""

from gettext import gettext as _

from ..lib import utils
from .page import Page

# Top tracks previewed before "More".
_TOP_TRACKS = 10
# Albums previewed in the carousel.
_PREVIEW = 16


class HTGenrePage(Page):
    """Genre detail: Top tracks list + Albums carousel for ``self.id``."""

    __gtype_name__ = "HTGenrePage"

    def _load_async(self) -> None:
        db = utils.db
        self.genre = self.id or ""
        self.top_tracks = db.genre_top_tracks(self.genre, _TOP_TRACKS)
        self.albums = db.genre_albums(self.genre, _PREVIEW)

    def _load_finish(self) -> None:
        self.set_title(self.genre or _("Genre"))

        db = utils.db
        genre = self.genre

        self.new_track_list_for(
            _("Top tracks"), self.top_tracks,
            more_function=lambda offset, limit: db.genre_top_tracks(
                genre, limit, offset=offset
            ),
        )
        self.new_carousel_for(
            _("Albums"), self.albums,
            more_function=lambda offset, limit: db.genre_albums(
                genre, limit, offset=offset
            ),
        )
