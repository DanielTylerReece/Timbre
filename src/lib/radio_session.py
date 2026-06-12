# radio_session.py
#
# Copyright (C) 2026 Tyler Reece
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Queue-low radio-extend state machine (gi-free, headless-testable).

When the user starts a track radio, the window records a :class:`RadioSession`
(the seed track + the ids already served). On each ``song-changed`` it asks
``should_extend(remaining)``; when the queue tail runs low it triggers a worker
``discovery.extend_radio(seed, exclude=served)`` and appends the results. The
re-entrancy guard (``mark_extending`` / ``finish_extending``) stops two
overlapping song-changed signals from firing concurrent extends — a real hazard
because gapless advance + an explicit skip can both emit in quick succession.
"""

# Extend the queue when fewer than this many tracks remain after the current.
_DEFAULT_THRESHOLD = 5


class RadioSession:
    """Tracks an active radio's seed + served ids and the queue-low decision."""

    def __init__(self, threshold: int = _DEFAULT_THRESHOLD):
        self.threshold = threshold
        self.seed_id = None
        self.served_ids: set = set()
        self._active = False
        self._extending = False

    @property
    def active(self) -> bool:
        return self._active

    def begin(self, seed_id, served_ids) -> None:
        """Start (or restart) a radio session seeded by ``seed_id``.

        ``served_ids`` are the ids already in the queue (the seed plus the
        first batch). Resets the re-entrancy guard.
        """
        self.seed_id = seed_id
        self.served_ids = set(served_ids or ())
        if seed_id is not None:
            self.served_ids.add(seed_id)
        self._active = True
        self._extending = False

    def should_extend(self, remaining: int) -> bool:
        """True when an extend should fire now.

        Requires an active session, no extend already in flight, and fewer than
        ``threshold`` tracks remaining after the current one.
        """
        if not self._active or self._extending:
            return False
        return remaining < self.threshold

    def mark_extending(self) -> None:
        """Latch the re-entrancy guard while an extend worker is in flight."""
        self._extending = True

    def finish_extending(self, new_ids) -> None:
        """Record the newly-served ids and release the re-entrancy guard."""
        for i in new_ids or ():
            if i is not None:
                self.served_ids.add(i)
        self._extending = False

    def clear(self) -> None:
        """Deactivate the session (e.g. the user started a non-radio playlist)."""
        self.seed_id = None
        self.served_ids = set()
        self._active = False
        self._extending = False
