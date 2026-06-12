# browse_item.py
#
# Copyright (C) 2026 Tyler Reece
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Browse-item accessors (gi-free, headless-testable).

The Phase 5 widget kit consumes *browse items* that are EITHER kind-tagged db
row dicts (from ``Database`` paged helpers) OR model dataclasses
(``lib.jellyfin.models``). These two helpers read them uniformly so no widget
ever needs ``isinstance`` on a concrete type. See the convention docstring in
``src/widgets/card_widget.py``.
"""

# Map model dataclass name -> kind string.
_MODEL_KIND = {
    "Track": "track",
    "Album": "album",
    "Artist": "artist",
    "Playlist": "playlist",
    "Genre": "genre",
}

# Navigation action name per non-track kind (clicking a card pushes that page).
# Single source of truth — imported by both card_widget and wide_card_widget.
KIND_ACTION = {
    "album": "win.push-album-page",
    "artist": "win.push-artist-page",
    "playlist": "win.push-playlist-page",
}


def item_kind(item):
    """Return the browse-item kind string for a dict or model dataclass.

    Dicts carry an explicit ``kind`` key (set by the db helpers); model
    dataclasses derive it from their class name. Returns None for an untagged
    dict or an unrecognised object.
    """
    if isinstance(item, dict):
        return item.get("kind")
    return _MODEL_KIND.get(type(item).__name__)


def item_get(item, attr, default=None):
    """Read ``attr`` from a browse item (dict key or dataclass attribute)."""
    if isinstance(item, dict):
        return item.get(attr, default)
    return getattr(item, attr, default)
