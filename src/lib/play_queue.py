# play_queue.py
#
# Copyright (C) 2026 Tyler Reece
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Pure-python play-queue / shuffle / repeat logic.

No ``gi`` imports — this is the headless, exhaustively-testable core that the
GStreamer ``PlayerObject`` delegates *every* queue decision to. Phase 9's
drag-reorder UI also operates on this class.

Behavioral notes (ported from High Tide ``player_object.py``):

* **SONG repeat vs. user next.** When the *gapless / automatic* advance fires
  (a track finished on its own), SONG repeat replays the current track. When
  the *user* presses next, the current track is skipped even under SONG repeat.
  This is a deliberate DIVERGENCE from upstream High Tide, whose
  ``play_next(gapless=False)`` re-seeks to 0 and does NOT advance the queue
  under SONG repeat; Timbre chose the conventional repeat-one-plus-
  explicit-skip behavior instead. We model this with the ``user`` flag on
  :meth:`next`: ``user=True`` (the default, a deliberate user action)
  advances; ``user=False`` (auto/gapless) honors SONG repeat.

* **Shuffle.** Toggling shuffle on floats the current track to the front and
  shuffles the remainder; the original order is retained so toggling shuffle
  off restores it. The current track is preserved across the round trip.

* **peek_next()** returns exactly what the *gapless* advance (``next(user=
  False)``) would play next, honoring repeat + shuffle, without mutating state.
  Gapless prefetch in the player relies on this consistency.
"""

import json
import random
import threading
from enum import IntEnum


class RepeatType(IntEnum):
    NONE = 0
    SONG = 1
    LIST = 2


# --------------------------------------------------------------------------- #
# Resume-state (de)serialization — pure, gi-free, lives here with the rest of  #
# the headless queue logic so it can be unit-tested without importing gi.      #
# --------------------------------------------------------------------------- #

# Current resume-state schema version (bumped if the blob shape changes).
_STATE_VERSION = 1


def serialize_player_state(track_ids, current_index, position, shuffle) -> str:
    """Serialize resume state to a compact JSON string.

    ``track_ids`` is the queue in CURRENT (live) order — when shuffle is on this
    is the shuffled order, which is exactly what must be restored so playback
    continues from the same point in the same sequence. ``position`` is the
    playback position in seconds. ``current_index`` indexes ``track_ids``.
    """
    return json.dumps(
        {
            "v": _STATE_VERSION,
            "track_ids": [str(t) for t in (track_ids or [])],
            "current_index": int(current_index),
            "position": float(position or 0.0),
            "shuffle": bool(shuffle),
        }
    )


def deserialize_player_state(blob):
    """Parse a resume-state JSON blob into a normalized dict, or None.

    Returns ``None`` for empty/corrupt input (bad JSON, wrong shape) so the
    caller starts clean instead of crashing. On success returns a dict with
    keys ``track_ids`` (list of str), ``current_index`` (int, clamped into the
    bounds of ``track_ids``; -1 for an empty queue), ``position`` (float >= 0),
    ``shuffle`` (bool). Library-membership of ids is NOT checked here (that is
    resolved at the player boundary).
    """
    if not blob:
        return None
    try:
        data = json.loads(blob)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    raw_ids = data.get("track_ids")
    if not isinstance(raw_ids, list):
        return None
    track_ids = [str(t) for t in raw_ids if t is not None and str(t)]
    try:
        current_index = int(data.get("current_index", 0))
    except (ValueError, TypeError):
        current_index = 0
    try:
        position = float(data.get("position", 0.0) or 0.0)
    except (ValueError, TypeError):
        position = 0.0
    if position < 0:
        position = 0.0
    shuffle = bool(data.get("shuffle", False))
    if track_ids:
        current_index = max(0, min(current_index, len(track_ids) - 1))
    else:
        current_index = -1
    return {
        "track_ids": track_ids,
        "current_index": current_index,
        "position": position,
        "shuffle": shuffle,
    }


class PlayQueue:
    """Ordered list of tracks with a current pointer, shuffle and repeat.

    A "track" is any object with an ``.id`` attribute; PlayQueue never reaches
    for anything else, so Phase 1 ``Track`` dataclasses or test stubs both work.

    **Thread safety.** All public methods are thread-safe: every public
    mutating/reading method acquires ``self._lock`` (a ``threading.RLock``).
    This guards against the GStreamer ``about-to-finish`` race, where the
    streaming thread calls :meth:`next` (``user=False``) concurrently with
    main-thread mutations like the shuffle setter (which non-atomically
    reassigns ``_tracks``/``_current_index``). RLock is used so internal
    cross-calls (e.g. setters calling ``_apply_shuffle``, ``current`` reads
    from within other methods) do not self-deadlock.
    """

    def __init__(self):
        # Reentrant lock: public methods that call other public methods (or the
        # ``current`` accessor) must not deadlock against themselves.
        self._lock = threading.RLock()
        self._tracks = []
        self._current_index = -1
        self._repeat_type = RepeatType.NONE
        self._shuffle = False
        # Original (un-shuffled) order, kept so shuffle can be reversed.
        self._original_order = []

    # ------------------------------------------------------------------ #
    # Read-only views                                                    #
    # ------------------------------------------------------------------ #

    @property
    def tracks(self):
        """The live ordered track list (shuffled order when shuffle is on)."""
        with self._lock:
            # Return a snapshot copy: callers iterating this list must not see
            # it mutate mid-iteration from another thread.
            return list(self._tracks)

    @property
    def current_index(self):
        with self._lock:
            return self._current_index

    @property
    def current(self):
        with self._lock:
            if 0 <= self._current_index < len(self._tracks):
                return self._tracks[self._current_index]
            return None

    # ------------------------------------------------------------------ #
    # Repeat / shuffle properties                                        #
    # ------------------------------------------------------------------ #

    @property
    def repeat_type(self):
        with self._lock:
            return self._repeat_type

    @repeat_type.setter
    def repeat_type(self, value):
        with self._lock:
            self._repeat_type = RepeatType(value)

    @property
    def shuffle(self):
        with self._lock:
            return self._shuffle

    @shuffle.setter
    def shuffle(self, value):
        with self._lock:
            value = bool(value)
            if value == self._shuffle:
                return
            self._shuffle = value
            self._apply_shuffle()

    def _apply_shuffle(self):
        """Rebuild ``_tracks`` for the current shuffle state, keeping current."""
        cur = self.current
        if self._shuffle:
            # Preserve the original order for later un-shuffle.
            self._original_order = list(self._tracks)
            remaining = [t for t in self._tracks if t is not cur]
            random.shuffle(remaining)
            self._tracks = ([cur] if cur is not None else []) + remaining
        else:
            # Restore original order if we have it (else leave as-is).
            if self._original_order:
                self._tracks = list(self._original_order)
            self._original_order = []
        if cur is not None:
            self._current_index = self._tracks.index(cur)
        elif self._tracks:
            self._current_index = 0
        else:
            self._current_index = -1

    # ------------------------------------------------------------------ #
    # Loading tracks                                                     #
    # ------------------------------------------------------------------ #

    def set_tracks(self, tracks, start_index=0):
        """Replace the queue with ``tracks`` and start at ``start_index``."""
        with self._lock:
            self._tracks = list(tracks)
            self._original_order = []
            if not self._tracks:
                self._current_index = -1
                return
            start_index = max(0, min(start_index, len(self._tracks) - 1))
            self._current_index = start_index
            if self._shuffle:
                self._apply_shuffle()

    def clear(self):
        with self._lock:
            self._tracks = []
            self._original_order = []
            self._current_index = -1

    # ------------------------------------------------------------------ #
    # Navigation                                                         #
    # ------------------------------------------------------------------ #

    def _next_index(self, user):
        """Index that an advance would move to, or None if it stops.

        ``user`` mirrors :meth:`next`'s flag. Does not mutate.
        """
        if not self._tracks:
            return None
        # SONG repeat: auto-advance repeats current; user-advance moves on.
        if self._repeat_type == RepeatType.SONG and not user:
            return self._current_index
        nxt = self._current_index + 1
        if nxt < len(self._tracks):
            return nxt
        if self._repeat_type == RepeatType.LIST:
            return 0
        return None  # NONE (and SONG under user-next) stop at the end

    def next(self, user=True):
        """Advance to the next track and return it (or None if stopping).

        Args:
            user: True for an explicit user "next" (skips current even under
                SONG repeat); False for an automatic / gapless advance (honors
                SONG repeat by repeating the current track).
        """
        with self._lock:
            idx = self._next_index(user)
            if idx is None:
                return None
            self._current_index = idx
            return self.current

    def peek_next(self):
        """Return what the gapless (auto) advance would play, without moving.

        Consistent with ``next(user=False)`` across every repeat/shuffle mode.
        """
        with self._lock:
            idx = self._next_index(user=False)
            if idx is None:
                return None
            return self._tracks[idx]

    def previous(self):
        """Step backward and return the track (or None at the start)."""
        with self._lock:
            if not self._tracks:
                return None
            prev = self._current_index - 1
            if prev < 0:
                if self._repeat_type == RepeatType.LIST:
                    prev = len(self._tracks) - 1
                else:
                    return None
            self._current_index = prev
            return self.current

    def jump_to(self, index):
        """Set current to ``index`` and return it; None if out of range."""
        with self._lock:
            if not (0 <= index < len(self._tracks)):
                return None
            self._current_index = index
            return self.current

    # ------------------------------------------------------------------ #
    # Mutation (queue editing)                                           #
    # ------------------------------------------------------------------ #

    def append(self, track):
        """Add a track to the end of the queue."""
        with self._lock:
            self._tracks.append(track)
            if self._original_order:
                self._original_order.append(track)
            if self._current_index < 0:
                self._current_index = 0

    def add_next(self, track):
        """Insert a track to play immediately after the current one."""
        with self._lock:
            if self._current_index < 0:
                self._tracks.insert(0, track)
                self._current_index = 0
            else:
                self._tracks.insert(self._current_index + 1, track)
            if self._original_order:
                self._original_order.append(track)

    def remove(self, index):
        """Remove the track at ``index``, keeping the current pointer valid.

        Removing a track before the current decrements the index so it keeps
        pointing at the same object. Removing the current track advances the
        pointer to the following track (clamped at the end).
        """
        with self._lock:
            if not (0 <= index < len(self._tracks)):
                return
            removed = self._tracks.pop(index)
            if self._original_order:
                try:
                    self._original_order.remove(removed)
                except ValueError:
                    pass
            if not self._tracks:
                self._current_index = -1
                return
            if index < self._current_index:
                self._current_index -= 1
            elif index == self._current_index:
                # Stay at the same slot (now the following track); clamp to end.
                self._current_index = min(
                    self._current_index, len(self._tracks) - 1
                )

    def move(self, from_index, to_index):
        """Move a track within the queue, keeping the current pointer on its
        object."""
        with self._lock:
            n = len(self._tracks)
            if not (0 <= from_index < n) or not (0 <= to_index < n):
                return
            cur = self.current
            track = self._tracks.pop(from_index)
            self._tracks.insert(to_index, track)
            if cur is not None:
                self._current_index = self._tracks.index(cur)
