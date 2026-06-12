# search_page.py
#
# Copyright (C) 2026 Tyler Reece
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Search results page — sectioned db.search() results.

Sections: Artists / Albums (carousels), Tracks (rows), Genres (chips). A section
hides when empty; an empty state shows when nothing matched. The Explore search
entry owns the debounce and calls ``update_query(q)`` when this page is already
on top (live re-query as the user keeps typing), else pushes a fresh page.

The query runs on a worker thread (SQLite only) via ``run_async`` (owner-guarded
so a late result after pop is dropped); the results box rebuilds on the main
loop. The genre chips push the genre page through the same nav action wiring.
"""

import logging
from gettext import gettext as _

from gi.repository import Gtk

from ..disconnectable_iface import IDisconnectable
from ..lib import utils
from .page import Page

logger = logging.getLogger(__name__)

# Per-section result cap (matches db.search default).
_LIMIT = 20


class HTSearchPage(Page):
    """Sectioned, live-updating search results for a query string."""

    __gtype_name__ = "HTSearchPage"

    def __init__(self, query=""):
        IDisconnectable.__init__(self)
        super().__init__()

        self.query = query or ""
        self.results = None
        self._results_box = None
        # ``self._section_signals`` (per-build section scope) is declared in the
        # base Page; re-queries disconnect+clear it via _clear so live typing
        # doesn't accrete stale entries on self.signals.
        # Built immediately; the data fetch is async (like from_function_page).
        self.content_stack.set_visible_child_name("content")

        self._results_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.content.append(self._results_box)

    def _load_async(self) -> None:
        self.results = utils.db.search(self.query, _LIMIT)

    def _load_finish(self) -> None:
        self.set_title(_("Search"))
        self._render(self.results)

    # ------------------------------------------------------------------ #
    # Live update (the Explore entry calls this when the page is on top) #
    # ------------------------------------------------------------------ #

    def update_query(self, query) -> None:
        """Re-run the search for a new query and rebuild the results in place."""
        self.query = query or ""

        def work():
            return utils.db.search(self.query, _LIMIT)

        def done(results):
            self.results = results
            self._render(results)

        utils.run_async(work, on_done=done, owner=self)

    # ------------------------------------------------------------------ #
    # Rendering                                                          #
    # ------------------------------------------------------------------ #

    def _render(self, results):
        self._clear()
        results = results or {}
        artists = results.get("artists") or []
        albums = results.get("albums") or []
        tracks = results.get("tracks") or []
        genres = results.get("genres") or []

        any_results = bool(artists or albums or tracks or genres)
        if not any_results:
            self._build_empty_state()
            return

        # Tag dicts so the widget kit dispatches correctly (db.search rows are
        # untagged plain dicts).
        artists = [{**a, "kind": "artist"} for a in artists]
        albums = [{**a, "kind": "album"} for a in albums]
        tracks = [{**t, "kind": "track"} for t in tracks]

        self._section(lambda: self.new_carousel_for(
            _("Artists"), artists, scope=self._section_signals))
        self._section(lambda: self.new_carousel_for(
            _("Albums"), albums, scope=self._section_signals))
        self._section(lambda: self.new_track_list_for(
            _("Tracks"), tracks, scope=self._section_signals))
        if genres:
            self._build_genre_chips(genres)

    def _build_genre_chips(self, genres):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin_start=12,
                      margin_end=12, margin_top=12, spacing=6)
        box.append(Gtk.Label(label=_("Genres"), xalign=0,
                             css_classes=["title-3"]))
        flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE,
                           max_children_per_line=8, column_spacing=6,
                           row_spacing=6)
        for g in genres:
            name = g.get("name") or ""
            chip = Gtk.Button(label=name, css_classes=["pill"])
            self._section_signals.append((
                chip, chip.connect("clicked", self._on_genre_chip, name)
            ))
            flow.append(chip)
        box.append(flow)
        self._results_box.append(box)

    def _on_genre_chip(self, _btn, name):
        from .genre_page import HTGenrePage

        utils.navigation_view.push(HTGenrePage.new_from_id(name).load())

    def _build_empty_state(self):
        from gi.repository import Adw

        status = Adw.StatusPage(
            icon_name="system-search-symbolic",
            title=_("No results"),
            description=_("Nothing matched “{}”.").format(self.query),
            vexpand=True,
        )
        self._results_box.append(status)

    # ------------------------------------------------------------------ #
    # Section reparenting + teardown (mirrors decade_page)               #
    # ------------------------------------------------------------------ #

    def _section(self, build_fn):
        """Run a base section builder (appends to self.content), then move the
        new child into the swappable results box."""
        self.reparent_section(self._results_box, build_fn)

    def _clear(self):
        if self._results_box is None:
            return
        self.teardown_sections(self._results_box, self._section_signals)
