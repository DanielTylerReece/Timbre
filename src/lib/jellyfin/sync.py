# sync.py
#
# Copyright (C) 2026 Tyler Reece
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Jellyfin -> local SQLite sync.

No gi/GTK imports — headless-testable. Pulls every selected music library's
albums/tracks/playlists/artists/genres and merges them into one ``Database``,
stamping each row with its ``library_id`` so the underlying collections are
invisible to the UI (which reads SQLite only).

``progress_cb(stage, done, total)`` is invoked from worker threads; the GTK
caller adapts it to ``GLib.idle_add`` later. It is always called under a lock
so a UI adapter never sees interleaved partial calls.
"""

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from .models import Album, Artist, Genre, Playlist, Track

_PAGE = 500
_BATCH = 500
# Two tracks are duplicates when their durations are within this many ticks
# (1 tick = 100ns; 20_000_000 ticks = 2 seconds).
_DUP_TICKS = 20_000_000


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _chunks(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


class LibrarySync:
    """Drives a full or incremental sync of selected libraries into the DB."""

    def __init__(self, client, db):
        self.client = client
        self.db = db
        self._progress_lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # Paging.                                                            #
    # ------------------------------------------------------------------ #

    def _walk(self, parent_id, item_types, progress_cb=None, stage=None):
        """Yield every item of ``item_types`` under ``parent_id``, paged.

        When ``progress_cb``/``stage`` are given, emit ``(stage, done, total)``
        after each page so a long stage (e.g. tracks on a multi-thousand-track
        library) reports incremental progress instead of a single end-of-stage
        tick. ``total`` is the server-reported ``TotalRecordCount``; ``done`` is
        the running count yielded so far.
        """
        start = 0
        while True:
            items, total = self.client.items_page(
                parent_id, item_types, start, limit=_PAGE
            )
            if not items:
                break
            for it in items:
                yield it
            start += len(items)
            if stage is not None:
                self._emit(progress_cb, stage, min(start, total), total)
            if start >= total:
                break

    def _emit(self, progress_cb, stage, done, total):
        if progress_cb is None:
            return
        with self._progress_lock:
            # NOTE: progress_cb runs while self._progress_lock is held. It must
            # NOT call back into this sync object (e.g. trigger another sync or
            # anything that re-enters _emit) or it will deadlock on this lock.
            progress_cb(stage, done, total)

    # ------------------------------------------------------------------ #
    # Per-library sync (runs on its own thread).                         #
    # ------------------------------------------------------------------ #

    def _sync_one(self, library_id, progress_cb, seen=None):
        """Sync one library. If ``seen`` is given, record ids per type into it
        (for incremental prune). Returns nothing; writes go through ``db``."""
        # Artists first — tracks/albums FK-reference them. Upserting artists
        # (and albums) ahead of tracks keeps foreign keys satisfiable.
        artists = [Artist.from_item(it) for it in self.client.artists(library_id)]
        for batch in _chunks(artists, _BATCH):
            self.db.upsert_artists(batch)
        # Server-supplied bios (Overview) are authoritative: when an artist
        # carries a non-empty Overview, store it as the local bio with
        # bio_source='jellyfin', OVERWRITING any prior AI-generated bio. An
        # empty/absent Overview leaves existing bio columns untouched (we never
        # null out a previously-stored AI bio just because the server has none).
        for a in artists:
            overview = (a.overview or "").strip()
            if a.id and overview:
                self.db.set_artist_bio(a.id, overview, source="jellyfin")
        self._emit(progress_cb, "artists", len(artists), len(artists))
        artist_ids = {a.id for a in artists if a.id}

        # Albums. Emit per page so the bar advances during the walk.
        albums = [
            Album.from_item(it)
            for it in self._walk(
                library_id, "MusicAlbum", progress_cb, "albums"
            )
        ]
        for a in albums:
            a.library_id = library_id
        # Null album-artist refs we never synced (keeps FK satisfiable).
        for a in albums:
            if a.album_artist_id and a.album_artist_id not in artist_ids:
                a.album_artist_id = None
        for batch in _chunks(albums, _BATCH):
            self.db.upsert_albums(batch)
        self._emit(progress_cb, "albums", len(albums), len(albums))
        album_ids = {a.id for a in albums if a.id}
        if seen is not None:
            seen["albums"].update(album_ids)

        # Tracks. Null any album/artist refs we never synced so FK holds even
        # when Jellyfin reports a referent outside the synced item set.
        tracks = [
            Track.from_item(it)
            for it in self._walk(library_id, "Audio", progress_cb, "tracks")
        ]
        for t in tracks:
            t.library_id = library_id
            if t.album_id and t.album_id not in album_ids:
                t.album_id = None
            if t.artist_id and t.artist_id not in artist_ids:
                t.artist_id = None
        for batch in _chunks(tracks, _BATCH):
            self.db.upsert_tracks(batch)
        self._emit(progress_cb, "tracks", len(tracks), len(tracks))
        if seen is not None:
            seen["tracks"].update(t.id for t in tracks if t.id)

        # Playlists (+ their ordered children).
        playlists = [
            Playlist.from_item(it) for it in self._walk(library_id, "Playlist")
        ]
        for p in playlists:
            self.db.upsert_playlists([p])
            child_ids = [
                it.get("Id")
                for it in self._walk_children(p.id)
                if it.get("Id")
            ]
            self.db.set_playlist_tracks(p.id, child_ids)
        self._emit(progress_cb, "playlists", len(playlists), len(playlists))
        if seen is not None:
            seen["playlists"].update(p.id for p in playlists if p.id)

        # Genres.
        genres = [Genre.from_item(it) for it in self.client.genres(library_id)]
        if genres:
            self.db.upsert_genres(genres)
        self._emit(progress_cb, "genres", len(genres), len(genres))

    def _walk_children(self, playlist_id):
        """Yield a playlist's audio children in order (paged)."""
        start = 0
        while True:
            items, total = self.client.items_page(
                playlist_id, "Audio", start, limit=_PAGE
            )
            if not items:
                break
            for it in items:
                yield it
            start += len(items)
            if start >= total:
                break

    # ------------------------------------------------------------------ #
    # Public entry points.                                               #
    # ------------------------------------------------------------------ #

    def full_sync(self, library_ids, progress_cb=None):
        """Full sync of the given libraries (one worker thread per library)."""
        self._record_libraries(library_ids)
        if library_ids:
            with ThreadPoolExecutor(max_workers=len(library_ids)) as ex:
                futures = [
                    ex.submit(self._sync_one, lib, progress_cb)
                    for lib in library_ids
                ]
                for f in futures:
                    f.result()  # propagate exceptions
        self._dedupe()
        self.db.meta_set("last_sync", _now_iso())

    def incremental_sync(self, library_ids, progress_cb=None):
        """Re-walk + upsert everything, then prune rows not seen this pass.

        Pruning is scoped to ``library_ids`` only — rows belonging to
        unselected/unsynced libraries are never touched.
        """
        self._record_libraries(library_ids)
        seen = {"albums": set(), "tracks": set(), "playlists": set()}
        seen_lock = threading.Lock()

        def run(lib):
            local = {"albums": set(), "tracks": set(), "playlists": set()}
            self._sync_one(lib, progress_cb, seen=local)
            with seen_lock:
                for k in seen:
                    seen[k].update(local[k])

        if library_ids:
            with ThreadPoolExecutor(max_workers=len(library_ids)) as ex:
                futures = [ex.submit(run, lib) for lib in library_ids]
                for f in futures:
                    f.result()

        self._prune(library_ids, seen)
        self._dedupe()
        self.db.meta_set("last_sync", _now_iso())

    def seed_history(self):
        """One-time: backfill play_history from existing play_count/last_played.

        Inserts ONE row (source='seed') per played track WITHOUT bumping
        play_count. Guarded by the ``history_seeded`` meta key.
        """
        if self.db.meta_get("history_seeded") == "1":
            return

        def fn(conn):
            rows = conn.execute(
                "SELECT id, last_played FROM tracks "
                "WHERE play_count > 0 AND last_played IS NOT NULL"
            ).fetchall()
            for r in rows:
                conn.execute(
                    "INSERT INTO play_history(track_id, played_at, source) "
                    "VALUES(?,?,'seed')",
                    (r["id"], r["last_played"]),
                )

        self.db.write(fn)
        self.db.meta_set("history_seeded", "1")

    # ------------------------------------------------------------------ #
    # Internals.                                                         #
    # ------------------------------------------------------------------ #

    def _record_libraries(self, library_ids):
        """Ensure a libraries row exists for each synced id (FK target)."""
        names = {}
        try:
            for lib in self.client.music_libraries():
                names[lib.id] = lib.name
        except Exception:  # noqa: BLE001 - names are best-effort
            pass
        rows = [
            {"id": lib, "name": names.get(lib, lib), "selected": 1}
            for lib in library_ids
        ]
        if rows:
            self.db.upsert_libraries(rows)

    def _dedupe(self):
        """Collapse *cross-library* duplicate tracks only.

        Dedupe exists solely to merge identical tracks that appear in MULTIPLE
        selected libraries. Same-library duplicates (deluxe editions,
        intentional dupes) must NEVER be collapsed.

        Algorithm:

        * Rows with NULL ``duration_ticks`` or NULL/empty ``artist_name`` /
          ``album_name`` / ``name`` are excluded from dedupe entirely — they
          carry too little identity to safely match and previously
          mass-clustered via COALESCE.
        * Remaining rows are grouped on
          ``(lower(artist_name), lower(album_name), lower(name))`` and within a
          group bucketed into duration clusters (durations within +-_DUP_TICKS).
        * Within a cluster, the highest-bitrate row overall is the ``primary``.
          A row is a removable duplicate ONLY if its ``library_id`` differs
          from the primary's. Rows sharing the primary's ``library_id`` are
          always kept — so a same-library pair never collapses into itself, and
          a cluster can only ever shed rows that duplicate a row from a
          DIFFERENT library.

        Kept/removed semantics by example (cluster of identical title/album/
        artist, durations within window):

        * libA{320k, 256k} + libB{128k}: primary = libA's 320k row. libB's 128k
          row is removed (recorded in track_duplicates); BOTH libA rows survive
          (they share the primary's library).
        * libA{320k} + libA{256k} (same library only): primary = the 320k row;
          the 256k row shares its library, so it is kept. Nothing removed.
        * libA{128k} + libB{900k}: primary = libB's 900k row; libA's 128k row is
          a cross-library dup and is removed.

        Removed rows are recorded in track_duplicates and DELETEd from tracks.
        """

        def fn(conn):
            rows = conn.execute(
                "SELECT id, library_id, "
                "LOWER(artist_name) AS a, "
                "LOWER(album_name) AS al, "
                "LOWER(name) AS n, "
                "duration_ticks AS dur, "
                "COALESCE(bitrate, 0) AS br "
                "FROM tracks "
                "WHERE duration_ticks IS NOT NULL "
                "AND artist_name IS NOT NULL AND artist_name != '' "
                "AND album_name IS NOT NULL AND album_name != '' "
                "AND name IS NOT NULL AND name != ''"
            ).fetchall()

            groups = {}
            for r in rows:
                groups.setdefault((r["a"], r["al"], r["n"]), []).append(r)

            to_delete = []
            for members in groups.values():
                if len(members) < 2:
                    continue
                # Cluster by duration window. Sort by duration, then greedily
                # bucket members within +-_DUP_TICKS of a cluster's anchor.
                members = sorted(members, key=lambda r: r["dur"])
                clusters = []
                for m in members:
                    placed = False
                    for cl in clusters:
                        if abs(m["dur"] - cl[0]["dur"]) <= _DUP_TICKS:
                            cl.append(m)
                            placed = True
                            break
                    if not placed:
                        clusters.append([m])
                for cl in clusters:
                    if len(cl) < 2:
                        continue
                    # A cluster can only merge across libraries. If every row
                    # shares one library, there is nothing to collapse.
                    if len({m["library_id"] for m in cl}) < 2:
                        continue
                    primary = max(cl, key=lambda r: r["br"])
                    for m in cl:
                        # Keep every row in the primary's own library; only rows
                        # from a DIFFERENT library are removable duplicates.
                        if m["library_id"] == primary["library_id"]:
                            continue
                        conn.execute(
                            "INSERT OR IGNORE INTO track_duplicates"
                            "(primary_id, dup_id) VALUES(?,?)",
                            (primary["id"], m["id"]),
                        )
                        to_delete.append(m["id"])

            for tid in to_delete:
                conn.execute("DELETE FROM track_artists WHERE track_id=?", (tid,))
                conn.execute("DELETE FROM tracks WHERE id=?", (tid,))

        self.db.write(fn)

    def _prune(self, library_ids, seen):
        """Delete tracks/albums/playlists in the synced libraries not seen.

        Scoped strictly to ``library_ids`` so unselected libraries are safe.
        Then prune artists no longer referenced by any track or album.
        """
        if not library_ids:
            return
        libs = list(library_ids)

        def fn(conn):
            ph = ",".join("?" for _ in libs)

            keep_tracks = list(seen["tracks"])
            stale = conn.execute(
                f"SELECT id FROM tracks WHERE library_id IN ({ph})", libs
            ).fetchall()
            for r in stale:
                if r["id"] not in seen["tracks"]:
                    conn.execute(
                        "DELETE FROM track_artists WHERE track_id=?", (r["id"],)
                    )
                    conn.execute("DELETE FROM tracks WHERE id=?", (r["id"],))

            stale_al = conn.execute(
                f"SELECT id FROM albums WHERE library_id IN ({ph})", libs
            ).fetchall()
            for r in stale_al:
                if r["id"] not in seen["albums"]:
                    conn.execute("DELETE FROM albums WHERE id=?", (r["id"],))

            # Playlists have no library_id column; prune any not seen this pass
            # (playlists are global, but a full re-walk sees all of them).
            stale_pl = conn.execute("SELECT id FROM playlists").fetchall()
            for r in stale_pl:
                if r["id"] not in seen["playlists"]:
                    conn.execute(
                        "DELETE FROM playlist_tracks WHERE playlist_id=?",
                        (r["id"],),
                    )
                    conn.execute("DELETE FROM playlists WHERE id=?", (r["id"],))

            # Orphan artists: referenced by no track and no album.
            conn.execute(
                "DELETE FROM artists WHERE id NOT IN "
                "(SELECT artist_id FROM tracks WHERE artist_id IS NOT NULL) "
                "AND id NOT IN "
                "(SELECT album_artist_id FROM albums WHERE album_artist_id IS NOT NULL) "
                "AND id NOT IN "
                "(SELECT artist_id FROM track_artists)"
            )

        self.db.write(fn)
