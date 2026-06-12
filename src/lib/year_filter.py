# year_filter.py
#
# Copyright (C) 2026 Tyler Reece
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Pure (gi-free) helpers for the year-filter dropdown.

The Phase 7 year filter renders a ``Gtk.DropDown`` of ``"All years"`` plus each
``db.album_years()`` entry. These helpers map between the dropdown's selected
index and the year value (None == "All years") with no GTK dependency, so the
index<->year wiring is unit-testable headlessly. The page only builds the
DropDown widget and rebinds the auto-load fetch fn on top.
"""

_ALL_LABEL = "All years"


def dropdown_labels(years):
    """``["All years", "2005", ...]`` — the dropdown string model.

    ``years`` is the ``album_years()`` list (already descending).
    """
    return [_ALL_LABEL] + [str(y) for y in years]


def index_to_year(years, index):
    """Map a selected dropdown ``index`` to a year value.

    Index 0 is ``"All years"`` -> None. Index ``i`` (1-based into ``years``)
    -> ``years[i-1]``. Out-of-range indices fall back to None ("All years").
    """
    if index is None or index <= 0:
        return None
    pos = index - 1
    if 0 <= pos < len(years):
        return years[pos]
    return None


def year_to_index(years, year):
    """Inverse of :func:`index_to_year`: the dropdown index for ``year``.

    None -> 0 ("All years"). A ``year`` not present in ``years`` also falls back
    to 0 (so a preselected year that isn't in the model degrades gracefully).
    """
    if year is None:
        return 0
    try:
        return years.index(year) + 1
    except ValueError:
        return 0
