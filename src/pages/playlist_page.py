# playlist_page.py
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

from gettext import gettext as _

from ..lib import utils
from ..widgets.card_widget import item_get
from .track_list_page import TrackListPage


class HTPlaylistPage(TrackListPage):
    """Playlist detail page: header + ordered track rows."""

    __gtype_name__ = "HTPlaylistPage"

    # Playlists have no local favorite column; the heart button is hidden.
    favorite_kind = None

    def _load_async(self):
        row = utils.db.read(
            lambda c: c.execute(
                "SELECT * FROM playlists WHERE id=?", (self.id,)
            ).fetchone()
        )
        self.item = dict(row) if row else {"id": self.id, "name": _("Playlist")}
        self.item["kind"] = "playlist"
        self.tracks = utils.db.playlist_tracks(self.id)

    def _load_finish(self):
        title = item_get(self.item, "name") or _("Playlist")
        subtitle = _("Playlist")
        self._setup_ui(title, subtitle, self.tracks)
