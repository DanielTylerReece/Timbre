# lyrics_cache.py
#
# Copyright (C) 2026 Tyler Reece
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Pure, headless-testable decision logic for the lyrics ai_cache.

A 404 / no-lyrics fetch must NOT poison the cache with an empty list, and any
pre-existing poisoned empty row must be treated as a miss so it self-heals on
the next fetch. These two predicates encode that policy with no gi/db imports.
"""

from typing import Optional, Sequence


def should_use_cache(cached: Optional[Sequence]) -> bool:
    """Return True only when the cached value is a usable (non-empty) hit.

    ``None``  -> cache miss (never fetched)            -> False
    ``[]``    -> poisoned empty row (self-heal: refetch) -> False
    nonempty  -> real cached lyrics                     -> True
    """
    return bool(cached)


def should_cache(lines: Optional[Sequence], fetch_ok: bool) -> bool:
    """Return True only when a successful fetch produced non-empty lyrics.

    Empty results are never cached (no poisoning); failed fetches (``fetch_ok``
    False, e.g. an exception) are never cached either.
    """
    return bool(fetch_ok and lines)
