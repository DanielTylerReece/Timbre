# queue_row.py
#
# Copyright (C) 2026 Tyler Reece
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Slim queue row (Phase 4, drag-to-reorder + remove in Phase 9).

Consumes a Phase 1 ``Track`` dataclass OR a db dict (``.name``/``["name"]``,
``.artist_name``, ``.duration_ticks``). Shows title, artist, duration, a
"now playing" indicator + highlight for the current row, a hover remove button,
and a drag handle. The full HTGenericTrackWidget port (context menu, album art,
etc.) is deferred to a later phase per the Phase 4 decision.

**Drag-to-reorder (Phase 9).** Each row is both a ``GtkDragSource`` (carries its
own queue index as an ``int`` payload) and a ``GtkDropTarget`` (accepts an
``int`` and emits ``move-requested(from_index, to_index)``). The queue widget
owns the move; the row only reports intent. Row-activate (click → jump) is not
disturbed: GTK's drag threshold separates a click from a drag, so a plain click
still activates the row.

**Signal hygiene.** The DragSource / DropTarget / remove-button connections are
recorded so :meth:`teardown` can drop them when the row is cleared from the list
(the queue widget calls it before removing rows), keeping the queue tab
leak-safe across rebuilds.
"""

from gettext import gettext as _

from gi.repository import GObject, Gdk, Gtk

from ..lib import utils


def _g(obj, attr, default=None):
    if isinstance(obj, dict):
        return obj.get(attr, default)
    return getattr(obj, attr, default)


@Gtk.Template(resource_path="/io/github/tylerreece/timbre/ui/widgets/queue_row.ui")
class JTQueueRow(Gtk.ListBoxRow):
    """A single slim queue row bound to a track-like object."""

    __gtype_name__ = "JTQueueRow"

    __gsignals__ = {
        # (from_index, to_index) — request the queue widget reorder.
        "move-requested": (GObject.SignalFlags.RUN_FIRST, None, (int, int)),
        # (index,) — request removal of this row from the queue.
        "remove-requested": (GObject.SignalFlags.RUN_FIRST, None, (int,)),
    }

    drag_handle = Gtk.Template.Child()
    now_playing_icon = Gtk.Template.Child()
    title_label = Gtk.Template.Child()
    artist_label = Gtk.Template.Child()
    duration_label = Gtk.Template.Child()
    remove_button = Gtk.Template.Child()

    # The queue index this row represents (used to jump on activation).
    index = GObject.Property(type=int, default=0)

    def __init__(self, track, index: int = 0, is_current: bool = False):
        super().__init__()
        self.track = track
        self.index = index
        # (object, handler_id) pairs to drop in teardown().
        self._signals = []

        self.title_label.set_label(_g(track, "name") or _("Unknown"))
        artist = _g(track, "artist_name") or _("Unknown artist")
        self.artist_label.set_label(artist)

        ticks = _g(track, "duration_ticks") or 0
        self.duration_label.set_label(utils.pretty_duration(ticks // 10_000_000))

        self.set_current(is_current)

        self._signals.append(
            (
                self.remove_button,
                self.remove_button.connect("clicked", self._on_remove_clicked),
            )
        )
        self._setup_dnd()

    # ------------------------------------------------------------------ #
    # Drag-to-reorder                                                    #
    # ------------------------------------------------------------------ #

    def _setup_dnd(self) -> None:
        """Wire a DragSource (this row's index) + DropTarget (accepts an index).

        The DragSource lives on the drag handle so a press-drag on the handle
        starts a reorder while a press-release elsewhere on the row still
        activates it (jump). The DropTarget covers the whole row so a drop
        anywhere on a row targets that row's slot.
        """
        drag = Gtk.DragSource.new()
        drag.set_actions(Gdk.DragAction.MOVE)
        self._signals.append((drag, drag.connect("prepare", self._on_drag_prepare)))
        self._signals.append((drag, drag.connect("drag-begin", self._on_drag_begin)))
        # drag-end fires on every completed drag (drop or cancel); drag-cancel
        # fires additionally when the drag is aborted (e.g. Esc / dropped on no
        # target). Both clear the dimming so a cancelled drag doesn't leave the
        # row stuck at queue-dragging opacity.
        self._signals.append((drag, drag.connect("drag-end", self._on_drag_end)))
        self._signals.append((drag, drag.connect("drag-cancel", self._on_drag_cancel)))
        self.drag_handle.add_controller(drag)
        self._drag_source = drag

        drop = Gtk.DropTarget.new(GObject.TYPE_INT, Gdk.DragAction.MOVE)
        self._signals.append((drop, drop.connect("drop", self._on_drop)))
        self._signals.append((drop, drop.connect("enter", self._on_drop_enter)))
        self._signals.append((drop, drop.connect("leave", self._on_drop_leave)))
        self.add_controller(drop)
        self._drop_target = drop

    def _on_drag_prepare(self, _source, _x, _y):
        return Gdk.ContentProvider.new_for_value(int(self.index))

    def _on_drag_begin(self, source, drag):
        # Use a snapshot of the row as the drag icon for clear visual feedback.
        paintable = Gtk.WidgetPaintable.new(self)
        source.set_icon(paintable, 0, 0)
        self.add_css_class("queue-dragging")

    def _on_drag_end(self, _source, _drag, _delete_data):
        # Always clear the drag dimming when the drag finishes, regardless of
        # whether it ended in a drop or was cancelled.
        self.remove_css_class("queue-dragging")

    def _on_drag_cancel(self, _source, _drag, _reason):
        # A cancelled drag (Esc, no valid target) also lands here; clear the
        # dimming so the row isn't left visually stuck. Return False to let GTK
        # run its default cancel animation.
        self.remove_css_class("queue-dragging")
        return False

    def _on_drop(self, _target, value, _x, _y):
        self.remove_css_class("queue-drop-into")
        try:
            from_index = int(value)
        except (TypeError, ValueError):
            return False
        if from_index == self.index:
            return False
        self.emit("move-requested", from_index, self.index)
        return True

    def _on_drop_enter(self, _target, _x, _y):
        self.add_css_class("queue-drop-into")
        return Gdk.DragAction.MOVE

    def _on_drop_leave(self, _target):
        self.remove_css_class("queue-drop-into")

    # ------------------------------------------------------------------ #
    # Remove                                                             #
    # ------------------------------------------------------------------ #

    def _on_remove_clicked(self, _btn) -> None:
        self.emit("remove-requested", int(self.index))

    # ------------------------------------------------------------------ #
    # Current-row highlight                                              #
    # ------------------------------------------------------------------ #

    def set_current(self, is_current: bool) -> None:
        """Highlight (or un-highlight) this row as the now-playing track."""
        self.now_playing_icon.set_visible(is_current)
        if is_current:
            self.add_css_class("queue-current")
        else:
            self.remove_css_class("queue-current")

    # ------------------------------------------------------------------ #
    # Cleanup                                                            #
    # ------------------------------------------------------------------ #

    def teardown(self) -> None:
        """Disconnect DnD controllers + remove-button signal (leak-safe).

        Called by the queue widget before a row is removed from its list during
        a rebuild, so the GtkDragSource/GtkDropTarget signal handlers don't keep
        the row (or the widget closure) alive.
        """
        for obj, handler_id in self._signals:
            try:
                if obj.handler_is_connected(handler_id):
                    obj.disconnect(handler_id)
            except Exception:
                pass
        self._signals = []
