# cache.py
#
# Copyright 2025 Nokse <nokse@posteo.com>
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

# Phase 0 stub: tidalapi types removed.
# HTCache is an in-memory dict cache keyed by string ID.
# Jellyfin item caching implemented in Phase 1.

from typing import Any, Dict


class HTCache:
    """Generic in-memory item cache.  Jellyfin fetch logic added in Phase 1."""

    def __init__(self) -> None:
        self.artists: Dict[str, Any] = {}
        self.albums: Dict[str, Any] = {}
        self.tracks: Dict[str, Any] = {}
        self.playlists: Dict[str, Any] = {}
        self.mixes: Dict[str, Any] = {}

    def get_artist(self, artist_id: str) -> Any:
        return self.artists.get(artist_id)

    def get_album(self, album_id: str) -> Any:
        return self.albums.get(album_id)

    def get_track(self, track_id: str) -> Any:
        return self.tracks.get(track_id)

    def get_playlist(self, playlist_id: str) -> Any:
        return self.playlists.get(playlist_id)

    def get_mix(self, mix_id: str) -> Any:
        return self.mixes.get(mix_id)
