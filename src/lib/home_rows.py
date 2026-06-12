# home_rows.py
#
# Copyright (C) 2026 Tyler Reece
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Pure (gi-free) data-shaping + formatting helpers for the Home page.

Kept import-light and GTK-free so the Home page's non-widget logic — month
label formatting, the AI-placeholder visibility rule, and recents-grid chunking
— is unit-testable headlessly. The Home page imports these and only does widget
construction on top.
"""

# English month names (index 1..12). The Home page is English-only for now;
# this avoids a locale/strftime dependency and stays deterministic in tests.
_MONTHS = (
    "", "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)


def month_label(yyyy_mm) -> str:
    """Format a ``"YYYY-MM"`` key as ``"Month YYYY"`` (e.g. ``"May 2026"``).

    Defensive: tolerates a full ISO timestamp prefix (uses the first 7 chars),
    and returns the raw input unchanged if it can't be parsed (None -> "").
    """
    if not yyyy_mm:
        return ""
    key = str(yyyy_mm)[:7]
    parts = key.split("-")
    if len(parts) != 2:
        return str(yyyy_mm)
    year, mm = parts
    try:
        month_idx = int(mm)
    except ValueError:
        return str(yyyy_mm)
    if not (1 <= month_idx <= 12) or not year.isdigit():
        return str(yyyy_mm)
    return f"{_MONTHS[month_idx]} {year}"


def month_plays_label(n) -> str:
    """Format a play count as ``"N plays"`` (``"1 play"`` for exactly one)."""
    n = int(n or 0)
    return f"{n} play" if n == 1 else f"{n} plays"


def ai_placeholder_visible(provider) -> bool:
    """The AI-feature placeholder rows show ONLY when no provider is configured.

    ``settings ai-provider`` defaults to ``"none"``; any other value means the
    real (Phase 8) feature will fill the section instead, so the placeholder is
    hidden.
    """
    return provider == "none"


def recents_grid_rows(items, cols=3):
    """Chunk ``items`` into rows of ``cols`` for the recents FlowBox grid."""
    items = list(items)
    return [items[i:i + cols] for i in range(0, len(items), cols)]


def collage_slots(covers, n=4):
    """Assign the available ``covers`` to ``n`` collage cells (month-card 2x2).

    Fill rules:
      * 4+ covers -> the first ``n`` (one distinct cover per cell)
      * 2-3 covers -> cycle through them to fill the remaining cells
      * 1 cover    -> that cover fills every cell
      * 0 covers   -> ``[]`` (caller keeps the text-only card; no empty grid)

    ``covers`` is any sequence (album dicts, ids, paths — opaque here). Returns
    a list of length ``n`` (or empty when there are no covers).
    """
    covers = list(covers)
    if not covers:
        return []
    return [covers[i % len(covers)] for i in range(n)]
