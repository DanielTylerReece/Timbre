# explore_page.py
#
# Copyright (C) 2026 Tyler Reece
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""The Explore page — search + genres / decades / years.

Top-to-bottom (High Tide explore look):
  1. Search entry (debounced 250ms, >=2 chars) — pushes / live-updates a
     SearchPage.
  2. Genres — a 2-per-row grid of large rounded buttons (bold name + "N
     tracks"). HIDDEN only when the library has NO genre with content.
  3. Decades — a 2-per-row grid of large rounded buttons ("1970s" + "N
     tracks").
  4. Years — a horizontal carousel of compact year buttons with prev/next
     arrows -> a year-filtered albums list.

Each section is a titled row (bold title + prev/next arrow buttons on the
right) over its content, mirroring the carousel section pattern. All data is
SQLite (queried in the single ``_load_async`` pass). Sections hide when empty.
The genre / decade / year buttons are plain labeled buttons (these items have
no art); their click + arrow handlers are tracked in ``self.signals`` so
``disconnect_all`` severs them on pop (no page leak).
"""

import logging
from gettext import gettext as _

from gi.repository import Adw, GLib, Gtk

from ..lib import utils
from ..lib.search_debounce import SearchDebouncer
from .page import Page

logger = logging.getLogger(__name__)

# Genres row hides only when there are NO genres with content. A library with
# zero genre tags (common on live servers) still hides the section; a single
# tagged genre is enough to show it.
_MIN_GENRES = 1
# Large grid sections render this many big buttons per row.
_GRID_COLUMNS = 2
# Year-filtered albums list page size.
_ALBUMS_LIMIT = 50


class HTExplorePage(Page):
    """Search + Genres / Decades / Years browse surface."""

    __gtype_name__ = "HTExplorePage"

    def _load_async(self) -> None:
        db = utils.db
        self.genres = db.genres_with_counts()
        self.decades = db.decades()
        self.album_years = db.album_years()

    def _load_finish(self) -> None:
        self.set_tag("explore")
        self.set_title(_("Explore"))

        self._debouncer = SearchDebouncer(
            on_fire=self._fire_search,
            min_chars=2,
            delay=250,
            schedule=GLib.timeout_add,
            cancel=GLib.source_remove,
        )

        self._build_search_entry()

        # Genres — large 2-per-row buttons. Hidden only with zero tagged genres.
        if len(self.genres) >= _MIN_GENRES:
            self._build_grid_section(
                _("Genres"),
                [(name, self._tracks_label(n), ("genre", name))
                 for name, n in self.genres],
            )

        # Decades — large 2-per-row buttons ("1970s" + "N tracks").
        if self.decades:
            self._build_grid_section(
                _("Decades"),
                [(_("{}s").format(start), self._tracks_label(n),
                  ("decade", start))
                 for start, n in self.decades],
            )

        # Years — horizontal carousel of compact year buttons (+ arrows).
        if self.album_years:
            self._build_year_carousel(
                _("Years"),
                [(str(y), ("year", y)) for y in self.album_years],
            )

    # ------------------------------------------------------------------ #
    # Search                                                             #
    # ------------------------------------------------------------------ #

    def _build_search_entry(self):
        entry = Gtk.SearchEntry(
            placeholder_text=_("Search artists, albums, tracks…"),
            margin_start=12, margin_end=12, margin_top=12, hexpand=True,
        )
        self.signals.append((
            entry, entry.connect("search-changed", self._on_search_changed)
        ))
        self.content.append(entry)
        # Exposed so the Ctrl+F shortcut can focus it (window.focus_search()).
        self.search_entry = entry

    def focus_search(self):
        """Move keyboard focus to the search entry (Ctrl+F shortcut)."""
        entry = getattr(self, "search_entry", None)
        if entry is not None:
            entry.grab_focus()

    def _on_search_changed(self, entry):
        self._debouncer.submit(entry.get_text())

    def _fire_search(self, query):
        """Debouncer fire: push a SearchPage, or live-update the one on top."""
        nav = utils.navigation_view
        visible = nav.get_visible_page() if nav is not None else None
        if visible is not None and visible.get_tag() == "search":
            update = getattr(visible, "update_query", None)
            if callable(update):
                update(query)
                return
        from .search_page import HTSearchPage

        page = HTSearchPage(query)
        page.set_tag("search")
        page.load()
        nav.push(page)

    # ------------------------------------------------------------------ #
    # Sections (genres / decades / years) — High Tide explore look       #
    # ------------------------------------------------------------------ #

    def _tracks_label(self, n):
        return _("{} track").format(n) if n == 1 else _("{} tracks").format(n)

    def _section_header(self, title, prev_btn=None, next_btn=None):
        """A bold section title row with optional prev/next arrows on the right.

        Mirrors the carousel widget's header (title-3 label, hexpand, circular
        arrow buttons aligned end) so Explore matches the rest of the browse
        surface. Returns the header Box.
        """
        header = Gtk.Box(margin_bottom=6)
        header.append(Gtk.Label(label=title, xalign=0, hexpand=True,
                                css_classes=["title-3"], ellipsize=3))
        for btn in (prev_btn, next_btn):
            if btn is not None:
                header.append(btn)
        return header

    def _build_grid_section(self, title, entries):
        """A titled 2-per-row grid of LARGE rounded buttons (no arrows).

        ``entries`` is ``(label, subtitle, target)``; each becomes a big
        rounded "card" button with a bold label over a dim count, matching the
        upstream genre buttons. ``target`` is ``("genre", name)`` |
        ``("decade", start)``.
        """
        if not entries:
            return
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin_start=12,
                      margin_end=12, margin_top=12, spacing=6)
        box.append(self._section_header(title))
        # FlowBox capped at 2 columns gives the 2-per-row grid that reflows on
        # narrow widths; homogeneous children keep the big buttons equal width.
        flow = Gtk.FlowBox(
            selection_mode=Gtk.SelectionMode.NONE,
            min_children_per_line=_GRID_COLUMNS,
            max_children_per_line=_GRID_COLUMNS,
            column_spacing=12, row_spacing=12, homogeneous=True,
        )
        for label, subtitle, target in entries:
            flow.append(self._make_big_button(label, subtitle, target))
        box.append(flow)
        self.content.append(box)

    def _build_year_carousel(self, title, entries):
        """A titled horizontal carousel of compact year buttons + arrows.

        ``entries`` is ``(label, target)`` where ``target`` is
        ``("year", year)``. The prev/next arrows scroll the row a page at a
        time (Adwaita spring animation), matching the carousel widget.
        """
        if not entries:
            return
        prev_btn = Gtk.Button(icon_name="go-previous-symbolic",
                              css_classes=["circular"], valign=Gtk.Align.CENTER,
                              margin_start=6, sensitive=False)
        next_btn = Gtk.Button(icon_name="go-next-symbolic",
                              css_classes=["circular"], valign=Gtk.Align.CENTER,
                              margin_start=6)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin_start=12,
                      margin_end=12, margin_top=12, spacing=6)
        box.append(self._section_header(title, prev_btn, next_btn))

        scroller = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.EXTERNAL,
            vscrollbar_policy=Gtk.PolicyType.NEVER,
            hexpand=True, overflow=Gtk.Overflow.VISIBLE,
        )
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6,
                      halign=Gtk.Align.START)
        for label, target in entries:
            row.append(self._make_year_button(label, target))
        scroller.set_child(row)
        box.append(scroller)
        self.content.append(box)

        adj = scroller.get_hadjustment()
        self.signals.append((
            prev_btn, prev_btn.connect(
                "clicked", self._scroll_carousel, scroller, -1)
        ))
        self.signals.append((
            next_btn, next_btn.connect(
                "clicked", self._scroll_carousel, scroller, 1)
        ))
        self.signals.append((
            adj, adj.connect(
                "value-changed", self._update_arrows, prev_btn, next_btn)
        ))
        GLib.idle_add(self._update_arrows, adj, prev_btn, next_btn)

    def _make_big_button(self, label, subtitle, target):
        """A large rounded card button: bold label over a dim track count."""
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                        margin_top=12, margin_bottom=12, margin_start=16,
                        margin_end=16)
        inner.append(Gtk.Label(label=label, xalign=0, ellipsize=3,
                               css_classes=["title-4"]))
        inner.append(Gtk.Label(label=subtitle, xalign=0,
                               css_classes=["caption", "dim-label"]))
        btn = Gtk.Button(css_classes=["card", "explore-big-button"],
                         hexpand=True)
        btn.set_child(inner)
        self.signals.append((
            btn, btn.connect("clicked", self._on_chip_clicked, target)
        ))
        return btn

    def _make_year_button(self, label, target):
        """A compact pill year button for the Years carousel."""
        btn = Gtk.Button(label=label, css_classes=["pill"],
                         valign=Gtk.Align.CENTER)
        self.signals.append((
            btn, btn.connect("clicked", self._on_chip_clicked, target)
        ))
        return btn

    def _scroll_carousel(self, _btn, scroller, direction):
        """Page the year carousel one viewport in ``direction`` (-1/+1)."""
        adj = scroller.get_hadjustment()
        page = adj.get_page_size()
        value = adj.get_value()
        upper = adj.get_upper()
        target = value + direction * page
        target = min(max(target, 0.0), max(upper - page, 0.0))
        if target == value:
            return
        anim_target = Adw.CallbackAnimationTarget.new(adj.set_value)
        params = Adw.SpringParams.new(
            damping_ratio=1.0, mass=1.0, stiffness=1200.0)
        anim = Adw.SpringAnimation.new(scroller, value, target, params,
                                       anim_target)
        anim.set_clamp(True)
        anim.play()

    def _update_arrows(self, adj, prev_btn, next_btn):
        """Enable/disable the year-carousel arrows at the scroll extents."""
        value = adj.get_value()
        max_scroll = adj.get_upper() - adj.get_page_size()
        prev_btn.set_sensitive(value > 0)
        next_btn.set_sensitive(value < max_scroll)
        return False

    def _on_chip_clicked(self, _btn, target):
        kind, value = target
        nav = utils.navigation_view
        if kind == "genre":
            from .genre_page import HTGenrePage
            nav.push(HTGenrePage.new_from_id(value).load())
        elif kind == "decade":
            from .decade_page import HTDecadePage
            nav.push(HTDecadePage.new_from_id(str(value)).load())
        elif kind == "year":
            self._push_year_albums(value)

    def _push_year_albums(self, year):
        """Push a year-filtered albums list (album_years chip click)."""
        from .from_function_page import HTFromFunctionPage

        db = utils.db
        page = HTFromFunctionPage(
            _("Albums"), item_type="card",
            year_filter=True, preselect_year=year,
        )
        page.set_year_function(
            lambda yr, offset, limit: db.albums_page(offset, limit, year=yr)
        )
        page.load()
        utils.navigation_view.push(page)

    # ------------------------------------------------------------------ #
    # Teardown — cancel the debouncer so a late timeout never fires into #
    # a popped page (owner-guard parity for the GLib.timeout path).      #
    # ------------------------------------------------------------------ #

    def disconnect_all(self, *args):
        debouncer = getattr(self, "_debouncer", None)
        if debouncer is not None:
            # dispose() cancels any pending timeout AND drops the
            # (bound-method -> page) fire reference so a popped Explore page is
            # collectable immediately (cancel() alone left a transient cycle).
            debouncer.dispose()
            self._debouncer = None
        super().disconnect_all(*args)
