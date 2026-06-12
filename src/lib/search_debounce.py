# search_debounce.py
#
# Copyright (C) 2026 Tyler Reece
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Pure (gi-free) debounce coordinator for the Explore search entry.

The search entry coalesces rapid keystrokes into a single query fire after a
quiet delay (250ms), and only fires for queries with at least ``min_chars``
non-whitespace characters. The scheduler + cancel are injectable so the
coalescing / cancel semantics are unit-testable with no GLib main loop — the
Explore page injects ``GLib.timeout_add`` / ``GLib.source_remove``.

Lifecycle:
* ``submit(query)`` — called on every ``search-changed``. Cancels any pending
  fire (coalescing), then either schedules a new fire (>= min_chars) or stays
  cancelled (too short / empty).
* ``cancel()`` — drop any pending fire. Called on owner teardown / dialog close
  so a late timeout never calls back into a destroyed page.

The fire callback receives the *trimmed* query.
"""


def _noop(*_args):
    """Inert fire callback installed by ``dispose`` to drop the page ref."""
    return None


class SearchDebouncer:
    """Coalesces search-entry changes into one delayed, min-length fire."""

    def __init__(self, on_fire, min_chars=2, delay=250,
                 schedule=None, cancel=None):
        """
        Args:
            on_fire: ``callable(query)`` invoked once per quiet period.
            min_chars: minimum non-whitespace length to fire (else cancel).
            delay: debounce delay passed to the scheduler (ms).
            schedule: ``callable(delay, fn) -> handle`` — argument order
                matches ``GLib.timeout_add(interval, function)``, which the
                Explore page injects directly.
            cancel: ``callable(handle)`` (e.g. ``GLib.source_remove``).
        """
        self._on_fire = on_fire
        self._min_chars = min_chars
        self._delay = delay
        self._schedule = schedule
        self._cancel = cancel
        self._handle = None

    def submit(self, query):
        """Handle a new query value; (re)schedule or cancel the pending fire."""
        self._cancel_pending()
        text = (query or "").strip()
        if len(text) < self._min_chars:
            return
        # GLib.timeout_add order: interval FIRST, then callback. Swapping these
        # raises "TypeError: Must be number, not function" on every keystroke.
        self._handle = self._schedule(self._delay, lambda: self._fire(text))

    def cancel(self):
        """Drop any pending fire (owner teardown / dialog close)."""
        self._cancel_pending()

    def dispose(self):
        """Cancel and drop the fire callback reference.

        Called on owner teardown so the debouncer no longer holds a (bound
        method -> page) reference that could keep a popped page alive past gc.
        After dispose ``submit`` is a no-op.
        """
        self._cancel_pending()
        self._on_fire = _noop

    # ------------------------------------------------------------------ #

    def _cancel_pending(self):
        if self._handle is not None:
            if self._cancel is not None:
                self._cancel(self._handle)
            self._handle = None

    def _fire(self, text):
        self._handle = None
        self._on_fire(text)
        return False  # one-shot: tell a GLib timeout source not to repeat
