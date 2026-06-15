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
  1. Search entry. Typing (debounced 250ms, >=3 trimmed chars) shows a
     SUGGESTIONS dropdown under the entry; typing alone NEVER pushes a search
     page. Pressing Enter (``activate``) or picking a suggestion COMMITS the
     query — pushing an HTSearchPage, or live-updating the one already on top.
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

from gi.repository import Adw, Gdk, GLib, Gtk

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
# Minimum trimmed length before the suggestions dropdown fires.
_SUGGEST_MIN_CHARS = 3
# Max suggestion rows in the dropdown (matches db.search_suggestions default).
_SUGGEST_LIMIT = 8
# Human-readable kind captions for suggestion rows.
_KIND_CAPTIONS = {
    "artist": _("Artist"),
    "album": _("Album"),
    "track": _("Track"),
}


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

        # Typing fires SUGGESTIONS (>=3 trimmed chars), never a search push.
        self._debouncer = SearchDebouncer(
            on_fire=self._request_suggestions,
            min_chars=_SUGGEST_MIN_CHARS,
            delay=250,
            schedule=GLib.timeout_add,
            cancel=GLib.source_remove,
        )
        # Tracks the query of the in-flight suggestions fetch so a stale
        # (slower) response that lands after the text changed is dropped.
        self._suggest_query = None
        # The query last COMMITTED (Enter / suggestion pick). Gtk.SearchEntry
        # fires a DELAYED search-changed (~150ms) after set_text, which lands
        # AFTER the commit — without this, that late signal resubmits the
        # debouncer and the suggestions popover reopens over the just-pushed
        # search page. We drop exactly one search-changed whose text equals the
        # committed query; a real later edit (different text) clears it.
        self._committed_query = None

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
        # Typing -> debounced suggestions only (no push). Enter -> commit.
        # stop-search (Escape) and focus loss hide the dropdown.
        self.signals.append((
            entry, entry.connect("search-changed", self._on_search_changed)
        ))
        self.signals.append((
            entry, entry.connect("activate", self._on_entry_activate)
        ))
        self.signals.append((
            entry, entry.connect("stop-search", self._on_stop_search)
        ))
        focus = Gtk.EventControllerFocus()
        self.signals.append((
            focus, focus.connect("leave", self._on_entry_focus_leave)
        ))
        entry.add_controller(focus)
        self._focus_controller = focus
        # Down/Up move the SUGGESTION SELECTION (not focus) while the dropdown
        # is open. Bubble phase: the SearchEntry's text widget never consumes
        # vertical arrows, so handling them here doesn't disturb editing, and
        # other keys (Escape -> stop-search, Space -> window accel, all typing)
        # fall straight through untouched. We only STOP propagation for Down/Up
        # while the popover is visible.
        keys = Gtk.EventControllerKey()
        self.signals.append((
            keys, keys.connect("key-pressed", self._on_entry_key)
        ))
        entry.add_controller(keys)
        self._key_controller = keys
        self.content.append(entry)
        # Exposed so the Ctrl+F shortcut can focus it (window.focus_search()).
        self.search_entry = entry
        self._build_suggestions_popover(entry)

    def _build_suggestions_popover(self, entry):
        """Build the (initially empty, hidden) suggestions dropdown.

        Parented to the entry, BOTTOM, ``autohide=False`` so it never grabs
        focus — the user keeps typing while suggestions are visible. The
        popover is unparented in ``disconnect_all`` (a parented popover left
        behind would pin the entry and leak the page).
        """
        # SINGLE (not BROWSE): a populated list starts with NO row selected —
        # the "free typing" state. BROWSE would force-select the first row on
        # every rebuild, which we don't want. Click-to-commit (row-activated)
        # still fires regardless of selection mode. The selected row paints the
        # Adwaita selected state for the keyboard-nav highlight.
        listbox = Gtk.ListBox(
            selection_mode=Gtk.SelectionMode.SINGLE,
            css_classes=["menu"],
        )
        self.signals.append((
            listbox, listbox.connect("row-activated", self._on_suggestion_row)
        ))
        popover = Gtk.Popover(
            autohide=False, has_arrow=False,
            position=Gtk.PositionType.BOTTOM,
            css_classes=["menu"],
        )
        popover.set_child(listbox)
        popover.set_parent(entry)
        self._suggest_list = listbox
        self._suggest_popover = popover

    def focus_search(self):
        """Move keyboard focus to the search entry (Ctrl+F shortcut)."""
        entry = getattr(self, "search_entry", None)
        if entry is not None:
            entry.grab_focus()

    # ------------------------------------------------------------------ #
    # Typing -> suggestions (no push)                                    #
    # ------------------------------------------------------------------ #

    def _on_search_changed(self, entry):
        text = entry.get_text().strip()
        # Layer 2 (source): swallow the delayed search-changed that set_text
        # fires AFTER a commit. If the entry text still equals what we just
        # committed, this is that stale signal — drop it once (without
        # resubmitting the debouncer) so the popover can't reopen. Any real
        # later edit produces different text and falls through normally.
        if self._committed_query is not None and text == self._committed_query:
            self._committed_query = None
            return
        # A genuine edit to other text invalidates a pending commit guard.
        self._committed_query = None
        if len(text) < _SUGGEST_MIN_CHARS:
            # Below threshold: drop the dropdown and any pending fetch.
            self._suggest_query = None
            self._hide_suggestions()
        self._debouncer.submit(entry.get_text())

    def _request_suggestions(self, query):
        """Debouncer fire (>=3 chars): fetch suggestions off the main thread."""
        db = utils.db
        if db is None:
            return
        self._suggest_query = query

        def work():
            return db.search_suggestions(query, limit=_SUGGEST_LIMIT)

        utils.run_async(
            work,
            on_done=lambda rows: self._render_suggestions(query, rows),
            owner=self,
        )

    def _render_suggestions(self, query, rows):
        """Main-loop: render suggestion rows, dropping stale responses."""
        entry = getattr(self, "search_entry", None)
        if entry is None:
            return
        # Stale-response guard: the entry text may have changed (or fallen
        # below threshold) since this fetch was issued. run_async's owner guard
        # only covers page teardown, not query churn.
        if query != entry.get_text().strip() or query != self._suggest_query:
            return
        listbox = self._suggest_list
        if listbox is None:  # torn down (defense for any late delivery)
            return
        child = listbox.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            listbox.remove(child)
            child = nxt
        if not rows:
            self._hide_suggestions()
            return
        for row in rows:
            listbox.append(self._make_suggestion_row(row))
        self._show_suggestions()

    def _make_suggestion_row(self, suggestion):
        """A suggestion row: ellipsized name + dim kind caption."""
        name = suggestion.get("name") or ""
        kind = suggestion.get("kind") or ""
        box = Gtk.Box(spacing=8, margin_top=4, margin_bottom=4,
                      margin_start=8, margin_end=8)
        box.append(Gtk.Label(label=name, xalign=0, hexpand=True, ellipsize=3,
                             max_width_chars=32, can_target=False))
        box.append(Gtk.Label(
            label=_KIND_CAPTIONS.get(kind, kind.title()), xalign=1,
            css_classes=["caption", "dim-label"], can_target=False))
        row = Gtk.ListBoxRow(activatable=True)
        row.set_child(box)
        # Stash the committed text on the row for row-activated.
        row._suggest_name = name
        return row

    def _show_suggestions(self):
        entry = getattr(self, "search_entry", None)
        popover = getattr(self, "_suggest_popover", None)
        if entry is None or popover is None:
            return
        # Layer 1 (safety net): never pop the dropdown when Explore is not the
        # visible nav page. A committed search pushes a search page on top of
        # Explore (still in the stack, so run_async's owner guard passes); a
        # late suggestions fetch must not reopen the popover over that page.
        nav = utils.navigation_view
        if nav is not None and nav.get_visible_page() is not self:
            return
        # Match the dropdown width to the entry's current allocation.
        width = entry.get_allocated_width()
        if width > 0:
            popover.set_size_request(width, -1)
        popover.popup()

    def _hide_suggestions(self):
        popover = getattr(self, "_suggest_popover", None)
        if popover is not None:
            popover.popdown()

    def _on_entry_focus_leave(self, _controller):
        self._hide_suggestions()

    def _on_stop_search(self, _entry):
        # Escape in the SearchEntry. Two-stage: if a suggestion is selected,
        # the FIRST Escape clears the selection (back to free typing) and keeps
        # the dropdown open; only Escape with no selection hides it. The key
        # controller intercepts the selection-clearing case before stop-search
        # fires (see _on_entry_key), so by the time we're here there is no
        # selection — hide the dropdown and cancel the pending fetch as before.
        self._suggest_query = None
        self._hide_suggestions()

    # ------------------------------------------------------------------ #
    # Keyboard navigation of the suggestions dropdown (focus stays in the #
    # entry; only the listbox SELECTION moves).                          #
    # ------------------------------------------------------------------ #

    def _popover_open(self):
        popover = getattr(self, "_suggest_popover", None)
        return popover is not None and popover.get_visible()

    def _suggestion_rows(self):
        listbox = getattr(self, "_suggest_list", None)
        if listbox is None:
            return []
        rows = []
        child = listbox.get_first_child()
        while child is not None:
            rows.append(child)
            child = child.get_next_sibling()
        return rows

    def _select_suggestion(self, row):
        """Select (and scroll into view) ``row``, or clear when ``row`` is None.

        Selection only — never touches keyboard focus, which stays in the entry
        so the user can keep typing at any moment.
        """
        listbox = getattr(self, "_suggest_list", None)
        if listbox is None:
            return
        if row is None:
            listbox.unselect_all()
            return
        listbox.select_row(row)
        # Keep the selected row visible if the list ever scrolls.
        adj = listbox.get_adjustment()
        if adj is not None:
            ok, rect = row.compute_bounds(listbox)
            if ok:
                top = rect.origin.y
                bottom = top + rect.size.height
                value = adj.get_value()
                page = adj.get_page_size()
                if top < value:
                    adj.set_value(top)
                elif bottom > value + page:
                    adj.set_value(bottom - page)

    def _on_entry_key(self, _controller, keyval, _keycode, _state):
        """Down/Up move the suggestion selection while the dropdown is open.

        Returns ``Gdk.EVENT_STOP`` only for the keys it actually handles so
        normal editing, Escape (stop-search) and the window Space accel are
        untouched. Enter is intentionally NOT handled here — it flows to the
        entry's ``activate`` signal, which checks for a selected row, so exactly
        one commit happens (see ``_on_entry_activate``).
        """
        if not self._popover_open():
            return Gdk.EVENT_PROPAGATE
        rows = self._suggestion_rows()
        if not rows:
            return Gdk.EVENT_PROPAGATE
        listbox = self._suggest_list
        current = listbox.get_selected_row()

        if keyval in (Gdk.KEY_Down, Gdk.KEY_KP_Down):
            if current is None:
                self._select_suggestion(rows[0])
            else:
                idx = rows.index(current)
                # Stop at the end (Adwaita-conventional — no wrap).
                if idx < len(rows) - 1:
                    self._select_suggestion(rows[idx + 1])
            return Gdk.EVENT_STOP

        if keyval in (Gdk.KEY_Up, Gdk.KEY_KP_Up):
            if current is None:
                return Gdk.EVENT_STOP
            idx = rows.index(current)
            if idx == 0:
                # Up from the first row clears selection -> free typing.
                self._select_suggestion(None)
            else:
                self._select_suggestion(rows[idx - 1])
            return Gdk.EVENT_STOP

        if keyval == Gdk.KEY_Escape and current is not None:
            # First Escape with a selection clears it (keeps dropdown open);
            # stop-search (which hides the dropdown) only fires once there's no
            # selection. Consume so stop-search doesn't also run this press.
            self._select_suggestion(None)
            return Gdk.EVENT_STOP

        return Gdk.EVENT_PROPAGATE

    # ------------------------------------------------------------------ #
    # Commit (Enter / suggestion click) -> push or live-update           #
    # ------------------------------------------------------------------ #

    def _on_entry_activate(self, entry):
        # If a suggestion is selected via keyboard nav, Enter commits THAT row
        # (same path as a click: set entry text + commit). Otherwise commit the
        # typed text. Enter is handled ONLY here (the key controller leaves
        # Return alone) so exactly one commit happens.
        listbox = getattr(self, "_suggest_list", None)
        selected = listbox.get_selected_row() if (
            listbox is not None and self._popover_open()) else None
        if selected is not None:
            self._on_suggestion_row(listbox, selected)
            return
        self._commit_search(entry.get_text())

    def _on_suggestion_row(self, _listbox, row):
        name = getattr(row, "_suggest_name", None)
        if name is None:
            return
        entry = getattr(self, "search_entry", None)
        if entry is not None:
            entry.set_text(name)
        self._commit_search(name)

    def _commit_search(self, query):
        """Commit a query: push a SearchPage, or live-update the one on top.

        Called only on Enter or suggestion selection — never from typing.
        Cancels any pending suggestions fetch and hides the dropdown first.
        """
        text = (query or "").strip()
        self._suggest_query = None
        self._debouncer.cancel()
        self._hide_suggestions()
        if not text:
            return
        # Arm the one-shot guard against the delayed search-changed that
        # set_text(name) (suggestion pick) will emit ~150ms from now. We store
        # the entry's CURRENT text (post-set_text) — that is what the late
        # signal will carry. (On the Enter path the entry already holds `query`,
        # so this matches too.)
        entry = getattr(self, "search_entry", None)
        self._committed_query = (
            entry.get_text().strip() if entry is not None else text
        )
        nav = utils.navigation_view
        visible = nav.get_visible_page() if nav is not None else None
        if visible is not None and visible.get_tag() == "search":
            update = getattr(visible, "update_query", None)
            if callable(update):
                update(text)
                return
        from .search_page import HTSearchPage

        page = HTSearchPage(text)
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
        # A parented popover left behind keeps the entry (and thus the page)
        # alive — unparent it explicitly. popdown first so it isn't visible
        # mid-teardown.
        popover = getattr(self, "_suggest_popover", None)
        if popover is not None:
            popover.popdown()
            popover.unparent()
            self._suggest_popover = None
            self._suggest_list = None
        super().disconnect_all(*args)
