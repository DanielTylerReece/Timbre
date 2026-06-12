# models.py
#
# Copyright (C) 2026 Tyler Reece
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Domain models for Jellyfin items.

Each model exposes a ``from_item`` classmethod that maps a raw Jellyfin
``/Items`` dict onto the model. Mapping is fully None-safe: missing keys
never raise.
"""

import json
from dataclasses import dataclass


def _first(seq):
    """Return the first element of a list-like, else None (None-safe)."""
    if isinstance(seq, list) and seq:
        return seq[0]
    return None


def _image_tag(item):
    tags = item.get("ImageTags")
    if isinstance(tags, dict):
        return tags.get("Primary")
    return None


def _user(item):
    ud = item.get("UserData")
    return ud if isinstance(ud, dict) else {}


@dataclass
class Track:
    id: "str | None"
    name: "str | None"
    album_id: "str | None"
    album_name: "str | None"
    artist_id: "str | None"
    artist_name: "str | None"
    duration_ticks: "int | None"
    index_number: "int | None"
    parent_index_number: "int | None"
    year: "int | None"
    genres: "list | None"
    bitrate: "int | None"
    codec: "str | None"
    is_favorite: "bool | None"
    play_count: "int | None"
    last_played: "str | None"
    date_created: "str | None"
    library_id: "str | None"

    @classmethod
    def from_item(cls, item: dict) -> "Track":
        # Artist id: first of ArtistItems[].Id (fallback None).
        artist_item = _first(item.get("ArtistItems"))
        artist_id = artist_item.get("Id") if isinstance(artist_item, dict) else None

        # Artist name: first Artists[] or AlbumArtist.
        artist_name = _first(item.get("Artists")) or item.get("AlbumArtist")

        # Bitrate + codec come from the first MediaSource (None-safe).
        media_source = _first(item.get("MediaSources"))
        bitrate = None
        codec = None
        if isinstance(media_source, dict):
            bitrate = media_source.get("Bitrate")
            for stream in media_source.get("MediaStreams") or []:
                if isinstance(stream, dict) and stream.get("Type") == "Audio":
                    codec = stream.get("Codec")
                    break

        ud = _user(item)

        return cls(
            id=item.get("Id"),
            name=item.get("Name"),
            album_id=item.get("AlbumId"),
            album_name=item.get("Album"),
            artist_id=artist_id,
            artist_name=artist_name,
            duration_ticks=item.get("RunTimeTicks"),
            index_number=item.get("IndexNumber"),
            parent_index_number=item.get("ParentIndexNumber"),
            year=item.get("ProductionYear"),
            genres=item.get("Genres"),
            bitrate=bitrate,
            codec=codec,
            is_favorite=ud.get("IsFavorite"),
            play_count=ud.get("PlayCount"),
            last_played=ud.get("LastPlayedDate"),
            date_created=item.get("DateCreated"),
            library_id=None,  # set later by the sync layer
        )

    @classmethod
    def from_row(cls, row: dict) -> "Track":
        """Build a Track from a local SQLite ``tracks`` row dict.

        Browse pages hand the player raw db rows, but the player, MPRIS,
        queue rows and the player pane all read track *attributes* —
        ``getattr(dict, "id")`` is None, which made row clicks silently play
        nothing. Rows are normalised through here at the player boundary.
        Column names map 1:1 to dataclass fields; ``genres`` is stored as a
        JSON array string.
        """
        genres = row.get("genres")
        if isinstance(genres, str):
            try:
                genres = json.loads(genres)
            except (ValueError, TypeError):
                genres = None
        return cls(
            id=row.get("id"),
            name=row.get("name"),
            album_id=row.get("album_id"),
            album_name=row.get("album_name"),
            artist_id=row.get("artist_id"),
            artist_name=row.get("artist_name"),
            duration_ticks=row.get("duration_ticks"),
            index_number=row.get("index_number"),
            parent_index_number=row.get("parent_index_number"),
            year=row.get("year"),
            genres=genres,
            bitrate=row.get("bitrate"),
            codec=row.get("codec"),
            is_favorite=bool(row.get("is_favorite")),
            play_count=row.get("play_count"),
            last_played=row.get("last_played"),
            date_created=row.get("date_created"),
            library_id=row.get("library_id"),
        )


def normalize_tracks(items) -> list:
    """Normalise a heterogeneous track list for the player boundary.

    Dicts (local db rows / kind-tagged browse items) become :class:`Track`
    instances; anything already exposing ``.id`` passes through unchanged.
    Items with no usable id are dropped.
    """
    out = []
    for item in items or []:
        if isinstance(item, dict):
            track = Track.from_row(item)
            if track.id:
                out.append(track)
        elif getattr(item, "id", None):
            out.append(item)
    return out


@dataclass
class Album:
    id: "str | None"
    name: "str | None"
    album_artist_name: "str | None"
    album_artist_id: "str | None"
    year: "int | None"
    date_created: "str | None"
    image_tag: "str | None"
    is_favorite: "bool | None"
    play_count: "int | None"
    last_played: "str | None"

    @classmethod
    def from_item(cls, item: dict) -> "Album":
        aa = _first(item.get("AlbumArtists"))
        album_artist_id = aa.get("Id") if isinstance(aa, dict) else None
        ud = _user(item)
        return cls(
            id=item.get("Id"),
            name=item.get("Name"),
            album_artist_name=item.get("AlbumArtist"),
            album_artist_id=album_artist_id,
            year=item.get("ProductionYear"),
            date_created=item.get("DateCreated"),
            image_tag=_image_tag(item),
            is_favorite=ud.get("IsFavorite"),
            play_count=ud.get("PlayCount"),
            last_played=ud.get("LastPlayedDate"),
        )


@dataclass
class Artist:
    id: "str | None"
    name: "str | None"
    image_tag: "str | None"
    is_favorite: "bool | None"
    overview: "str | None"

    @classmethod
    def from_item(cls, item: dict) -> "Artist":
        ud = _user(item)
        return cls(
            id=item.get("Id"),
            name=item.get("Name"),
            image_tag=_image_tag(item),
            is_favorite=ud.get("IsFavorite"),
            # Server-side bio (from metadata providers). None when the request
            # didn't ask for the Overview field or the artist has none.
            overview=item.get("Overview"),
        )


@dataclass
class Playlist:
    id: "str | None"
    name: "str | None"
    image_tag: "str | None"
    track_count: "int | None"

    @classmethod
    def from_item(cls, item: dict) -> "Playlist":
        return cls(
            id=item.get("Id"),
            name=item.get("Name"),
            image_tag=_image_tag(item),
            track_count=item.get("ChildCount"),
        )


@dataclass
class Genre:
    id: "str | None"
    name: "str | None"

    @classmethod
    def from_item(cls, item: dict) -> "Genre":
        return cls(id=item.get("Id"), name=item.get("Name"))
