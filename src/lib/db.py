# db.py
#
# Copyright (C) 2026 Tyler Reece
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Local SQLite library layer for Timbre.

No gi/GTK imports — this module is intentionally headless-testable.

Architecture
------------
Timbre is **sync-first**: every selected Jellyfin music library is fully
synced into one local SQLite database, and ALL UI browsing/search reads SQLite
only. Multiple Jellyfin music libraries merge into one database (each row keeps
a ``library_id``) so the underlying collections are invisible to the user.

Concurrency contract
---------------------
One ``Database`` object is shared app-wide.

* **Writes** all funnel through a single dedicated writer thread. Call
  ``write(fn)`` (or ``write_many(fns)``); ``fn(conn)`` runs on the writer
  thread and its return value is handed back to the caller, which blocks until
  it completes. Because every mutation is serialized on one connection there
  are no lost updates and no ``database is locked`` races.
* **Reads** use per-thread connections (``threading.local``). Call
  ``read(fn)``; ``fn(conn)`` runs on the caller's thread against that thread's
  own read connection. Reads never block the writer.
* ``close()`` drains and stops the writer thread and closes every connection.

SQLite is opened in WAL mode with ``foreign_keys=ON``. The writer connection is
the single source of truth for mutations; reader connections are opened with
``check_same_thread=False`` only defensively (each is still used from exactly
one thread).
"""

import json
import logging
import os
import queue
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

_LOG = logging.getLogger(__name__)

SCHEMA_VERSION = 3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta      (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS libraries (id TEXT PRIMARY KEY, name TEXT NOT NULL, selected INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS artists   (id TEXT PRIMARY KEY, name TEXT NOT NULL, sort_name TEXT, image_tag TEXT,
                        is_favorite INTEGER NOT NULL DEFAULT 0, bio TEXT, bio_updated_at TEXT,
                        bio_source TEXT);
CREATE TABLE IF NOT EXISTS albums    (id TEXT PRIMARY KEY, name TEXT NOT NULL, sort_name TEXT,
                        album_artist_id TEXT REFERENCES artists(id), album_artist_name TEXT,
                        year INTEGER, date_created TEXT, image_tag TEXT,
                        is_favorite INTEGER NOT NULL DEFAULT 0,
                        play_count INTEGER NOT NULL DEFAULT 0, last_played TEXT,
                        library_id TEXT REFERENCES libraries(id));
CREATE TABLE IF NOT EXISTS tracks    (id TEXT PRIMARY KEY, name TEXT NOT NULL,
                        album_id TEXT REFERENCES albums(id), album_name TEXT,
                        artist_id TEXT REFERENCES artists(id), artist_name TEXT,
                        duration_ticks INTEGER, index_number INTEGER, parent_index_number INTEGER,
                        year INTEGER, genres TEXT,
                        bitrate INTEGER, codec TEXT,
                        is_favorite INTEGER NOT NULL DEFAULT 0,
                        play_count INTEGER NOT NULL DEFAULT 0, last_played TEXT,
                        date_created TEXT, library_id TEXT REFERENCES libraries(id));
CREATE TABLE IF NOT EXISTS track_artists    (track_id TEXT REFERENCES tracks(id), artist_id TEXT REFERENCES artists(id),
                               PRIMARY KEY (track_id, artist_id));
CREATE TABLE IF NOT EXISTS track_duplicates (primary_id TEXT REFERENCES tracks(id), dup_id TEXT, PRIMARY KEY (primary_id, dup_id));
CREATE TABLE IF NOT EXISTS genres    (id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL);
CREATE TABLE IF NOT EXISTS playlists (id TEXT PRIMARY KEY, name TEXT NOT NULL, image_tag TEXT,
                        track_count INTEGER, last_played TEXT);
CREATE TABLE IF NOT EXISTS playlist_tracks (playlist_id TEXT REFERENCES playlists(id), track_id TEXT, position INTEGER,
                              PRIMARY KEY (playlist_id, position));
CREATE TABLE IF NOT EXISTS play_history (id INTEGER PRIMARY KEY AUTOINCREMENT,
                           track_id TEXT NOT NULL, played_at TEXT NOT NULL,
                           source TEXT NOT NULL DEFAULT 'local');
CREATE TABLE IF NOT EXISTS ai_cache  (kind TEXT NOT NULL, key TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL,
                        PRIMARY KEY (kind, key));
CREATE INDEX IF NOT EXISTS idx_tracks_album      ON tracks(album_id);
CREATE INDEX IF NOT EXISTS idx_tracks_artist     ON tracks(artist_id);
CREATE INDEX IF NOT EXISTS idx_tracks_playcount  ON tracks(play_count DESC);
CREATE INDEX IF NOT EXISTS idx_tracks_lastplayed ON tracks(last_played DESC);
CREATE INDEX IF NOT EXISTS idx_tracks_year       ON tracks(year);
CREATE INDEX IF NOT EXISTS idx_albums_created    ON albums(date_created DESC);
CREATE INDEX IF NOT EXISTS idx_history_when      ON play_history(played_at);
CREATE INDEX IF NOT EXISTS idx_history_track     ON play_history(track_id);
"""


def default_db_path() -> Path:
    """Return the default on-disk database path, creating parent dirs.

    ``$XDG_DATA_HOME/timbre/library.db`` if XDG_DATA_HOME is set, else
    ``~/.local/share/timbre/library.db``.
    """
    base = os.environ.get("XDG_DATA_HOME")
    root = Path(base) if base else Path.home() / ".local" / "share"
    path = root / "timbre" / "library.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _g(obj, attr, default=None):
    """Read ``attr`` from a dict (by key) or an object (by attribute)."""
    if isinstance(obj, dict):
        return obj.get(attr, default)
    return getattr(obj, attr, default)


def _int(val):
    """Coerce a bool/None/int-ish flag value to an int (None -> 0)."""
    if val is None:
        return 0
    return int(bool(val)) if isinstance(val, bool) else int(val)


def _dicts(rows):
    """Convert a list of ``sqlite3.Row`` to a list of plain dicts."""
    return [dict(r) for r in rows]


def _tagged(rows, kind):
    """Convert ``sqlite3.Row`` rows to dicts, each tagged with a ``kind`` key.

    The Phase 5 widget kit dispatches polymorphically on a ``kind`` key
    (``"album"`` | ``"artist"`` | ``"playlist"`` | ``"track"`` | ``"genre"``)
    rather than ``isinstance``. Browse query helpers tag their rows so cards and
    track rows can be built directly from db dicts. See the convention docstring
    in ``src/widgets/card_widget.py``.
    """
    out = []
    for r in rows:
        d = dict(r)
        d["kind"] = kind
        out.append(d)
    return out


# Sentinel pushed onto the writer queue to stop the writer thread.
_STOP = object()


class Database:
    """Shared SQLite library store with a single-writer / many-reader model.

    See the module docstring for the full concurrency contract. Construction is
    idempotent: it creates the schema and runs migrations if needed, so calling
    ``Database(path)`` twice against the same file is safe.
    """

    def __init__(self, path):
        self.path = str(path)
        self._local = threading.local()
        self._write_q: "queue.Queue" = queue.Queue()
        self._closed = False

        # Dedicated writer thread owns the one writable connection.
        self._writer_ready = threading.Event()
        self._writer_conn = None
        self._writer = threading.Thread(
            target=self._writer_loop, name="timbre-db-writer", daemon=True
        )
        self._writer.start()
        self._writer_ready.wait()

        # Create / migrate schema synchronously on the writer thread.
        self.write(self._migrate)

    # ------------------------------------------------------------------ #
    # Connection plumbing.                                               #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _configure(conn: sqlite3.Connection):
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")

    def _writer_loop(self):
        conn = sqlite3.connect(self.path, check_same_thread=False)
        self._configure(conn)
        self._writer_conn = conn
        self._writer_ready.set()
        while True:
            item = self._write_q.get()
            if item is _STOP:
                conn.close()
                self._write_q.task_done()
                return
            fn, future = item
            try:
                result = fn(conn)
                conn.commit()
                future["result"] = result
            except BaseException as exc:  # noqa: BLE001 - relayed to caller
                conn.rollback()
                future["error"] = exc
            finally:
                future["done"].set()
                self._write_q.task_done()

    @property
    def _read_conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, check_same_thread=False)
            self._configure(conn)
            self._local.conn = conn
        return conn

    # ------------------------------------------------------------------ #
    # Public read/write API.                                             #
    # ------------------------------------------------------------------ #

    def write(self, fn):
        """Run ``fn(conn)`` on the writer thread; block and return its result.

        The whole call is one transaction: it commits on success, rolls back
        and re-raises on any exception.
        """
        if self._closed:
            raise RuntimeError("Database is closed")
        future = {"done": threading.Event(), "result": None, "error": None}
        self._write_q.put((fn, future))
        future["done"].wait()
        if future["error"] is not None:
            raise future["error"]
        return future["result"]

    def write_many(self, fns):
        """Run several ``fn(conn)`` callables as one transaction on the writer.

        Returns the list of their results. Any exception rolls the whole batch
        back.
        """

        def _batch(conn):
            return [fn(conn) for fn in fns]

        return self.write(_batch)

    def read(self, fn):
        """Run ``fn(conn)`` against the calling thread's read connection."""
        return fn(self._read_conn)

    def close(self):
        """Drain the writer queue, stop the writer thread, close connections."""
        if self._closed:
            return
        self._closed = True
        self._write_q.put(_STOP)
        self._writer.join(timeout=10)
        if self._writer.is_alive():
            _LOG.warning(
                "db writer thread did not stop within 10s; "
                "queued writes may be lost"
            )
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------------ #
    # Migrations.                                                        #
    # ------------------------------------------------------------------ #

    def _migrate(self, conn: sqlite3.Connection):
        conn.executescript(_SCHEMA)
        row = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        current = int(row["value"]) if row else 0
        # v1 is the baseline. v2 adds idx_tracks_year, which the executescript
        # above already creates (CREATE INDEX IF NOT EXISTS in _SCHEMA), so it
        # needs no per-version work. Per-version backfills add `if current < N`
        # blocks below.
        #
        # v3: artists.bio_source TEXT (values 'jellyfin' | 'ai' | NULL). On a
        # FRESH db the column is already present (it's in _SCHEMA's CREATE
        # TABLE), but for an EXISTING artists table CREATE TABLE IF NOT EXISTS
        # is a no-op, so the column must be added explicitly. Guard on the
        # actual column set rather than the version number so a re-run is safe.
        if current < 3:
            cols = {
                r["name"]
                for r in conn.execute("PRAGMA table_info(artists)").fetchall()
            }
            if "bio_source" not in cols:
                conn.execute("ALTER TABLE artists ADD COLUMN bio_source TEXT")
        if current < SCHEMA_VERSION:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )

    # ------------------------------------------------------------------ #
    # Upserts. Each accepts model dataclasses (Track/Album/...) OR dicts. #
    # Bools/None are coerced to ints where the column is an INTEGER flag. #
    # ------------------------------------------------------------------ #

    def upsert_libraries(self, libs):
        """Upsert (id, name[, selected]) rows. ``libs`` are dicts or objects."""

        def fn(conn):
            for lib in libs:
                conn.execute(
                    "INSERT INTO libraries(id, name, selected) VALUES(?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET name=excluded.name, "
                    "selected=excluded.selected",
                    (
                        _g(lib, "id"),
                        _g(lib, "name"),
                        _int(_g(lib, "selected", 1)),
                    ),
                )

        return self.write(fn)

    def upsert_artists(self, artists):
        def fn(conn):
            for a in artists:
                conn.execute(
                    "INSERT INTO artists(id, name, sort_name, image_tag, is_favorite) "
                    "VALUES(?,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET name=excluded.name, "
                    "sort_name=excluded.sort_name, image_tag=excluded.image_tag, "
                    "is_favorite=excluded.is_favorite",
                    (
                        _g(a, "id"),
                        _g(a, "name"),
                        _g(a, "sort_name"),
                        _g(a, "image_tag"),
                        _int(_g(a, "is_favorite")),
                    ),
                )

        return self.write(fn)

    def upsert_albums(self, albums):
        def fn(conn):
            for a in albums:
                conn.execute(
                    "INSERT INTO albums(id, name, sort_name, album_artist_id, "
                    "album_artist_name, year, date_created, image_tag, is_favorite, "
                    "library_id) VALUES(?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET name=excluded.name, "
                    "sort_name=excluded.sort_name, "
                    "album_artist_id=excluded.album_artist_id, "
                    "album_artist_name=excluded.album_artist_name, "
                    "year=excluded.year, date_created=excluded.date_created, "
                    "image_tag=excluded.image_tag, is_favorite=excluded.is_favorite, "
                    "library_id=excluded.library_id",
                    (
                        _g(a, "id"),
                        _g(a, "name"),
                        _g(a, "sort_name"),
                        _g(a, "album_artist_id"),
                        _g(a, "album_artist_name"),
                        _g(a, "year"),
                        _g(a, "date_created"),
                        _g(a, "image_tag"),
                        _int(_g(a, "is_favorite")),
                        _g(a, "library_id"),
                    ),
                )

        return self.write(fn)

    def upsert_tracks(self, tracks):
        """Upsert tracks. ``genres`` (list) is stored as a JSON array string.

        Also maintains the ``track_artists`` join from the track's primary
        ``artist_id`` (a track row knows its own album-artist; richer multi-
        artist data is populated by the sync layer when present).
        """

        def fn(conn):
            for t in tracks:
                genres = _g(t, "genres")
                genres_json = json.dumps(genres) if genres else None
                conn.execute(
                    "INSERT INTO tracks(id, name, album_id, album_name, artist_id, "
                    "artist_name, duration_ticks, index_number, parent_index_number, "
                    "year, genres, bitrate, codec, is_favorite, date_created, "
                    "library_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET name=excluded.name, "
                    "album_id=excluded.album_id, album_name=excluded.album_name, "
                    "artist_id=excluded.artist_id, artist_name=excluded.artist_name, "
                    "duration_ticks=excluded.duration_ticks, "
                    "index_number=excluded.index_number, "
                    "parent_index_number=excluded.parent_index_number, "
                    "year=excluded.year, genres=excluded.genres, "
                    "bitrate=excluded.bitrate, codec=excluded.codec, "
                    "is_favorite=excluded.is_favorite, "
                    "date_created=excluded.date_created, "
                    "library_id=excluded.library_id",
                    (
                        _g(t, "id"),
                        _g(t, "name"),
                        _g(t, "album_id"),
                        _g(t, "album_name"),
                        _g(t, "artist_id"),
                        _g(t, "artist_name"),
                        _g(t, "duration_ticks"),
                        _g(t, "index_number"),
                        _g(t, "parent_index_number"),
                        _g(t, "year"),
                        genres_json,
                        _g(t, "bitrate"),
                        _g(t, "codec"),
                        _int(_g(t, "is_favorite")),
                        _g(t, "date_created"),
                        _g(t, "library_id"),
                    ),
                )
                tid = _g(t, "id")
                aid = _g(t, "artist_id")
                if tid and aid:
                    conn.execute(
                        "INSERT OR IGNORE INTO track_artists(track_id, artist_id) "
                        "VALUES(?,?)",
                        (tid, aid),
                    )

        return self.write(fn)

    def upsert_genres(self, genres):
        def fn(conn):
            for g in genres:
                gid = _g(g, "id")
                name = _g(g, "name")
                if not name:
                    continue
                conn.execute(
                    "INSERT INTO genres(id, name) VALUES(?,?) "
                    "ON CONFLICT(id) DO UPDATE SET name=excluded.name",
                    (gid, name),
                )

        return self.write(fn)

    def upsert_playlists(self, playlists):
        def fn(conn):
            for p in playlists:
                conn.execute(
                    "INSERT INTO playlists(id, name, image_tag, track_count) "
                    "VALUES(?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET name=excluded.name, "
                    "image_tag=excluded.image_tag, track_count=excluded.track_count",
                    (
                        _g(p, "id"),
                        _g(p, "name"),
                        _g(p, "image_tag"),
                        _g(p, "track_count"),
                    ),
                )

        return self.write(fn)

    def set_playlist_tracks(self, playlist_id, track_ids):
        """Replace a playlist's ordered track list (0-based positions)."""

        def fn(conn):
            conn.execute(
                "DELETE FROM playlist_tracks WHERE playlist_id=?", (playlist_id,)
            )
            for pos, tid in enumerate(track_ids):
                conn.execute(
                    "INSERT INTO playlist_tracks(playlist_id, track_id, position) "
                    "VALUES(?,?,?)",
                    (playlist_id, tid, pos),
                )

        return self.write(fn)

    # ------------------------------------------------------------------ #
    # meta + ai_cache helpers.                                           #
    # ------------------------------------------------------------------ #

    def meta_get(self, key, default=None):
        row = self.read(
            lambda c: c.execute(
                "SELECT value FROM meta WHERE key=?", (key,)
            ).fetchone()
        )
        return row["value"] if row else default

    def meta_set(self, key, value):
        return self.write(
            lambda c: c.execute(
                "INSERT INTO meta(key, value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )
        )

    def ai_cache_get(self, kind, key):
        row = self.read(
            lambda c: c.execute(
                "SELECT payload FROM ai_cache WHERE kind=? AND key=?", (kind, key)
            ).fetchone()
        )
        return json.loads(row["payload"]) if row else None

    def ai_cache_set(self, kind, key, payload_dict):
        return self.write(
            lambda c: c.execute(
                "INSERT INTO ai_cache(kind, key, payload, created_at) "
                "VALUES(?,?,?,?) ON CONFLICT(kind, key) DO UPDATE SET "
                "payload=excluded.payload, created_at=excluded.created_at",
                (kind, key, json.dumps(payload_dict), _now_iso()),
            )
        )

    # ------------------------------------------------------------------ #
    # Counts.                                                            #
    # ------------------------------------------------------------------ #

    def _count(self, table):
        return self.read(
            lambda c: c.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        )

    def track_count(self):
        return self._count("tracks")

    def album_count(self):
        return self._count("albums")

    def artist_count(self):
        return self._count("artists")

    # ------------------------------------------------------------------ #
    # Play recording + history.                                          #
    # ------------------------------------------------------------------ #

    def record_play(self, track_id, played_at=None, source="local"):
        """Record one play: insert history + bump play_count + set last_played.

        All three happen in one writer transaction.
        """
        when = played_at or _now_iso()

        def fn(conn):
            conn.execute(
                "INSERT INTO play_history(track_id, played_at, source) "
                "VALUES(?,?,?)",
                (track_id, when, source),
            )
            cur = conn.execute(
                "UPDATE tracks SET play_count = play_count + 1, last_played=? "
                "WHERE id=?",
                (when, track_id),
            )
            if cur.rowcount == 0:
                # History row was still inserted above; only the counter bump
                # no-oped because no tracks row matched this id.
                _LOG.warning(
                    "record_play: no tracks row for id=%s; "
                    "history inserted but play_count not bumped",
                    track_id,
                )

        return self.write(fn)

    # ------------------------------------------------------------------ #
    # Query helpers (reads). Return lists of plain dicts.                #
    # ------------------------------------------------------------------ #

    def top_tracks(self, n):
        return self.read(
            lambda c: _dicts(
                c.execute(
                    "SELECT * FROM tracks WHERE play_count > 0 "
                    "ORDER BY play_count DESC, last_played DESC LIMIT ?",
                    (n,),
                ).fetchall()
            )
        )

    def recent_tracks(self, n):
        return self.read(
            lambda c: _dicts(
                c.execute(
                    "SELECT * FROM tracks WHERE last_played IS NOT NULL "
                    "ORDER BY last_played DESC LIMIT ?",
                    (n,),
                ).fetchall()
            )
        )

    def recents_mixed(self, n):
        """Last-played albums + playlists, mixed, deduped, newest first.

        Returns dicts with at least ``id``, ``name``, ``last_played`` and a
        ``kind`` of ``'album'`` or ``'playlist'``.
        """

        def fn(c):
            rows = c.execute(
                "SELECT id, name, image_tag, last_played, 'album' AS kind "
                "FROM albums WHERE last_played IS NOT NULL "
                "UNION ALL "
                "SELECT id, name, image_tag, last_played, 'playlist' AS kind "
                "FROM playlists WHERE last_played IS NOT NULL "
                "ORDER BY last_played DESC"
            ).fetchall()
            seen = set()
            out = []
            for r in rows:
                key = (r["kind"], r["id"])
                if key in seen:
                    continue
                seen.add(key)
                out.append(dict(r))
                if len(out) >= n:
                    break
            return out

        return self.read(fn)

    def albums_by_playcount(self, n, exclude_ids=()):
        """Albums ranked by aggregate track play_count (descending)."""
        exclude = list(exclude_ids)

        def fn(c):
            sql = (
                "SELECT al.*, "
                "COALESCE(SUM(t.play_count), 0) AS total_plays "
                "FROM albums al JOIN tracks t ON t.album_id = al.id "
            )
            params = []
            if exclude:
                placeholders = ",".join("?" for _ in exclude)
                sql += f"WHERE al.id NOT IN ({placeholders}) "
                params.extend(exclude)
            sql += (
                "GROUP BY al.id HAVING total_plays > 0 "
                "ORDER BY total_plays DESC LIMIT ?"
            )
            params.append(n)
            # Kind-tagged so Home's "Albums you'll enjoy" cards carry the
            # navigation action (clicking pushes the album page). Untagged rows
            # leave the card with no action -> a dead click.
            return _tagged(c.execute(sql, params).fetchall(), "album")

        return self.read(fn)

    def new_albums(self, n):
        # Kind-tagged: Home's "New albums" carousel cards need the album
        # navigation action so a click opens the album (untagged -> dead click).
        return self.read(
            lambda c: _tagged(
                c.execute(
                    "SELECT * FROM albums WHERE date_created IS NOT NULL "
                    "ORDER BY date_created DESC LIMIT ?",
                    (n,),
                ).fetchall(),
                "album",
            )
        )

    def recent_albums(self, n):
        """Recently-played albums (have a ``last_played``), newest first.

        Kind-tagged so the widget kit can build cards directly. Used by the Home
        page's "Recently played" carousel.
        """
        return self.read(
            lambda c: _tagged(
                c.execute(
                    "SELECT * FROM albums WHERE last_played IS NOT NULL "
                    "ORDER BY last_played DESC LIMIT ?",
                    (n,),
                ).fetchall(),
                "album",
            )
        )

    def favorite_artists(self, n=None):
        """Favorited artists, alphabetical. Kind-tagged for the widget kit."""

        def fn(c):
            sql = (
                "SELECT * FROM artists WHERE is_favorite=1 "
                "ORDER BY COALESCE(sort_name, name) COLLATE NOCASE"
            )
            params = ()
            if n is not None:
                sql += " LIMIT ?"
                params = (n,)
            return _tagged(c.execute(sql, params).fetchall(), "artist")

        return self.read(fn)

    def forgotten_favorites(self, n, now=None):
        """Tracks at/above the 75th percentile of played tracks' play_count
        whose ``last_played`` is older than 30 days. Ordered by play_count desc.

        ``now`` is injectable for deterministic tests; defaults to UTC now.
        """
        now = now or datetime.now(timezone.utc)
        cutoff = (now - timedelta(days=30)).isoformat()

        def fn(c):
            counts = [
                r["play_count"]
                for r in c.execute(
                    "SELECT play_count FROM tracks WHERE play_count > 0"
                ).fetchall()
            ]
            if not counts:
                return []
            counts.sort()
            # 75th percentile (nearest-rank).
            idx = max(0, int(round(0.75 * (len(counts) - 1))))
            threshold = counts[idx]
            return _dicts(
                c.execute(
                    "SELECT * FROM tracks WHERE play_count >= ? "
                    "AND last_played IS NOT NULL AND last_played < ? "
                    "ORDER BY play_count DESC LIMIT ?",
                    (threshold, cutoff, n),
                ).fetchall()
            )

        return self.read(fn)

    def months(self):
        """List of (YYYY-MM, play_count) from play_history, newest month first."""
        return self.read(
            lambda c: [
                (r["m"], r["n"])
                for r in c.execute(
                    "SELECT substr(played_at, 1, 7) AS m, COUNT(*) AS n "
                    "FROM play_history GROUP BY m ORDER BY m DESC"
                ).fetchall()
            ]
        )

    def month_top_tracks(self, yyyy_mm, n):
        return self.read(
            lambda c: _dicts(
                c.execute(
                    "SELECT t.*, COUNT(*) AS month_plays FROM play_history h "
                    "JOIN tracks t ON t.id = h.track_id "
                    "WHERE substr(h.played_at, 1, 7) = ? "
                    "GROUP BY t.id ORDER BY month_plays DESC, t.play_count DESC "
                    "LIMIT ?",
                    (yyyy_mm, n),
                ).fetchall()
            )
        )

    def month_top_albums(self, yyyy_mm, n=4):
        """Distinct albums most-played in ``yyyy_mm``, ordered by that month's
        play count (descending), limited to ``n``.

        Walks ``play_history -> tracks -> albums`` and counts plays whose
        ``played_at`` falls in the target month only (plays from other months
        are excluded by the ``substr(played_at, 1, 7)`` predicate). Returns
        kind-tagged album dicts (carrying ``id`` + ``image_tag``) so the Home
        month card can load each cell's cover via the image cache. Fewer than
        ``n`` distinct albums is fine; an empty month yields ``[]``.
        """
        return self.read(
            lambda c: _tagged(
                c.execute(
                    "SELECT al.*, COUNT(*) AS month_plays FROM play_history h "
                    "JOIN tracks t ON t.id = h.track_id "
                    "JOIN albums al ON al.id = t.album_id "
                    "WHERE substr(h.played_at, 1, 7) = ? "
                    "GROUP BY al.id "
                    "ORDER BY month_plays DESC, al.name COLLATE NOCASE "
                    "LIMIT ?",
                    (yyyy_mm, n),
                ).fetchall(),
                "album",
            )
        )

    def albums_for_tracks(self, track_ids, n=4):
        """Distinct albums of the given ``track_ids``, ranked by how many of
        those tracks belong to each album (descending), limited to ``n``.

        Drives a collage thumbnail for any track-id collection (AI mixes /
        radios). Returns kind-tagged album dicts (carrying ``id`` +
        ``image_tag``) so the Home collage card can load each cell's cover via
        the image cache. Empty / all-unknown id lists yield ``[]``; unknown ids
        are simply absent from the join.
        """
        ids = list(track_ids)
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        return self.read(
            lambda c: _tagged(
                c.execute(
                    "SELECT al.*, COUNT(*) AS member_tracks FROM tracks t "
                    "JOIN albums al ON al.id = t.album_id "
                    f"WHERE t.id IN ({placeholders}) "
                    "GROUP BY al.id "
                    "ORDER BY member_tracks DESC, al.name COLLATE NOCASE "
                    "LIMIT ?",
                    (*ids, n),
                ).fetchall(),
                "album",
            )
        )

    def search(self, q, limit=20):
        """NOCASE substring search; prefix matches rank ahead of mid-string.

        Returns a dict with keys ``artists``, ``albums``, ``tracks``,
        ``genres`` — each a list of dicts.
        """
        like = f"%{q}%"
        prefix = f"{q}%"

        def one(c, table, name_col="name"):
            return _dicts(
                c.execute(
                    f"SELECT * FROM {table} WHERE {name_col} LIKE ? "
                    "ORDER BY CASE WHEN {col} LIKE ? THEN 0 ELSE 1 END, "
                    "{col} COLLATE NOCASE LIMIT ?".format(col=name_col),
                    (like, prefix, limit),
                ).fetchall()
            )

        def fn(c):
            return {
                "artists": one(c, "artists"),
                "albums": one(c, "albums"),
                "tracks": one(c, "tracks"),
                "genres": one(c, "genres"),
            }

        return self.read(fn)

    # ------------------------------------------------------------------ #
    # Phase 5 paged browse helpers. Each returns kind-tagged dicts so the #
    # widget kit can build cards / track rows directly. The fetch fns are #
    # (offset, limit) so the auto-load widget can drive infinite scroll.  #
    # ------------------------------------------------------------------ #

    # Whitelisted sort columns per table (no user input reaches the SQL).
    _ALBUM_SORTS = {
        "sort_name": "COALESCE(sort_name, name) COLLATE NOCASE",
        "name": "COALESCE(sort_name, name) COLLATE NOCASE",
        "year": "year",
        "date_created": "date_created DESC",
    }
    _TRACK_SORTS = {
        "sort_name": "name COLLATE NOCASE",
        "name": "name COLLATE NOCASE",
        "album": "album_name COLLATE NOCASE, parent_index_number, index_number",
        "artist": "artist_name COLLATE NOCASE",
    }

    def albums_page(self, offset, limit, sort="sort_name", year=None):
        """Paged albums. Optional ``year`` narrows to albums with that year.

        The year filter is the Phase 7 headline feature: the same paged helper
        backs Collection's "Albums" full list and the Explore Years list. A
        ``year`` of None is unfiltered (back-compat).
        """
        order = self._ALBUM_SORTS.get(sort, self._ALBUM_SORTS["sort_name"])

        def fn(c):
            if year is None:
                sql = f"SELECT * FROM albums ORDER BY {order} LIMIT ? OFFSET ?"
                params = (limit, offset)
            else:
                sql = (
                    f"SELECT * FROM albums WHERE year=? ORDER BY {order} "
                    "LIMIT ? OFFSET ?"
                )
                params = (year, limit, offset)
            return _tagged(c.execute(sql, params).fetchall(), "album")

        return self.read(fn)

    def artists_page(self, offset, limit, year=None):
        """Paged artists. Optional ``year`` narrows to artists who have at
        least one album with that year (album-membership semantics)."""

        def fn(c):
            if year is None:
                sql = (
                    "SELECT * FROM artists "
                    "ORDER BY COALESCE(sort_name, name) COLLATE NOCASE "
                    "LIMIT ? OFFSET ?"
                )
                params = (limit, offset)
            else:
                sql = (
                    "SELECT ar.* FROM artists ar "
                    "WHERE EXISTS (SELECT 1 FROM albums al "
                    "WHERE al.album_artist_id = ar.id AND al.year = ?) "
                    "ORDER BY COALESCE(ar.sort_name, ar.name) COLLATE NOCASE "
                    "LIMIT ? OFFSET ?"
                )
                params = (year, limit, offset)
            return _tagged(c.execute(sql, params).fetchall(), "artist")

        return self.read(fn)

    # ------------------------------------------------------------------ #
    # Phase 7 Explore helpers — genres, decades, years, year filter.     #
    # ------------------------------------------------------------------ #

    def genres_with_counts(self):
        """``[(genre_name, track_count)]`` from tracks.genres JSON arrays.

        Uses ``json_each`` to unnest each track's genres JSON array and groups
        by genre name. The result is derived purely from tracks, so only genres
        that actually have ≥1 track tagged appear — a name present only in the
        ``genres`` table (no tagged tracks) is excluded. Ordered by track count
        descending, then name (NOCASE) as a tiebreak. A multi-genre track
        contributes to each of its genres. The Explore page hides the Genres
        row when this has <3 entries (live servers carry almost no genre
        metadata).
        """

        def fn(c):
            rows = c.execute(
                "SELECT g AS name, COUNT(*) AS n FROM ("
                "  SELECT je.value AS g FROM tracks t, json_each(t.genres) je "
                "  WHERE t.genres IS NOT NULL"
                ") GROUP BY g ORDER BY n DESC, g COLLATE NOCASE"
            ).fetchall()
            return [(r["name"], r["n"]) for r in rows]

        return self.read(fn)

    def decades(self):
        """``[(decade_start_year, track_count)]`` from tracks.year.

        NULL years are excluded. ``decade_start = year - (year % 10)`` so 1975
        and 1979 both bucket to 1970, 1999 -> 1990, 2000 -> 2000. Ordered by
        decade start descending (newest first).
        """

        def fn(c):
            rows = c.execute(
                "SELECT (year - (year % 10)) AS d, COUNT(*) AS n "
                "FROM tracks WHERE year IS NOT NULL "
                "GROUP BY d ORDER BY d DESC"
            ).fetchall()
            return [(r["d"], r["n"]) for r in rows]

        return self.read(fn)

    def years_in_decade(self, decade_start):
        """Distinct years present in ``[decade_start, decade_start+9]``, asc."""
        return self.read(
            lambda c: [
                r["year"]
                for r in c.execute(
                    "SELECT DISTINCT year FROM tracks "
                    "WHERE year IS NOT NULL AND year BETWEEN ? AND ? "
                    "ORDER BY year",
                    (decade_start, decade_start + 9),
                ).fetchall()
            ]
        )

    def album_years(self):
        """Distinct non-NULL album years, descending (for the year dropdown)."""
        return self.read(
            lambda c: [
                r["year"]
                for r in c.execute(
                    "SELECT DISTINCT year FROM albums "
                    "WHERE year IS NOT NULL ORDER BY year DESC"
                ).fetchall()
            ]
        )

    def genre_top_tracks(self, genre, n, offset=0):
        """Tracks tagged with ``genre`` (JSON membership), play_count desc.

        A multi-genre track matches each of its genres. Secondary sort by name
        keeps paging stable. Kind-tagged for the widget kit.
        """
        return self.read(
            lambda c: _tagged(
                c.execute(
                    "SELECT t.* FROM tracks t "
                    "WHERE t.genres IS NOT NULL AND EXISTS ("
                    "  SELECT 1 FROM json_each(t.genres) je WHERE je.value = ?"
                    ") ORDER BY t.play_count DESC, t.name COLLATE NOCASE "
                    "LIMIT ? OFFSET ?",
                    (genre, n, offset),
                ).fetchall(),
                "track",
            )
        )

    def genre_albums(self, genre, n, offset=0):
        """Albums that contain at least one track tagged ``genre``.

        Ranked by aggregate track play_count desc, then album name. Kind-tagged.
        """
        return self.read(
            lambda c: _tagged(
                c.execute(
                    "SELECT al.*, COALESCE(SUM(t.play_count), 0) AS total_plays "
                    "FROM albums al JOIN tracks t ON t.album_id = al.id "
                    "WHERE t.genres IS NOT NULL AND EXISTS ("
                    "  SELECT 1 FROM json_each(t.genres) je WHERE je.value = ?"
                    ") GROUP BY al.id "
                    "ORDER BY total_plays DESC, "
                    "COALESCE(al.sort_name, al.name) COLLATE NOCASE "
                    "LIMIT ? OFFSET ?",
                    (genre, n, offset),
                ).fetchall(),
                "album",
            )
        )

    def decade_top_tracks(self, start_year, n, offset=0, year=None):
        """Top tracks in a decade (play_count desc).

        ``year BETWEEN start_year..start_year+9`` by default; when ``year`` is
        given, narrows to exactly that year (the decade-page year-chip filter).
        NULL years never match. Kind-tagged.
        """

        def fn(c):
            if year is None:
                sql = (
                    "SELECT * FROM tracks "
                    "WHERE year IS NOT NULL AND year BETWEEN ? AND ? "
                    "ORDER BY play_count DESC, name COLLATE NOCASE "
                    "LIMIT ? OFFSET ?"
                )
                params = (start_year, start_year + 9, n, offset)
            else:
                sql = (
                    "SELECT * FROM tracks WHERE year = ? "
                    "ORDER BY play_count DESC, name COLLATE NOCASE "
                    "LIMIT ? OFFSET ?"
                )
                params = (year, n, offset)
            return _tagged(c.execute(sql, params).fetchall(), "track")

        return self.read(fn)

    def decade_albums(self, start_year, n, offset=0, year=None):
        """Albums in a decade (by aggregate play_count desc, then name).

        ``year BETWEEN start..start+9`` by default; ``year`` narrows to exactly
        that year. NULL album years never match. Kind-tagged.
        """

        def fn(c):
            if year is None:
                where = "al.year IS NOT NULL AND al.year BETWEEN ? AND ?"
                params = [start_year, start_year + 9]
            else:
                where = "al.year = ?"
                params = [year]
            sql = (
                "SELECT al.*, COALESCE(SUM(t.play_count), 0) AS total_plays "
                "FROM albums al LEFT JOIN tracks t ON t.album_id = al.id "
                f"WHERE {where} GROUP BY al.id "
                "ORDER BY total_plays DESC, "
                "COALESCE(al.sort_name, al.name) COLLATE NOCASE "
                "LIMIT ? OFFSET ?"
            )
            params.extend([n, offset])
            return _tagged(c.execute(sql, params).fetchall(), "album")

        return self.read(fn)

    def tracks_page(self, offset, limit, sort="sort_name"):
        order = self._TRACK_SORTS.get(sort, self._TRACK_SORTS["sort_name"])
        return self.read(
            lambda c: _tagged(
                c.execute(
                    f"SELECT * FROM tracks ORDER BY {order} LIMIT ? OFFSET ?",
                    (limit, offset),
                ).fetchall(),
                "track",
            )
        )

    def playlists_page(self, offset, limit):
        return self.read(
            lambda c: _tagged(
                c.execute(
                    "SELECT * FROM playlists "
                    "ORDER BY name COLLATE NOCASE LIMIT ? OFFSET ?",
                    (limit, offset),
                ).fetchall(),
                "playlist",
            )
        )

    def album_tracks(self, album_id):
        """All tracks on an album, ordered by disc then track number."""
        return self.read(
            lambda c: _tagged(
                c.execute(
                    "SELECT * FROM tracks WHERE album_id=? "
                    "ORDER BY COALESCE(parent_index_number, 0), "
                    "COALESCE(index_number, 0), name COLLATE NOCASE",
                    (album_id,),
                ).fetchall(),
                "track",
            )
        )

    def artist_albums(self, artist_id):
        """Albums where this artist is the album artist (sort_name order)."""
        return self.read(
            lambda c: _tagged(
                c.execute(
                    "SELECT * FROM albums WHERE album_artist_id=? "
                    "ORDER BY COALESCE(year, 0) DESC, "
                    "COALESCE(sort_name, name) COLLATE NOCASE",
                    (artist_id,),
                ).fetchall(),
                "album",
            )
        )

    def artist_appears_on(self, artist_id):
        """Albums containing tracks by this artist where they are NOT the
        album artist (guest appearances / compilations)."""
        return self.read(
            lambda c: _tagged(
                c.execute(
                    "SELECT DISTINCT al.* FROM albums al "
                    "JOIN tracks t ON t.album_id = al.id "
                    "WHERE t.artist_id = ? "
                    "AND (al.album_artist_id IS NULL OR al.album_artist_id != ?) "
                    "ORDER BY COALESCE(al.sort_name, al.name) COLLATE NOCASE",
                    (artist_id, artist_id),
                ).fetchall(),
                "album",
            )
        )

    def artist_top_tracks(self, artist_id, n):
        """This artist's tracks ranked by play_count (descending).

        Phase 5: ranked purely by local play_count. AI-ranked "Popular" lands
        in Phase 8 and will layer on top of this.
        """
        return self.read(
            lambda c: _tagged(
                c.execute(
                    "SELECT * FROM tracks WHERE artist_id=? "
                    "ORDER BY play_count DESC, last_played DESC, "
                    "name COLLATE NOCASE LIMIT ?",
                    (artist_id, n),
                ).fetchall(),
                "track",
            )
        )

    def playlist_tracks(self, playlist_id):
        """A playlist's tracks in playlist order (position ascending)."""
        return self.read(
            lambda c: _tagged(
                c.execute(
                    "SELECT t.* FROM playlist_tracks pt "
                    "JOIN tracks t ON t.id = pt.track_id "
                    "WHERE pt.playlist_id=? ORDER BY pt.position",
                    (playlist_id,),
                ).fetchall(),
                "track",
            )
        )

    # ------------------------------------------------------------------ #
    # Favorite write-through (local mirror of a server set_favorite).    #
    # ------------------------------------------------------------------ #

    _FAVORITE_TABLES = {
        "track": "tracks",
        "album": "albums",
        "artist": "artists",
    }

    def set_favorite_local(self, kind, item_id, fav):
        """Set the local ``is_favorite`` flag for a track/album/artist.

        ``kind`` is one of ``"track"``, ``"album"``, ``"artist"``. Raises
        ``ValueError`` for any other kind (genres/playlists have no favorite
        column). Used by the favorite write-through helper alongside the
        server-side ``client.set_favorite``.
        """
        table = self._FAVORITE_TABLES.get(kind)
        if table is None:
            raise ValueError(f"set_favorite_local: unsupported kind {kind!r}")
        return self.write(
            lambda c: c.execute(
                f"UPDATE {table} SET is_favorite=? WHERE id=?",
                (1 if fav else 0, item_id),
            )
        )

    # ------------------------------------------------------------------ #
    # Phase 8 AI helpers — id-ordered track resolution + artist bio.     #
    # ------------------------------------------------------------------ #

    def tracks_by_ids(self, ids):
        """Resolve ``ids`` to track dicts **in the order given**.

        Unknown ids are dropped. Kind-tagged (``"track"``) so the widget kit can
        build track rows directly. Used to turn an AI-returned ``track_ids`` list
        (a mix / radio) into playable, ordered tracks — preserving the model's
        ranking is the whole point, so SQL ``IN`` (which loses order) is
        re-sorted by the input position here.
        """
        ids = list(ids)
        if not ids:
            return []

        def fn(c):
            placeholders = ",".join("?" for _ in ids)
            rows = c.execute(
                f"SELECT * FROM tracks WHERE id IN ({placeholders})", ids
            ).fetchall()
            by_id = {r["id"]: r for r in rows}
            out = []
            for tid in ids:
                r = by_id.get(tid)
                if r is not None:
                    d = dict(r)
                    d["kind"] = "track"
                    out.append(d)
            return out

        return self.read(fn)

    def set_artist_bio(self, artist_id, bio, source="ai"):
        """Store an artist bio + stamp ``bio_updated_at`` (UTC ISO).

        ``source`` records provenance in ``bio_source`` ('ai' | 'jellyfin').
        Defaults to 'ai' — the AI discovery path is the primary caller; the
        sync layer passes source='jellyfin' for server-supplied Overview bios.
        """
        return self.write(
            lambda c: c.execute(
                "UPDATE artists SET bio=?, bio_updated_at=?, bio_source=? "
                "WHERE id=?",
                (bio, _now_iso(), source, artist_id),
            )
        )
