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

from gi.repository import Gtk

from ..disconnectable_iface import IDisconnectable
from ..lib import utils
from ..lib import year_filter as yf
from ..widgets import HTAutoLoadWidget
from .page import Page


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
    """

    __gtype_name__ = "HTFromFunctionPage"

    def __init__(self, title="", item_type="card", year_filter=False,
                 preselect_year=None):
        IDisconnectable.__init__(self)
        super().__init__()

        self.set_title(title)
        # The page content is built immediately; only the data fetch is async.
        self.content_stack.set_visible_child_name("content")

        self._year_filter = year_filter
        self._preselect_year = preselect_year
        self._year_function = None  # fn(year, offset, limit) -> list
        self._years = []
        self._dropdown = None

        if year_filter:
            self._build_year_dropdown()

        self.auto_load = HTAutoLoadWidget(
            item_type=item_type,
            margin_start=12, margin_end=12, margin_top=12, margin_bottom=12,
        )
        self.auto_load.set_scrolled_window(self.scrolled_window)
        self.append(self.auto_load)

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
        self.auto_load.reset()
        self._apply_year(year)
        self.auto_load.th_load_items()

    def _apply_year(self, year):
        self.auto_load.set_function(
            lambda offset, limit: self._year_function(year, offset, limit)
        )

    def set_year_function(self, function) -> None:
        """Set the year-aware fetch ``fn(year, offset, limit) -> list``.

        Binds the auto-load to the currently-selected year immediately so the
        first ``load()`` honors any ``preselect_year``.
        """
        self._year_function = function
        if self._dropdown is not None:
            year = yf.index_to_year(self._years, self._dropdown.get_selected())
        else:
            year = self._preselect_year
        self._apply_year(year)

    def _load_async(self) -> None:
        # Runs on the worker thread (Page.load): fetch the first slice. The
        # auto-load widget marshals widget construction back to the main loop.
        self.auto_load.th_load_items()

    def _load_finish(self) -> None:
        ...

    def set_function(self, function) -> None:
        self.auto_load.set_function(function)

    def set_items(self, items) -> None:
        self.auto_load.set_items(items)
