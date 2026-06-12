# catalog.py
#
# Copyright (C) 2026 Tyler Reece
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Candidate catalog builder — the enforcement arm of the cardinal rule.

*The model never invents tracks.* Every AI prompt embeds a candidate catalog
exported from SQLite (``id\\tartist\\ttitle\\tgenres\\tyear`` lines, capped by
adjacency to the seed). The model is told to select ONLY from CANDIDATES; the
returned ids are then validated against the DB by :func:`validate_ids` before
anything reaches the player.

Adjacency strategy (seed-based)
-------------------------------
1. Same-artist tracks (the strongest signal).
2. Tracks by artists that share a genre with the seed (when genre metadata
   exists — the live server carries almost none).
3. Year-proximity fill (±5 years) — the genre-sparse fallback that lets the
   model's own knowledge of artist similarity do the ranking.
4. Top-played fill to reach the cap.

No gi imports — operates purely on a ``Database`` via ``db.read``.
"""

import json
import logging

logger = logging.getLogger(__name__)

# Hard cap on the formatted-catalog string handed to the model (prompt budget).
_FORMAT_BUDGET_CHARS = 25000

# Year-proximity window for the genre-sparse fallback.
_YEAR_WINDOW = 5

# 90-day listening-profile window (days) for profile-based features.
_PROFILE_DAYS = 90


def _seed_row(db, seed_track_id):
    return db.read(
        lambda c: c.execute(
            "SELECT * FROM tracks WHERE id=?", (seed_track_id,)
        ).fetchone()
    )


def _seed_genres(row):
    if row is None:
        return []
    raw = row["genres"]
    if not raw:
        return []
    try:
        vals = json.loads(raw)
        return [v for v in vals if v]
    except (ValueError, TypeError):
        return []


def candidates_for_seed(db, seed_track_id, cap=400):
    """Build a candidate track list adjacent to ``seed_track_id`` (cap rows).

    Returns a list of track dicts (the seed excluded, deduped, capped). The
    layers are appended in priority order — same-artist, genre-share, year-
    proximity, top-played fill — and truncated at ``cap``.
    """
    seed = _seed_row(db, seed_track_id)
    seed_artist = seed["artist_id"] if seed else None
    seed_year = seed["year"] if seed else None
    genres = _seed_genres(seed)

    out = []
    seen = {seed_track_id}

    def add(rows):
        for r in rows:
            d = dict(r)
            tid = d["id"]
            if tid in seen:
                continue
            seen.add(tid)
            out.append(d)
            if len(out) >= cap:
                return True
        return False

    def run(sql, params):
        return db.read(lambda c: c.execute(sql, params).fetchall())

    # 1. Same-artist tracks.
    if seed_artist:
        if add(run(
            "SELECT * FROM tracks WHERE artist_id=? "
            "ORDER BY play_count DESC, name COLLATE NOCASE",
            (seed_artist,),
        )):
            return out

    # 2. Genre-sharing artists' tracks (when the seed has genre metadata).
    for g in genres:
        if add(run(
            "SELECT t.* FROM tracks t WHERE t.genres IS NOT NULL AND EXISTS ("
            "  SELECT 1 FROM json_each(t.genres) je WHERE je.value=?"
            ") ORDER BY t.play_count DESC, t.name COLLATE NOCASE",
            (g,),
        )):
            return out

    # 3. Year-proximity fill (genre-sparse fallback / additional adjacency).
    if seed_year is not None:
        if add(run(
            "SELECT * FROM tracks WHERE year IS NOT NULL "
            "AND year BETWEEN ? AND ? "
            "ORDER BY play_count DESC, name COLLATE NOCASE",
            (seed_year - _YEAR_WINDOW, seed_year + _YEAR_WINDOW),
        )):
            return out

    # 4. Top-played fill to reach the cap.
    add(run(
        "SELECT * FROM tracks ORDER BY play_count DESC, last_played DESC, "
        "name COLLATE NOCASE LIMIT ?",
        (cap,),
    ))
    return out


def candidates_for_profile(db, cap=400):
    """Build a candidate list from the user's listening profile.

    Top artists' tracks (90-day window) + recent favorites + top-played + a
    random fill, deduped and capped. Used by daily mixes / personal radios where
    there is no single seed track.
    """
    out = []
    seen = set()

    def add(rows):
        for r in rows:
            d = dict(r)
            tid = d["id"]
            if tid in seen:
                continue
            seen.add(tid)
            out.append(d)
            if len(out) >= cap:
                return True
        return False

    def run(sql, params=()):
        return db.read(lambda c: c.execute(sql, params).fetchall())

    # 1. Tracks by the user's most-played artists (90-day window).
    if add(run(
        "SELECT t.* FROM tracks t WHERE t.artist_id IN ("
        "  SELECT t2.artist_id FROM play_history h "
        "  JOIN tracks t2 ON t2.id = h.track_id "
        "  WHERE h.played_at >= datetime('now', ?) "
        "  AND t2.artist_id IS NOT NULL "
        "  GROUP BY t2.artist_id ORDER BY COUNT(*) DESC LIMIT 20"
        ") ORDER BY t.play_count DESC, t.name COLLATE NOCASE",
        (f"-{_PROFILE_DAYS} days",),
    )):
        return out

    # 2. Recent favorites.
    if add(run(
        "SELECT * FROM tracks WHERE is_favorite=1 "
        "ORDER BY last_played DESC, name COLLATE NOCASE"
    )):
        return out

    # 3. Top played.
    if add(run(
        "SELECT * FROM tracks WHERE play_count > 0 "
        "ORDER BY play_count DESC, last_played DESC"
    )):
        return out

    # 4. Random fill.
    add(run(
        "SELECT * FROM tracks ORDER BY RANDOM() LIMIT ?", (cap,)
    ))
    return out


def _genres_str(track) -> str:
    raw = track.get("genres")
    if not raw:
        return ""
    if isinstance(raw, list):
        vals = raw
    else:
        try:
            vals = json.loads(raw)
        except (ValueError, TypeError):
            return ""
    return ", ".join(str(v) for v in vals if v)


def format_catalog(tracks) -> str:
    """Render tracks as ``id\\tartist\\ttitle\\tgenres\\tyear`` lines.

    Truncated to the prompt budget (≤ 25k chars): rows are appended until the
    next row would breach the budget, then formatting stops.
    """
    lines = []
    total = 0
    for t in tracks:
        tid = t.get("id") or ""
        artist = (t.get("artist_name") or "").replace("\t", " ").replace("\n", " ")
        title = (t.get("name") or "").replace("\t", " ").replace("\n", " ")
        genres = _genres_str(t).replace("\t", " ").replace("\n", " ")
        year = t.get("year")
        year_s = str(year) if year is not None else ""
        line = f"{tid}\t{artist}\t{title}\t{genres}\t{year_s}"
        # +1 for the newline join cost.
        add_len = len(line) + (1 if lines else 0)
        if total + add_len > _FORMAT_BUDGET_CHARS:
            break
        lines.append(line)
        total += add_len
    return "\n".join(lines)


def profile_summary(db) -> str:
    """Compact text summary of the user's top artists / genres / tracks (90d)."""
    window = f"-{_PROFILE_DAYS} days"

    def fn(c):
        artists = [
            r["artist_name"]
            for r in c.execute(
                "SELECT t.artist_name, COUNT(*) AS n FROM play_history h "
                "JOIN tracks t ON t.id = h.track_id "
                "WHERE h.played_at >= datetime('now', ?) "
                "AND t.artist_name IS NOT NULL "
                "GROUP BY t.artist_name ORDER BY n DESC LIMIT 10",
                (window,),
            ).fetchall()
        ]
        tracks = [
            f"{r['artist_name']} — {r['name']}"
            for r in c.execute(
                "SELECT t.name, t.artist_name, COUNT(*) AS n FROM play_history h "
                "JOIN tracks t ON t.id = h.track_id "
                "WHERE h.played_at >= datetime('now', ?) "
                "GROUP BY t.id ORDER BY n DESC LIMIT 10",
                (window,),
            ).fetchall()
        ]
        genres = [
            r["g"]
            for r in c.execute(
                "SELECT je.value AS g, COUNT(*) AS n FROM play_history h "
                "JOIN tracks t ON t.id = h.track_id, json_each(t.genres) je "
                "WHERE h.played_at >= datetime('now', ?) AND t.genres IS NOT NULL "
                "GROUP BY g ORDER BY n DESC LIMIT 10",
                (window,),
            ).fetchall()
        ]
        return artists, genres, tracks

    artists, genres, tracks = db.read(fn)
    # Fall back to all-time top artists/tracks if the 90-day window is empty
    # (a freshly-synced library has no local play history yet).
    if not artists and not tracks:
        def all_time(c):
            a = [
                r["artist_name"]
                for r in c.execute(
                    "SELECT artist_name, SUM(play_count) AS p FROM tracks "
                    "WHERE artist_name IS NOT NULL GROUP BY artist_name "
                    "ORDER BY p DESC LIMIT 10"
                ).fetchall()
            ]
            t = [
                f"{r['artist_name']} — {r['name']}"
                for r in c.execute(
                    "SELECT name, artist_name FROM tracks "
                    "ORDER BY play_count DESC LIMIT 10"
                ).fetchall()
            ]
            return a, t
        artists, tracks = db.read(all_time)

    parts = []
    if artists:
        parts.append("Top artists: " + ", ".join(artists))
    if genres:
        parts.append("Top genres: " + ", ".join(genres))
    if tracks:
        parts.append("Top tracks: " + "; ".join(tracks))
    return "\n".join(parts)


def validate_ids(db, ids):
    """Validate ``ids`` against the DB (cardinal-rule gate).

    Returns ``(valid_ids, valid_fraction)``: the subset of ``ids`` that exist in
    ``tracks``, in the order given (deduped), and the fraction of the *input*
    ids that were valid (0.0 for empty input). Callers drop AI results whose
    fraction is below 0.5 and fall back.
    """
    ids = list(ids)
    if not ids:
        return [], 0.0
    # Dedup the input preserving order for the lookup set.
    unique = []
    seen = set()
    for i in ids:
        if i not in seen:
            seen.add(i)
            unique.append(i)
    placeholders = ",".join("?" for _ in unique)
    present = db.read(
        lambda c: {
            r["id"]
            for r in c.execute(
                f"SELECT id FROM tracks WHERE id IN ({placeholders})", unique
            ).fetchall()
        }
    )
    valid = [i for i in unique if i in present]
    # Fraction is against the raw input length (matches "if <50% valid" intent).
    fraction = len(valid) / len(ids)
    return valid, fraction
