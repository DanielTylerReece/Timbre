# track_list_view.py
#
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

"""Virtualized track list: ``Gtk.ListView`` + ``Gio.ListStore`` + factory.

Replaces the capped, widget-per-row :class:`HTAutoLoadWidget` for the genuinely
large flat track lists (the Collection "Tracks" full list). The whole track
list — a few thousand plain dicts — lives in a ``Gio.ListStore`` of lightweight
``TrackItem`` wrappers; only the ~visible rows are realized as
:class:`HTTrackRowWidget` instances and recycled by the
``GtkSignalListItemFactory`` as the user scrolls. This caps RSS (the WIDGETS
were the +870 MB cost, not the dicts) and removes the 500-row stopgap.

Why full-fill, not incremental: the dict list is cheap (≈3 k rows ≈ a few MB),
the ListView only builds widgets for visible slots regardless of model size,
and a single up-front fill means ``play_this(full_list, index)`` needs no
realized rows to construct the play context (the model already holds every
dict). Incremental fill would add an edge-reached/auto-load mechanism back for
zero memory benefit. Year filtering swaps the model contents wholesale
(``set_tracks`` again with the filtered list).

CLAUDE.md compliance:
  * No db/network on the main thread — the page fills the model from a worker
    (the page's ``_load_async``); ``set_tracks`` itself only touches the store.
  * Recycled rows fully unbind (art fetch version-guarded, signals shed in
    teardown) so the leak class this codebase fought is not reintroduced.
  * The widget stores NO bound page methods; activation is surfaced via a
    GObject ``track-activated`` signal the page connects through its tracked
    signals list.
"""

import logging

from gi.repository import Gio, GObject, Gtk

from ..disconnectable_iface import IDisconnectable
from .track_row_widget import HTTrackRowWidget

logger = logging.getLogger(__name__)


class TrackItem(GObject.Object):
    """Minimal GObject wrapper holding one track dict for the ListStore.

    The dict (a db row dict or ``Track`` model) is carried verbatim so the page
    can reconstruct the FULL play context from the model without realizing any
    rows (``[item.track for item in store]``).
    """

    __gtype_name__ = "TimbreTrackItem"

    def __init__(self, track):
        super().__init__()
        self.track = track


class HTTrackListView(Gtk.ScrolledWindow, IDisconnectable):
    """Self-scrolling virtualized track list.

    Emits ``track-activated(index)`` when a row is single-clicked/activated; the
    page connects it and calls ``player.play_this(all_tracks, index)`` with the
    full model contents as context.
    """

    __gtype_name__ = "HTTrackListView"

    __gsignals__ = {
        # index of the activated row in the current model.
        "track-activated": (GObject.SignalFlags.RUN_FIRST, None, (int,)),
    }

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        IDisconnectable.__init__(self)

        self.set_vexpand(True)
        self.set_hexpand(True)

        self.store = Gio.ListStore.new(TrackItem)
        # selection-mode none equivalent: Gtk.NoSelection — single-click
        # activates (row activate -> play) with no persistent highlight, exactly
        # matching the old ListBox(selection_mode=NONE) behavior.
        self.selection = Gtk.NoSelection.new(self.store)

        self.factory = Gtk.SignalListItemFactory()
        self.signals.extend([
            (self.factory, self.factory.connect("setup", self._on_setup)),
            (self.factory, self.factory.connect("bind", self._on_bind)),
            (self.factory, self.factory.connect("unbind", self._on_unbind)),
            (self.factory, self.factory.connect("teardown", self._on_teardown)),
        ])

        self.list_view = Gtk.ListView(
            model=self.selection,
            factory=self.factory,
            single_click_activate=True,
            css_classes=["tracks-list-box"],
        )
        self.signals.append((
            self.list_view,
            self.list_view.connect("activate", self._on_activate),
        ))
        self.set_child(self.list_view)

    # ------------------------------------------------------------------ #
    # Model fill                                                          #
    # ------------------------------------------------------------------ #

    def set_tracks(self, tracks):
        """Replace the model contents with ``tracks`` (a list of track dicts).

        Call on the main loop. The full list — even a few thousand rows — is
        cheap because each entry is a plain ``TrackItem`` wrapper, not a widget.
        Uses ``splice`` so the swap is one model mutation (single notify) rather
        than N appends — important when a year filter re-fills a large list.
        """
        items = [TrackItem(t) for t in (tracks or [])]
        self.store.splice(0, self.store.get_n_items(), items)

    def get_tracks(self):
        """Return the FULL list of track dicts currently in the model.

        Used to build the play context without realizing any rows.
        """
        return [
            self.store.get_item(i).track
            for i in range(self.store.get_n_items())
        ]

    def get_n_items(self):
        return self.store.get_n_items()

    # ------------------------------------------------------------------ #
    # Factory                                                            #
    # ------------------------------------------------------------------ #

    def _on_setup(self, _factory, list_item):
        row = HTTrackRowWidget()
        # Stash the row on the list_item: at TEARDOWN time GTK has already
        # unset the child (``list_item.get_child()`` returns None), so reading
        # the child there would silently skip the row's teardown and leak its
        # player ``song-changed`` handler (caught by the instance-count gate).
        # A Python attribute on the (GObject) list_item survives the child
        # unset and gives teardown a reliable handle to the row.
        list_item._timbre_row = row
        list_item.set_child(row)

    def _on_bind(self, _factory, list_item):
        row = list_item.get_child()
        item = list_item.get_item()
        if row is not None and item is not None:
            row.bind(item.track, list_item.get_position())

    def _on_unbind(self, _factory, list_item):
        row = list_item.get_child()
        if row is not None:
            row.unbind()

    def _on_teardown(self, _factory, list_item):
        row = getattr(list_item, "_timbre_row", None) or list_item.get_child()
        if row is not None:
            row.teardown()
        list_item._timbre_row = None

    def _on_activate(self, _list_view, position):
        self.emit("track-activated", position)

    # ------------------------------------------------------------------ #
    # Teardown                                                           #
    # ------------------------------------------------------------------ #

    def disconnect_all(self, *args):
        """Tear down: force the ListView to release its recycled-row pool.

        ``store.remove_all()`` alone is NOT enough — the ``Gtk.ListView`` keeps
        its recycled :class:`HTTrackRowWidget` pool alive C-side (each row holds
        a ``song-changed`` handler on the GLOBAL player), and that pool outlives
        the (Python-collected) page. The instance-count gate caught this:
        ~20 HTTrackRowWidgets accreted per push/pop.

        Dropping the model AND the factory makes the ListView dispose its whole
        recycle pool, firing each row's factory ``teardown`` (→
        ``HTTrackRowWidget.teardown`` → ``disconnect_all``), so every row sheds
        its player handlers and is finalized. Then chain up to clear our own
        factory/list-view/activate handlers.
        """
        self.store.remove_all()
        # Detach the model and factory so the ListView releases (and tears down)
        # every recycled row. Order: model first, then factory — clearing the
        # factory triggers the teardown pass over the now-empty pool.
        self.list_view.set_model(None)
        self.list_view.set_factory(None)
        IDisconnectable.disconnect_all(self, *args)
