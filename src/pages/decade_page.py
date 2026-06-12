# decade_page.py
#
# Copyright (C) 2026 Tyler Reece
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Decade detail page — the Phase 7 headline feature.

Title is "1970s". A linked row of year-chip ToggleButtons ("All" + each year in
the decade) narrows BOTH the Top-tracks list and the Albums carousel to the
selected year. Selecting a chip refetches the two sections on a worker thread
(``decade_top_tracks`` / ``decade_albums`` with the ``year`` param), then
rebuilds only the sections box on the main loop — the chips row stays put. The
refetch is owner-guarded so a late callback after pop is dropped.

``self.id`` carries the decade start year (e.g. ``"1970"``) via ``new_from_id``.
"""

import logging
from gettext import gettext as _

from gi.repository import Gtk

from ..lib import utils
from .page import Page

logger = logging.getLogger(__name__)

_TOP_TRACKS = 10
_PREVIEW = 16


class HTDecadePage(Page):
    """Decade detail with a year-chip filter row + Top tracks + Albums."""

    __gtype_name__ = "HTDecadePage"

    def _load_async(self) -> None:
        db = utils.db
        self.start_year = int(self.id) if self.id is not None else 0
        self.years = db.years_in_decade(self.start_year)
        # Initial (All-years) sections.
        self.top_tracks = db.decade_top_tracks(self.start_year, _TOP_TRACKS)
        self.albums = db.decade_albums(self.start_year, _PREVIEW)
        # Selected year for the active filter (None == "All").
        self.selected_year = None
        self._sections_box = None
        self._chip_buttons = []
        # ``self._section_signals`` (the per-build section scope) is declared in
        # the base Page; year toggles disconnect+clear it via _clear_sections.

    def _load_finish(self) -> None:
        self.set_title(_("{}s").format(self.start_year))

        # Year-chip row (only when the decade actually has years).
        self._build_chips_row()

        # Sections live in their own box so a year toggle rebuilds just this.
        self._sections_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.content.append(self._sections_box)
        self._build_sections(self.top_tracks, self.albums)

    # ------------------------------------------------------------------ #
    # Year chips                                                         #
    # ------------------------------------------------------------------ #

    def _build_chips_row(self):
        if not self.years:
            return
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0,
                      margin_start=12, margin_end=12, margin_top=12,
                      css_classes=["linked"])

        all_btn = Gtk.ToggleButton(label=_("All"), active=True)
        self.signals.append((
            all_btn, all_btn.connect("toggled", self._on_chip_toggled, None)
        ))
        box.append(all_btn)
        self._chip_buttons.append((all_btn, None))

        for year in self.years:
            btn = Gtk.ToggleButton(label=str(year), group=all_btn)
            self.signals.append((
                btn, btn.connect("toggled", self._on_chip_toggled, year)
            ))
            box.append(btn)
            self._chip_buttons.append((btn, year))

        self.content.append(box)

    def _on_chip_toggled(self, button, year):
        # ToggleButtons in a group emit toggled on both the de-activated and the
        # newly-activated button; only act on the activation, and only when the
        # selection actually changed.
        if not button.get_active():
            return
        if year == self.selected_year:
            return
        self.selected_year = year
        self._refetch_for_year(year)

    def _refetch_for_year(self, year):
        """Refetch both sections for ``year`` off the main thread, then rebuild.

        SQLite reads run on the worker; the widget rebuild is marshalled back to
        the main loop. Owner-guarded: if the page is popped mid-flight the
        ``run_async`` owner check drops the rebuild.
        """
        db = utils.db
        start = self.start_year

        def work():
            return (
                db.decade_top_tracks(start, _TOP_TRACKS, year=year),
                db.decade_albums(start, _PREVIEW, year=year),
            )

        def done(result):
            # A newer toggle may have superseded this one; ignore stale results.
            if year != self.selected_year:
                return
            tracks, albums = result
            self._build_sections(tracks, albums)

        utils.run_async(work, on_done=done, owner=self)

    # ------------------------------------------------------------------ #
    # Sections (rebuilt on year toggle)                                  #
    # ------------------------------------------------------------------ #

    def _build_sections(self, tracks, albums):
        # Tear down the previous section widgets (disconnect their signals so a
        # rebuild doesn't leak prior track-row / card handlers), then rebuild.
        self._clear_sections()

        db = utils.db
        start = self.start_year
        year = self.selected_year

        self.reparent_section(
            self._sections_box,
            lambda: self.new_track_list_for(
                _("Top tracks"), tracks,
                more_function=lambda offset, limit: db.decade_top_tracks(
                    start, limit, offset=offset, year=year
                ),
                scope=self._section_signals,
            ),
        )
        self.reparent_section(
            self._sections_box,
            lambda: self.new_carousel_for(
                _("Albums"), albums,
                more_function=lambda offset, limit: db.decade_albums(
                    start, limit, offset=offset, year=year
                ),
                scope=self._section_signals,
            ),
        )

    def _clear_sections(self):
        if self._sections_box is None:
            return
        self.teardown_sections(self._sections_box, self._section_signals)
