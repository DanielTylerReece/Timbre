# collection_page.py
#
# Copyright 2024 Nokse22
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

from gi.repository import Adw, GLib, Gtk

from ..lib import utils
from ..lib.jellyfin.sync import LibrarySync
from .page import Page

logger = logging.getLogger(__name__)

# Cards rendered inline per collection carousel before "More".
_PREVIEW = 16

# Cumulative stage weighting for the manual-resync progress bar. Mirrors the
# onboarding sync page: the tracks stage dominates a real library, so it spans
# the widest band and the bar visibly moves through it instead of parking.
# Weights are the fraction of the [0,1] bar each stage spans (they sum to 1.0).
_SYNC_STAGES = ("artists", "albums", "tracks", "playlists", "genres")
_SYNC_WEIGHTS = {
    "artists": 0.10,
    "albums": 0.20,
    "tracks": 0.55,
    "playlists": 0.10,
    "genres": 0.05,
}


def _cumulative_stage_bases():
    """Map each sync stage -> ``(band_start, band_width)`` over the [0,1] bar.

    Pure helper (no gi state) so the progress->fraction math is unit-testable.
    """
    bases = {}
    acc = 0.0
    for stage in _SYNC_STAGES:
        width = _SYNC_WEIGHTS.get(stage, 0.0)
        bases[stage] = (acc, width)
        acc += width
    return bases


def sync_fraction(stage, done, total, bases=None):
    """Compute the absolute [0,1] bar fraction for a ``(stage, done, total)``.

    ``done/total`` positions within the stage's own band; an unknown stage maps
    to the full bar (1.0 within a zero-width band -> the accumulated base, i.e.
    no movement) so a stray callback never throws. Pure + unit-tested.
    """
    if bases is None:
        bases = _cumulative_stage_bases()
    base, width = bases.get(stage, (0.0, 0.0))
    within = (done / total) if total else 1.0
    within = min(max(within, 0.0), 1.0)
    return base + width * within


def sync_summary(added, removed):
    """Build the completion-toast string from real before/after track deltas.

    ``added``/``removed`` are non-negative track-count deltas the page derives
    from ``db.track_count()`` (``incremental_sync`` itself returns ``None`` — no
    counts — so this is the only honest source; nothing is invented). No net
    change -> "Library up to date". Pure + unit-tested.
    """
    if not added and not removed:
        return _("Library up to date")
    parts = []
    if added:
        parts.append(_("+{n} tracks").format(n=added))
    if removed:
        parts.append(_("{n} removed").format(n=removed))
    return _("Synced: {summary}").format(summary=", ".join(parts))


class HTCollectionPage(Page):
    """The user's library: Playlists / Albums / Tracks / Artists carousels.

    All data is read from SQLite. Each carousel's More button pushes a
    from-function page driven by the matching paged db helper.

    A "Sync library" affordance at the top re-runs an incremental sync against
    the server (reusing the same ``LibrarySync.incremental_sync`` orchestration
    the window's F5 / startup paths use, now WITH a progress callback), shows
    inline progress, then rebuilds the carousels in place from the freshened DB.
    """

    __gtype_name__ = "HTCollectionPage"

    def __init__(self):
        super().__init__()
        # Page-local re-entrancy guard for the Sync button (covers double-click
        # and a rebuild-in-flight). The window's own ``_refreshing`` flag is
        # additionally set/cleared during our sync so a Collection sync and an
        # F5 Home sync mutually exclude (see _start_sync).
        self._syncing = False
        # Widgets created lazily in _load_finish; declared here so callbacks
        # marshalled from worker threads can null-check them after a pop.
        self._sync_button = None
        self._sync_banner = None
        self._sync_bar = None
        # Staleness fingerprint of the data the carousels were last rendered
        # from (see _fingerprint). Captured at the end of each successful build;
        # the page-local "showing" signal re-checks it and rebuilds in place
        # only when it differs, so a playlist created from a track's menu (or a
        # favorite toggled / sync finished on another page) appears on return
        # without a manual re-navigate. ``None`` until the first build completes.
        self._fingerprint = None
        # Guards against overlapping staleness checks (a rapid show/hide/show).
        self._checking_stale = False

    # ------------------------------------------------------------------ #
    # Data load                                                          #
    # ------------------------------------------------------------------ #

    def _load_async(self) -> None:
        db = utils.db
        self.playlists = db.playlists_page(0, _PREVIEW)
        self.albums = db.albums_page(0, _PREVIEW)
        self.tracks = db.tracks_page(0, _PREVIEW)
        self.artists = db.artists_page(0, _PREVIEW)
        # Snapshot the staleness fingerprint on the SAME worker pass that read
        # the carousel data, so what we render and what we remember are
        # consistent (no window where a concurrent write splits them).
        self._pending_fingerprint = self._fingerprint_sync(db)

    def _load_finish(self) -> None:
        self.set_tag("collection")
        self.set_title(_("Collection"))

        self._build_sync_header()

        db = utils.db
        self.new_carousel_for(
            _("Playlists"), self.playlists,
            more_function=lambda offset, limit: db.playlists_page(offset, limit),
        )
        # Albums + Artists "More" pages get the Phase 7 year-filter dropdown.
        self.new_carousel_for(
            _("Albums"), self.albums,
            year_function=lambda year, offset, limit: db.albums_page(
                offset, limit, year=year
            ),
        )
        self.new_carousel_for(
            _("Tracks"), self.tracks,
            more_function=lambda offset, limit: db.tracks_page(offset, limit),
            item_type="track",
        )
        self.new_carousel_for(
            _("Artists"), self.artists,
            year_function=lambda year, offset, limit: db.artists_page(
                offset, limit, year=year
            ),
        )

        # Record the fingerprint these carousels were built from, then arm the
        # visibility hook that re-checks it. Both are set here (after a
        # successful build) so a half-built page never advertises a fingerprint.
        self._fingerprint = getattr(self, "_pending_fingerprint", None)
        self._pending_fingerprint = None
        self._arm_showing_hook()

    # ------------------------------------------------------------------ #
    # Live refresh on re-show (staleness fingerprint)                    #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _fingerprint_sync(db):
        """Cheap fingerprint of everything the carousels render from.

        ONE read on the caller's thread (must be a worker, like the rest of
        ``_load_async``). Captures the changes that should make a re-shown
        Collection page rebuild:

        * row counts of tracks / albums / artists / playlists — a sync that
          added or removed items elsewhere (startup sync, another page's F5);
        * ``SUM(is_favorite)`` across tracks / albums / artists — a favorite
          toggled from the player pane or an artist/album page (the carousels
          carry per-card favorite state, so this must invalidate too);
        * a hash over the full playlist set ``(id, name, track_count)`` in name
          order — the headline bug: creating a playlist from a track's
          three-dots menu writes a row immediately; a rename or a track added
          to an existing playlist changes name/track_count without moving the
          count. The playlist table is small (tens of rows), so hashing all of
          it is cheap.

        Returns an opaque tuple; equality is the only contract. Defensive
        against a missing/closed db (returns ``None`` -> treated as "unknown",
        never triggers a spurious rebuild).
        """
        if db is None:
            return None

        def query(conn):
            counts = conn.execute(
                "SELECT "
                "(SELECT COUNT(*) FROM tracks), "
                "(SELECT COUNT(*) FROM albums), "
                "(SELECT COUNT(*) FROM artists), "
                "(SELECT COUNT(*) FROM playlists), "
                "(SELECT COALESCE(SUM(is_favorite),0) FROM tracks), "
                "(SELECT COALESCE(SUM(is_favorite),0) FROM albums), "
                "(SELECT COALESCE(SUM(is_favorite),0) FROM artists)"
            ).fetchone()
            playlist_rows = conn.execute(
                "SELECT id, name, COALESCE(track_count, 0) FROM playlists "
                "ORDER BY id"
            ).fetchall()
            return tuple(counts), tuple(tuple(r) for r in playlist_rows)

        try:
            return db.read(query)
        except Exception:  # noqa: BLE001 — staleness check must never crash nav
            logger.debug("collection fingerprint read failed", exc_info=True)
            return None

    def _arm_showing_hook(self) -> None:
        """Connect the page-local "showing" signal once, tracked in signals.

        ``Adw.NavigationPage`` emits "showing" each time the page becomes the
        visible page (push, or pop-to back onto it). We re-check the fingerprint
        there and rebuild in place when it changed. Connected once per build and
        parked in ``self.signals`` so ``_rebuild`` / ``disconnect_all`` sweep it
        like every other page signal (no accretion, no leak).
        """
        self.signals.append((self, self.connect("showing", self._on_showing)))

    def _on_showing(self, _page) -> None:
        """Page became visible again — cheaply re-check for stale data.

        Skips when a sync is in flight (the sync's own rebuild will refresh and
        re-fingerprint), when we've never finished a build, or when a check is
        already running. The actual fingerprint read is async (db read off the
        main thread); the rebuild decision happens on the main loop.
        """
        if self._syncing or self._fingerprint is None or self._checking_stale:
            return
        if not getattr(self, "_alive", True):
            return
        self._checking_stale = True
        baseline = self._fingerprint

        def work():
            return self._fingerprint_sync(utils.db)

        def done(current):
            self._checking_stale = False
            if not getattr(self, "_alive", True) or self._syncing:
                return
            # ``None`` means the read failed/unknown — don't rebuild on noise.
            if current is not None and current != baseline:
                self._rebuild()

        def on_error(_exc):
            self._checking_stale = False

        utils.run_async(work, on_done=done, on_error=on_error, owner=self)

    # ------------------------------------------------------------------ #
    # Sync header (button + inline progress)                             #
    # ------------------------------------------------------------------ #

    def _build_sync_header(self) -> None:
        """Top-of-page "Sync library" button + a hidden inline progress banner.

        Rebuilt every ``_load_finish`` (the rebuild path re-runs the load
        pipeline), so the handler is parked in ``self.signals`` and swept by
        ``disconnect_all`` / the rebuild teardown like every other page signal.
        Adwaita idiom: a flat, icon+label header button matching the app's
        section-header style; the progress lives in an ``Adw.Banner`` beneath it.
        """
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                         margin_start=12, margin_end=12, margin_top=12,
                         margin_bottom=6)
        spacer = Gtk.Box(hexpand=True)
        header.append(spacer)

        button = Gtk.Button(valign=Gtk.Align.CENTER, css_classes=["flat"])
        btn_content = Adw.ButtonContent(
            icon_name="view-refresh-symbolic", label=_("Sync library")
        )
        button.set_child(btn_content)
        self.signals.append(
            (button, button.connect("clicked", self._on_sync_clicked))
        )
        header.append(button)
        self._sync_button = button
        self.content.append(header)

        # Inline progress: an Adw.Banner reveals beneath the header during sync.
        # Banner gives a tidy, full-width status strip; a child ProgressBar shows
        # the determinate fraction the progress_cb feeds. Chosen over a bare
        # ProgressBar because the Banner supplies the labelled, dismissible-style
        # container Adwaita uses for transient page-level status, and over a
        # spinner because incremental_sync reports real per-stage fractions
        # (a determinate bar is more honest than an indeterminate spinner).
        banner = Adw.Banner(revealed=False, title=_("Syncing library…"))
        self.content.append(banner)
        self._sync_banner = banner

        bar = Gtk.ProgressBar(
            margin_start=12, margin_end=12, margin_bottom=6,
            show_text=False, fraction=0.0, visible=False,
        )
        self.content.append(bar)
        self._sync_bar = bar

    # ------------------------------------------------------------------ #
    # Sync orchestration                                                 #
    # ------------------------------------------------------------------ #

    def _on_sync_clicked(self, _btn) -> None:
        self._start_sync()

    def _start_sync(self) -> None:
        """Kick a manual incremental sync with inline progress.

        Reuses the window's sync orchestration: same ``LibrarySync(client, db)
        .incremental_sync(library_ids)`` call the F5 / startup paths run (see
        window._run_incremental_sync), now passed a ``progress_cb`` that marshals
        to the main loop via ``GLib.idle_add`` — exactly mirroring the onboarding
        sync page. We do NOT reimplement any sync logic.

        Concurrency: no-op if our own sync is already running, if the window's
        F5/startup refresh is in flight (``window._refreshing``), or if we're not
        logged in. While running we set ``window._refreshing`` too, so an F5 and
        a Collection sync mutually exclude and clear it in both done/error.
        """
        if self._syncing:
            return
        window = self.get_root()
        client = getattr(window, "client", None) or utils.client
        if client is None or not getattr(window, "is_logged_in", True):
            utils.send_toast(_("Sync failed — server unreachable"), 3)
            return
        # Don't overlap an in-flight F5/startup/Home sync. Give feedback rather
        # than a dead click — the startup incremental sync now also holds this
        # flag, so a click during early startup lands here.
        if getattr(window, "_refreshing", False):
            utils.send_toast(_("Sync already running"), 2)
            return

        library_ids = list(utils.settings.get_strv("selected-libraries")) \
            if utils.settings is not None else []

        self._syncing = True
        if window is not None:
            window._refreshing = True
        self._before_count = utils.db.track_count() if utils.db else 0
        self._sync_window = window

        self._sync_button.set_sensitive(False)
        self._sync_bar.set_fraction(0.0)
        self._sync_bar.set_visible(True)
        self._sync_banner.set_revealed(True)

        sync = LibrarySync(client, utils.db)
        bases = _cumulative_stage_bases()

        def progress_cb(stage, done, total):
            # Fires from worker threads (one per library) — marshal to the main
            # loop, exactly like onboarding's sync page.
            GLib.idle_add(self._on_sync_progress, stage, done, total, bases)

        def work():
            # The sync exception is caught HERE (not routed to run_async's
            # on_error) and folded into the returned status, so the failure path
            # runs through the single owner-guarded on_done. Always clear the
            # window's reentrancy guard in a finally so an F5 isn't wedged even
            # if the page was popped mid-sync (a plain bool write across threads
            # is safe enough here).
            try:
                if library_ids:
                    sync.incremental_sync(library_ids, progress_cb=progress_cb)
                return True
            except Exception:  # noqa: BLE001 — surfaced via the toast below
                logger.exception("manual collection sync failed")
                return False
            finally:
                self._clear_window_refreshing()

        utils.run_async(work, on_done=self._on_sync_finish, owner=self)

    def _on_sync_progress(self, stage, done, total, bases) -> bool:
        """Advance the bar from a worker ``progress_cb`` (idle-marshalled).

        Owner-guarded: after a mid-sync pop, ``disconnect_all`` sets
        ``_alive=False`` and nulls the bar; tolerate both. Monotonic so
        interleaved earlier-stage callbacks from multiple library workers don't
        rewind the bar.
        """
        if not getattr(self, "_alive", True) or self._sync_bar is None:
            return False
        fraction = sync_fraction(stage, done, total, bases)
        if fraction > self._sync_bar.get_fraction():
            self._sync_bar.set_fraction(fraction)
        return False

    def _clear_window_refreshing(self) -> None:
        window = getattr(self, "_sync_window", None)
        if window is not None and getattr(window, "_refreshing", False):
            window._refreshing = False
        self._sync_window = None

    def _on_sync_finish(self, ok) -> None:
        """Single completion callback (owner-guarded via run_async).

        ``ok`` is the worker's status: True = sync succeeded, False = it raised
        (the worker folds the exception into this flag rather than relying on
        run_async's on_error). The window guard is cleared in the worker's
        ``finally`` already; this is belt-and-suspenders for the live path.

        Success: toast a real delta summary and rebuild the (now-stale)
        carousels in place. ``incremental_sync`` returns ``None`` (no counts), so
        the +N/M summary is derived from ``db.track_count()`` before vs. after —
        the only honest source; nothing is invented.

        Failure: toast, re-enable the button, hide progress. Never silent.
        """
        self._clear_window_refreshing()
        self._syncing = False
        # Popped mid-sync: run_async's owner-guard normally drops this callback
        # entirely; the explicit check defends against any path that still fires.
        if not getattr(self, "_alive", True) or self._sync_button is None:
            return

        if ok:
            after = utils.db.track_count() if utils.db else self._before_count
            added = max(0, after - self._before_count)
            removed = max(0, self._before_count - after)
            utils.send_toast(sync_summary(added, removed), 2)
            self._rebuild()
            return

        utils.send_toast(_("Sync failed — server unreachable"), 3)
        self._sync_button.set_sensitive(True)
        self._sync_bar.set_visible(False)
        self._sync_banner.set_revealed(False)

    # ------------------------------------------------------------------ #
    # In-place rebuild                                                   #
    # ------------------------------------------------------------------ #

    def _rebuild(self) -> None:
        """Tear down built content + tracked signals, then re-run ``load()``.

        Mirrors the artist page's reload, but does NOT call ``disconnect_all``
        (which sets ``_alive=False`` and would gate the reload's owner-guarded
        ``_load_finish`` out). Instead it sweeps the same resources by hand —
        child IDisconnectables (carousels), ``_section_signals``, and the
        page-level ``self.signals`` (the Sync button handler lives there) — so
        nothing accretes across rebuilds, while leaving ``_alive`` intact.
        """
        for d in list(self.disconnectables):
            if hasattr(d, "disconnect_all"):
                d.disconnect_all()
        self.disconnectables = []
        for obj, signal_id in self.signals:
            if obj.handler_is_connected(signal_id):
                obj.disconnect(signal_id)
        self.signals = []
        scope = getattr(self, "_section_signals", None)
        if scope:
            for obj, signal_id in scope:
                if obj.handler_is_connected(signal_id):
                    obj.disconnect(signal_id)
            scope.clear()

        # Sync widgets are about to be removed; drop refs so late idle callbacks
        # (none should remain — sync is done) can't touch detached widgets.
        self._sync_button = None
        self._sync_banner = None
        self._sync_bar = None

        child = self.content.get_first_child()
        while child is not None:
            self.content.remove(child)
            child = self.content.get_first_child()

        self.content_stack.set_visible_child_name("loading")
        self.load()
