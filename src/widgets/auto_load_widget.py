# auto_load_widget.py
#
# Copyright 2025 Nokse <nokse@posteo.com>
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

import logging
from gettext import gettext as _

from gi.repository import GLib, GObject, Gtk

from ..disconnectable_iface import IDisconnectable
from ..lib import utils
from .card_widget import HTCardWidget, item_kind
from .generic_track_widget import HTGenericTrackWidget

logger = logging.getLogger(__name__)


@Gtk.Template(
    resource_path="/io/github/tylerreece/timbre/ui/widgets/auto_load_widget.ui"
)
class HTAutoLoadWidget(Gtk.Box, IDisconnectable):
    """Infinite-scroll container of browse cards or track rows.

    ``set_function(fn)`` supplies a paged fetch ``fn(offset, limit) -> list`` of
    browse items (Phase 5 convention). Reaching the bottom edge of the host
    scrolled window fetches the next slice on a worker thread. ``item_type``
    (``"card"`` | ``"track"``) chooses the row widget; if unset it is inferred
    from the first item's kind.
    """

    __gtype_name__ = "HTAutoLoadWidget"

    # Hard cap on accreted rows. The widget keeps every built row alive (no
    # virtualization / ListView recycling), so memory + per-row signal fanout
    # grow linearly. Phase 10 measured the full 3197-track library at +870 MB
    # RSS (278 KB/row) -- untenable. Capping at 500 rows holds the worst case to
    # ~138 MB (under the 150 MB budget) and song-changed dispatch to ~1 ms,
    # while still showing a deep, scrollable list. Past the cap the user narrows
    # with search. A true Gtk.ListView rewrite (virtualized rows) is the
    # post-1.0 fix; this is the cheap, measured mitigation. See
    # tests/manual/listview_measure.py and docs/MEMORY-GATES.md.
    MAX_ROWS = 500

    content = Gtk.Template.Child()
    spinner = Gtk.Template.Child()

    def __init__(self, item_type=None, **kwargs) -> None:
        super().__init__(**kwargs)
        IDisconnectable.__init__(self)

        self.function = None
        self.item_type = item_type  # "card" | "track" | None (infer)

        self.parent = None
        self.is_loading = False

        self.items = []
        self.items_limit = 50
        self.items_n = 0
        self._capped = False
        self._footer = None

        self.handler_id = None
        self.scrolled_window = None

    def reset(self):
        self.items = []
        self.items_n = 0
        self.is_loading = False
        self._capped = False
        self._clear_footer()
        # Disconnect + drop the row widgets we own before removing them, else
        # repopulating a live list (e.g. a refresh) leaks every prior row's
        # signal handlers (latent leak fixed in the Phase 5 review).
        for d in self.disconnectables:
            if hasattr(d, "disconnect_all"):
                d.disconnect_all()
        self.disconnectables.clear()
        if self.parent is not None:
            child = self.parent.get_first_child()
            while child:
                self.parent.remove(child)
                child = self.parent.get_first_child()

    def _clear_footer(self):
        if self._footer is not None:
            self.remove(self._footer)
            self._footer = None

    def _show_cap_footer(self):
        """Show the 'showing first N' footer once the row cap is hit."""
        if self._footer is not None:
            return
        self._footer = Gtk.Label(
            label=_("Showing first {} — use search to narrow").format(self.MAX_ROWS),
            css_classes=["dim-label", "caption"],
            margin_top=6, margin_bottom=12, wrap=True, justify=Gtk.Justification.CENTER,
        )
        self.append(self._footer)

    def disconnect_all(self, *args) -> None:
        """Tear the widget down for page pop.

        ``reset()`` first: it disconnects every row's signals AND *removes the
        rows from the parent ListBox/FlowBox*. The base IDisconnectable
        ``disconnect_all`` only disconnects tracked handlers — it never unparents
        the rows, so without this the C-side subtree (ListBox -> rows -> grids ->
        HTLinkLabelWidget) stays mounted and is never disposed even after the
        (Python-collected) page is gone. That left the row link labels alive for
        the life of the process (caught by the instance-count gate). Resetting
        here unparents the rows so GTK can finalize the whole subtree, then we
        chain up to clear our own signals (edge-reached on the scrolled window).
        """
        self.reset()
        IDisconnectable.disconnect_all(self, *args)

    def set_function(self, function) -> None:
        """Set the ``(offset, limit) -> list`` paged fetch function."""
        self.function = function

    def set_items(self, items: list) -> None:
        """Set the initial items to display (replaces any current content).

        Applies the same MAX_ROWS cap as the fetch loop: a giant playlist/album
        track list is truncated to the cap with the footer, so the accreted-rows
        memory cost stays bounded regardless of the entry path.
        """
        self.reset()
        if not items:
            return
        capped = len(items) > self.MAX_ROWS
        self.items = list(items[: self.MAX_ROWS])
        self._infer_type(self.items[0])

        def _add():
            self._append_widgets(self.items)
            self.items_n = len(self.items)
            if capped:
                self._capped = True
                self._show_cap_footer()
            return False

        GLib.idle_add(_add)

    def set_scrolled_window(self, scrolled_window) -> None:
        self.scrolled_window = scrolled_window
        self.handler_id = self.scrolled_window.connect(
            "edge-reached", self._on_edge_reached
        )
        self.signals.append((self.scrolled_window, self.handler_id))

    def _infer_type(self, item):
        if self.item_type is None:
            self.item_type = "track" if item_kind(item) == "track" else "card"

    def th_load_items(self) -> None:
        """Fetch + append the next slice. Safe to call from a worker thread."""
        if self.is_loading or not self.function:
            return
        # Hard row cap (see MAX_ROWS). Once reached, stop fetching and surface
        # the "showing first N" footer; deeper results are reached via search.
        if self.items_n >= self.MAX_ROWS:
            if not self._capped:
                self._capped = True
                self._idle_if_alive(self._show_cap_footer)
            return
        self.is_loading = True
        self._idle_if_alive(lambda: self.spinner.set_visible(True))
        try:
            remaining = self.MAX_ROWS - self.items_n
            new_items = self.function(self.items_n, min(self.items_limit, remaining))
        except TypeError:
            logger.debug("auto-load fetch fn signature mismatch", exc_info=True)
            new_items = []
        except Exception:
            logger.debug("auto-load fetch failed", exc_info=True)
            new_items = []

        if not new_items:
            def _stop():
                if self.scrolled_window is not None and self.handler_id:
                    GObject.signal_handler_block(
                        self.scrolled_window, self.handler_id
                    )
                self.spinner.set_visible(False)
                self.is_loading = False
            self._idle_if_alive(_stop)
            return

        self.items.extend(new_items)
        self._infer_type(new_items[0])

        def _add():
            self._append_widgets(new_items)
            self.items_n += len(new_items)
            self.spinner.set_visible(False)
            self.is_loading = False
            if self.items_n >= self.MAX_ROWS and not self._capped:
                self._capped = True
                self._show_cap_footer()

        self._idle_if_alive(_add)

    def _on_edge_reached(self, scrolled_window, pos):
        if pos == Gtk.PositionType.BOTTOM:
            # Route through run_async with owner=self so the fetch (and its
            # marshalled UI updates) are dropped if the widget is torn down
            # mid-flight — no use-after-free callbacks into a finalized widget.
            utils.run_async(self.th_load_items, owner=self)

    def _idle_if_alive(self, fn):
        """Schedule ``fn`` on the main loop, but skip it if we're gone.

        ``th_load_items`` runs on a worker and marshals its UI mutations back
        with idle callbacks. Guarding each with the same owner-alive check
        ``run_async`` uses keeps a late slice from touching a disposed widget.
        """
        def _guarded():
            if utils._owner_alive(self):
                fn()
            return False
        GLib.idle_add(_guarded)

    def _append_widgets(self, new_items):
        if self.item_type == "track":
            self._add_tracks(new_items)
        else:
            self._add_cards(new_items)

    def _add_tracks(self, new_items):
        if self.parent is None:
            self.parent = Gtk.ListBox(css_classes=["tracks-list-box"])
            self.content.set_child(self.parent)
            self.signals.append((
                self.parent,
                self.parent.connect("row-activated", self._on_tracks_row_selected),
            ))
        base = self.items_n
        for index, track in enumerate(new_items):
            row = HTGenericTrackWidget(track)
            self.disconnectables.append(row)
            row.index = base + index
            self.parent.append(row)

    def _add_cards(self, new_items):
        if self.parent is None:
            self.parent = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE)
            self.content.set_child(self.parent)
        for item in new_items:
            card = HTCardWidget(item)
            self.disconnectables.append(card)
            self.parent.append(card)

    def _on_tracks_row_selected(self, list_box, row):
        utils.player_object.play_this(self.items, row.index)
