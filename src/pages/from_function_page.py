# from_function_page.py
#
# Copyright 2023 Nokse
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

from gettext import gettext as _

from gi.repository import GLib, Gtk

from ..disconnectable_iface import IDisconnectable
from ..lib import utils
from ..lib import year_filter as yf
from ..widgets import HTAutoLoadWidget
from ..widgets.track_list_view import HTTrackListView
from .page import Page

# Upper bound for the single "fetch the whole list" query on the virtualized
# track path. Far above any real library size (live server ≈ 3.2k tracks); the
# dicts are cheap (only the WIDGETS cost memory) and the ListView realizes only
# the visible rows regardless of model size. Acts as a sanity ceiling, not a
# product cap — distinct from the removed 500-row WIDGET cap.
_FULL_FETCH_LIMIT = 100_000


class HTFromFunctionPage(Page):
    """Infinite-scroll list of cards or track rows from a paged fetch fn.

    Used by every carousel / track-list "More" button. ``item_type`` is
    ``"card"`` (albums/artists/playlists) or ``"track"`` (track rows). Either
    ``set_function(fn)`` (paged ``offset, limit -> list``) or ``set_items(list)``
    is called before ``load()``.

    Phase 7 year filter (``year_filter=True``): renders a ``Gtk.DropDown`` of
    "All years" + ``album_years()`` in the header. Changing the selection resets
    the auto-load with a year-filtered fetch fn. The caller supplies the
    year-aware fetch via :meth:`set_year_function` (``fn(year, offset, limit)``).
    ``preselect_year`` opens the page already filtered to that year (Explore →
    Years). The index<->year mapping uses the tested ``year_filter`` helper.

    **Virtualized track lists** (``item_type="track"``): the genuinely-large
    flat track lists (Collection "Tracks" full list) render through a
    :class:`HTTrackListView` (``Gtk.ListView`` + ``Gio.ListStore`` + recycling
    factory) instead of the capped widget-per-row :class:`HTAutoLoadWidget`.
    The whole list is loaded into the model up front (cheap — dicts, not
    widgets) and the 500-row cap is gone; only the visible rows are realized.
    Card lists (albums/artists, incl. the year-filter) stay on the auto-load
    widget — they were never the memory offender (the perf audit measured the
    +870 MB on the track-row widgets).
    """

    __gtype_name__ = "HTFromFunctionPage"

    def __init__(self, title="", item_type="card", year_filter=False,
                 preselect_year=None):
        IDisconnectable.__init__(self)
        super().__init__()

        self.set_title(title)
        # The page content is built immediately; only the data fetch is async.
        self.content_stack.set_visible_child_name("content")

        self._item_type = item_type
        self._virtualized = item_type == "track"
        self._year_filter = year_filter
        self._preselect_year = preselect_year
        self._year_function = None  # fn(year, offset, limit) -> list
        self._function = None       # fn(offset, limit) -> list (track path)
        self._years = []
        self._dropdown = None
        # Full track list, fetched on the worker thread in _load_async and
        # spliced into the ListView model on the main loop in _load_finish.
        self._tracks = []

        if year_filter:
            self._build_year_dropdown()

        if self._virtualized:
            self._build_track_list_view()
        else:
            self.auto_load = HTAutoLoadWidget(
                item_type=item_type,
                margin_start=12, margin_end=12, margin_top=12, margin_bottom=12,
            )
            self.auto_load.set_scrolled_window(self.scrolled_window)
            self.append(self.auto_load)

    # ------------------------------------------------------------------ #
    # Virtualized track list (Gtk.ListView)                              #
    # ------------------------------------------------------------------ #

    def _build_track_list_view(self):
        """Mount the ListView and hand scrolling to it.

        The page template wraps ``_content`` in an outer ScrolledWindow. A
        ``Gtk.ListView`` only virtualizes when its viewport height is bounded —
        nesting it in another scroller would give it unbounded height and
        realize every row (defeating the whole feature). So we pin the OUTER
        scroller (no scrolling, don't grow to natural height) and let the
        ListView's own internal ScrolledWindow own the bounded viewport +
        scrolling. The content Box is vexpand:true, so the ListView fills the
        ToolbarView's bounded content area.
        """
        self.scrolled_window.set_policy(
            Gtk.PolicyType.NEVER, Gtk.PolicyType.NEVER
        )
        self.scrolled_window.set_propagate_natural_height(False)

        self.list_view = HTTrackListView(
            margin_start=12, margin_end=12, margin_top=12, margin_bottom=12,
        )
        self.signals.append((
            self.list_view,
            self.list_view.connect("track-activated", self._on_track_activated),
        ))
        self.append(self.list_view)

    def disconnect_all(self, *args):
        """Tear the page down, then unparent the ListView so its recycled-row
        pool disposes.

        ``Page.disconnect_all`` recurses into ``self.list_view`` (which drops the
        model + factory so the ListView tears down every pooled row), but the
        ListView itself stays parented in ``self.content``. Because the page is
        Python-collected on pop, that C-side subtree (content → ListView →
        recycle pool → HTTrackRowWidget rows → their link labels) is otherwise
        pinned for the life of the process — exactly the C-side leak the
        instance-count gate caught (~20 rows + 60 link labels per push/pop).
        Removing the ListView from ``self.content`` breaks the chain so GTK
        finalizes the whole subtree, mirroring ``TrackListPage.disconnect_all``.
        """
        super().disconnect_all(*args)
        lv = getattr(self, "list_view", None)
        if lv is not None and lv.get_parent() is not None:
            self.content.remove(lv)
        self.list_view = None

    def _on_track_activated(self, _view, index):
        # Play from the activated index with the FULL list as context. The play
        # context comes straight from the model dicts — no rows are realized to
        # build it (force-realizing all rows is exactly what we avoid).
        tracks = self.list_view.get_tracks()
        if 0 <= index < len(tracks):
            utils.player_object.play_this(tracks, index)

    # ------------------------------------------------------------------ #
    # Year filter                                                        #
    # ------------------------------------------------------------------ #

    def _build_year_dropdown(self):
        """Header DropDown of "All years" + album_years(); resets on change."""
        self._years = utils.db.album_years() if utils.db is not None else []
        labels = yf.dropdown_labels(self._years)
        self._dropdown = Gtk.DropDown.new_from_strings(labels)
        self._dropdown.set_halign(Gtk.Align.START)
        self._dropdown.set_margin_start(12)
        self._dropdown.set_margin_top(12)
        # Preselect (Explore → Years opens already filtered).
        self._dropdown.set_selected(
            yf.year_to_index(self._years, self._preselect_year)
        )
        self.signals.append((
            self._dropdown,
            self._dropdown.connect("notify::selected", self._on_year_changed),
        ))
        self.content.append(self._dropdown)

    def _on_year_changed(self, dropdown, _pspec):
        if self._year_function is None:
            return
        year = yf.index_to_year(self._years, dropdown.get_selected())
        self._apply_year(year)
        if self._virtualized:
            # Swap the model contents wholesale to the year-filtered list (db
            # read on a worker; the splice runs back on the main loop).
            self._refetch_tracks_async()
        else:
            self.auto_load.reset()
            self.auto_load.th_load_items()

    def _apply_year(self, year):
        fn = lambda offset, limit: self._year_function(year, offset, limit)
        if self._virtualized:
            self._function = fn
        else:
            self.auto_load.set_function(fn)

    def _refetch_tracks_async(self):
        """Re-fill the ListView model from ``self._function`` off-thread.

        Used by the year filter (and any future model swap). The db read runs
        on a worker (CLAUDE.md forbids db on the main thread); the splice lands
        on the main loop via the owner-guarded ``run_async`` callback.
        """
        fn = self._function
        if fn is None:
            return

        def work():
            return list(fn(0, _FULL_FETCH_LIMIT))

        def done(tracks):
            self._tracks = tracks
            self.list_view.set_tracks(tracks)

        utils.run_async(work, on_done=done, owner=self)

    def set_year_function(self, function) -> None:
        """Set the year-aware fetch ``fn(year, offset, limit) -> list``.

        Binds the fetch to the currently-selected year immediately so the first
        ``load()`` honors any ``preselect_year``.
        """
        self._year_function = function
        if self._dropdown is not None:
            year = yf.index_to_year(self._years, self._dropdown.get_selected())
        else:
            year = self._preselect_year
        self._apply_year(year)

    def _load_async(self) -> None:
        # Runs on the worker thread (Page.load). Track path: fetch the WHOLE
        # list into a plain dict list (cheap) for the ListView model. Card path:
        # the auto-load widget fetches the first slice + marshals widget build.
        if self._virtualized:
            if self._function is not None:
                self._tracks = list(self._function(0, _FULL_FETCH_LIMIT))
        else:
            self.auto_load.th_load_items()

    def _load_finish(self) -> None:
        # Main loop: fill the ListView model with the worker-fetched list. Only
        # the visible rows are realized; the model holds every dict (no cap).
        if self._virtualized:
            self.list_view.set_tracks(self._tracks)

    def set_function(self, function) -> None:
        if self._virtualized:
            self._function = function
        else:
            self.auto_load.set_function(function)

    def set_items(self, items) -> None:
        if self._virtualized:
            # Direct item list (no paged fetch fn): fill the model immediately.
            self._tracks = list(items or [])
            GLib.idle_add(self._idle_set_tracks)
        else:
            self.auto_load.set_items(items)

    def _idle_set_tracks(self):
        if utils._owner_alive(self):
            self.list_view.set_tracks(self._tracks)
        return False
