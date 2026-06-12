# playback_reporter.py
#
# Copyright (C) 2026 Tyler Reece
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Playback-reporting scheduler — testable, no ``gi``.

Decides *when* to fire Jellyfin playback reports (start / progress / stop) and
records local play history, off the main loop. All network/IO is funnelled
through a single daemon worker consuming a queue (same shape as the DB writer,
simpler). The worker swallows + logs ``JellyfinError`` / ``JellyfinNetworkError``
so a flaky server never crashes playback.

Rate-limiting (in :meth:`on_tick`) is driven by an injectable monotonic clock
so it can be unit-tested with a fake clock and zero sleeps.

Dependency injection: ``client`` and/or ``db`` may be ``None`` (the Phase 0
shell runs without them). With both None every method is a no-op.

Contract:
  * :meth:`on_start` → one ``report_start`` + one ``record_play`` per playback
    start. ``record_play`` is enqueued independently of the network report, so
    a failing ``report_start`` still records local history.
  * :meth:`on_tick` reports progress at most every 10s, UNLESS the paused state
    changed since the last report or ``force=True`` (a seek).
  * :meth:`on_stop` → ``report_stop`` with the final ticks. Never writes
    history.
"""

import logging
import queue
import threading
import time

logger = logging.getLogger(__name__)

# Minimum seconds between successive progress reports (unless forced / flipped).
_PROGRESS_INTERVAL = 10.0

# Sentinel that stops the worker thread.
_STOP = object()


class PlaybackReporter:
    def __init__(self, client, db, clock=time.monotonic):
        """Args:
        client: a Jellyfin client (or None) exposing report_start/
            report_progress/report_stop.
        db: a Database (or None) exposing record_play(track_id).
        clock: monotonic time source (seconds, float). Injectable for tests.
        """
        self._client = client
        self._db = db
        self._clock = clock

        self._track_id = None
        self._last_report_t = None
        self._last_paused = None

        self._q = queue.Queue()
        self._closed = False
        self._worker = threading.Thread(
            target=self._worker_loop, name="timbre-reporter", daemon=True
        )
        self._worker.start()

    # ------------------------------------------------------------------ #
    # Worker                                                             #
    # ------------------------------------------------------------------ #

    def _worker_loop(self):
        while True:
            job = self._q.get()
            try:
                if job is _STOP:
                    return
                fn = job
                try:
                    fn()
                except Exception:
                    # Network/Jellyfin errors (and anything else) must never
                    # escape the worker and crash playback — log and continue.
                    logger.warning("playback report failed", exc_info=True)
            finally:
                self._q.task_done()

    def _submit(self, fn):
        if self._closed:
            return
        self._q.put(fn)

    def flush(self):
        """Block until every queued job has been processed. (Test helper.)"""
        self._q.join()

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._q.put(_STOP)
        self._worker.join(timeout=5)
        if self._worker.is_alive():
            logger.warning("playback reporter worker did not stop within timeout")

    # ------------------------------------------------------------------ #
    # Public scheduling API (called from the main loop)                  #
    # ------------------------------------------------------------------ #

    def on_start(self, track_id):
        """A track began playing: report start + record one local play."""
        self._track_id = track_id
        self._last_report_t = None
        self._last_paused = None

        if self._db is not None:
            db = self._db
            self._submit(lambda: db.record_play(track_id))
        if self._client is not None:
            client = self._client
            self._submit(lambda: client.report_start(track_id))

    def on_tick(self, position_ticks, paused, force=False):
        """Maybe report progress. Rate-limited to >= 10s unless the paused
        state flipped or ``force`` is set (a seek)."""
        if self._client is None or self._track_id is None:
            return

        now = self._clock()
        flipped = self._last_paused is not None and paused != self._last_paused
        first = self._last_report_t is None
        due = first or (now - self._last_report_t) >= _PROGRESS_INTERVAL

        if not (due or flipped or force):
            self._last_paused = paused
            return

        self._last_report_t = now
        self._last_paused = paused
        track_id = self._track_id
        client = self._client
        self._submit(
            lambda: client.report_progress(track_id, position_ticks, paused)
        )

    def on_stop(self, position_ticks):
        """Playback of the current track ended/stopped: report stop."""
        track_id = self._track_id
        if track_id is None:
            return
        self._track_id = None
        self._last_report_t = None
        self._last_paused = None
        if self._client is not None:
            client = self._client
            self._submit(
                lambda: client.report_stop(track_id, position_ticks)
            )
