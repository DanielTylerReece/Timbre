# queue_widget.py
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

"""Queue tab (Phase 4 + Phase 9 queue management).

Reads ``player.queue.tracks`` + ``player.queue.current_index`` and splits them
into "Played Songs" (before current, dimmed) and "Next Songs" (current + after,
current highlighted). Rows are slim ``JTQueueRow`` widgets. Activating a row
jumps the player to that queue index.

Phase 9 adds, on top of the read-only Phase 4 tab:

* **Drag-to-reorder** — rows are GtkDragSource/GtkDropTarget; a drop emits the
  row's ``move-requested`` which this widget routes to ``player.move`` (which
  recomputes gapless prefetch + emits ``songs-list-changed``).
* **Per-row remove** — a hover remove button emits ``remove-requested`` →
  ``player.remove_at`` (remove-current advances playback per PlayQueue's pinned
  semantics; remove-other is a silent edit).
* **Toolbar** — shuffle toggle, repeat cycle, and a clear-queue button. The
  shuffle/repeat toggles mirror the transport controls two-way: this widget
  drives the player and listens to ``notify::shuffle`` / ``notify::repeat-type``
  to reflect external changes, blocking its own handlers while syncing so there
  is no feedback loop.

The middle "Queue" section from upstream (explicit add-to-queue items) stays
hidden — Timbre folds add-next/append into the Next Songs list.
"""

from gettext import gettext as _

from gi.repository import Gtk

from ..lib.play_queue import RepeatType
from ..widgets.queue_row import JTQueueRow


@Gtk.Template(resource_path="/io/github/tylerreece/timbre/ui/widgets/queue_widget.ui")
class HTQueueWidget(Gtk.Box):
    """Displays the play queue split by current index, with editing controls."""

    __gtype_name__ = "HTQueueWidget"

    played_songs_list = Gtk.Template.Child()
    queued_songs_list = Gtk.Template.Child()
    next_songs_list = Gtk.Template.Child()

    played_songs_box = Gtk.Template.Child()
    queued_songs_box = Gtk.Template.Child()
    next_songs_box = Gtk.Template.Child()

    shuffle_toggle = Gtk.Template.Child()
    repeat_toggle = Gtk.Template.Child()
    clear_button = Gtk.Template.Child()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._player = None
        self._toolbar_wired = False
        # Handler ids for the toolbar buttons (to block while syncing) and for
        # the player signals we listen to (so external state changes reflect).
        self._shuffle_handler = None
        self._repeat_handler = None
        self._clear_handler = None
        self._player_signals = []

        # The explicit add-to-queue section is unused.
        self.queued_songs_box.set_visible(False)
        self.played_songs_list.connect("row-activated", self._on_row_activated)
        self.next_songs_list.connect("row-activated", self._on_row_activated)

    # ------------------------------------------------------------------ #
    # Rebuild                                                            #
    # ------------------------------------------------------------------ #

    def update_all(self, player) -> None:
        """Rebuild both lists from the player's queue snapshot."""
        self._player = player
        self._ensure_toolbar_wired(player)

        tracks = player.queue.tracks
        current = player.queue.current_index

        played = tracks[:current] if current >= 0 else []
        next_tracks = tracks[current:] if current >= 0 else tracks
        # Absolute index of the first "next" row.
        next_base = current if current >= 0 else 0

        self._fill(self.played_songs_list, self.played_songs_box, played, 0, current)
        self._fill(
            self.next_songs_list,
            self.next_songs_box,
            next_tracks,
            next_base,
            current,
        )
        self._sync_toolbar(player)

    # Kept for compatibility with callers that update sections individually.
    def update_played_songs(self, player) -> None:
        self.update_all(player)

    def update_queue(self, player) -> None:
        self.update_all(player)

    def update_next_songs(self, player) -> None:
        self.update_all(player)

    def _fill(self, list_box, box, tracks, base_index, current_index) -> None:
        self._clear(list_box)
        if not tracks:
            box.set_visible(False)
            return
        box.set_visible(True)
        for offset, track in enumerate(tracks):
            abs_index = base_index + offset
            row = JTQueueRow(
                track, index=abs_index, is_current=(abs_index == current_index)
            )
            row.connect("move-requested", self._on_move_requested)
            row.connect("remove-requested", self._on_remove_requested)
            list_box.append(row)

    @staticmethod
    def _clear(list_box) -> None:
        child = list_box.get_row_at_index(0)
        while child:
            # Drop the row's DnD/remove signal handlers before removing it so
            # the controllers don't keep the row (or this widget) alive.
            if isinstance(child, JTQueueRow):
                child.teardown()
            list_box.remove(child)
            child = list_box.get_row_at_index(0)

    # ------------------------------------------------------------------ #
    # Row activation / editing                                           #
    # ------------------------------------------------------------------ #

    def _on_row_activated(self, list_box, row) -> None:
        if self._player is None or not isinstance(row, JTQueueRow):
            return
        self._player.load_at_index(row.index)

    def _on_move_requested(self, _row, from_index: int, to_index: int) -> None:
        if self._player is None:
            return
        self._player.move(from_index, to_index)
        # player.move emits songs-list-changed → window refreshes the tab.

    def _on_remove_requested(self, _row, index: int) -> None:
        if self._player is None:
            return
        self._player.remove_at(index)
        # player.remove_at emits songs-list-changed → window refreshes the tab.

    # ------------------------------------------------------------------ #
    # Toolbar (shuffle / repeat / clear) — two-way state sync            #
    # ------------------------------------------------------------------ #

    def _ensure_toolbar_wired(self, player) -> None:
        if self._toolbar_wired:
            return
        self._toolbar_wired = True
        self._shuffle_handler = self.shuffle_toggle.connect(
            "toggled", self._on_shuffle_toggled
        )
        self._repeat_handler = self.repeat_toggle.connect(
            "clicked", self._on_repeat_clicked
        )
        self._clear_handler = self.clear_button.connect(
            "clicked", self._on_clear_clicked
        )
        # Reflect external transport-driven state changes back onto the toolbar.
        self._player_signals.append(
            player.connect("notify::shuffle", lambda *_a: self._sync_toolbar(player))
        )
        self._player_signals.append(
            player.connect(
                "notify::repeat-type", lambda *_a: self._sync_toolbar(player)
            )
        )

    def _sync_toolbar(self, player) -> None:
        """Reflect player shuffle/repeat onto the toggles without feedback.

        The shuffle toggle's ``toggled`` handler is blocked while we set its
        active state (compare-and-block) so syncing player→UI never re-fires
        UI→player. The repeat button is a plain Button (manual icon), so it
        only needs its icon/tooltip refreshed.
        """
        if self._shuffle_handler is not None:
            self.shuffle_toggle.handler_block(self._shuffle_handler)
            self.shuffle_toggle.set_active(player.shuffle)
            self.shuffle_toggle.handler_unblock(self._shuffle_handler)
        self._update_repeat_icon(player.repeat_type)

    def _update_repeat_icon(self, repeat_type) -> None:
        match RepeatType(repeat_type):
            case RepeatType.LIST:
                self.repeat_toggle.set_icon_name("media-playlist-repeat-symbolic")
                self.repeat_toggle.add_css_class("accent")
                self.repeat_toggle.set_tooltip_text(_("Repeat all"))
            case RepeatType.SONG:
                self.repeat_toggle.set_icon_name("playlist-repeat-song-symbolic")
                self.repeat_toggle.add_css_class("accent")
                self.repeat_toggle.set_tooltip_text(_("Repeat one"))
            case _:
                self.repeat_toggle.set_icon_name(
                    "media-playlist-consecutive-symbolic"
                )
                self.repeat_toggle.remove_css_class("accent")
                self.repeat_toggle.set_tooltip_text(_("Repeat"))

    def _on_shuffle_toggled(self, btn) -> None:
        if self._player is None:
            return
        self._player.shuffle = btn.get_active()

    def _on_repeat_clicked(self, _btn) -> None:
        if self._player is None:
            return
        rt = self._player.repeat_type
        if rt == RepeatType.NONE:
            self._player.repeat_type = RepeatType.LIST
        elif rt == RepeatType.LIST:
            self._player.repeat_type = RepeatType.SONG
        else:
            self._player.repeat_type = RepeatType.NONE

    def _on_clear_clicked(self, _btn) -> None:
        if self._player is None:
            return
        self._player.clear_queue()
        self.update_all(self._player)
