# lyrics_providers.py
#
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
# SPDX-License-Identifier: GPL-3.0-or-later

"""Pure, headless-testable external lyrics fetching (LRCLIB).

No gi imports. The only third-party dependency is ``requests`` (the default
``http_get``); tests inject a fake ``http_get`` so the network is never hit.

The public surface is two functions:

* ``parse_lrc(text)`` — parse LRC text into ``[(line, start_ticks), ...]`` where
  ``start_ticks`` is in Jellyfin 100ns units (or ``None`` for untimed lines),
  matching the shape the lyrics widget already consumes.
* ``fetch_lrclib(artist, title, ...)`` — query LRCLIB's ``/api/get`` endpoint and
  return parsed lyrics (synced preferred, plain fallback) or ``None`` on miss.
"""

import logging
import re

logger = logging.getLogger(__name__)

# Jellyfin reports lyric offsets in ticks (100ns units): 1s == 10_000_000.
TICKS_PER_SECOND = 10_000_000

_TIMEOUT = 10

# LRCLIB asks clients to identify themselves via User-Agent.
# https://lrclib.net/docs
_USER_AGENT = "Timbre/0.1.0 (https://github.com/tylerreece/timbre)"

_LRCLIB_GET_URL = "https://lrclib.net/api/get"
_LRCLIB_SEARCH_URL = "https://lrclib.net/api/search"

# When we have a duration, a search candidate within this many seconds is
# treated as a duration match (deluxe re-rips, slightly different masters).
_DURATION_TOLERANCE_SECS = 7

# A leading [mm:ss] / [mm:ss.xx] / [mm:ss.xxx] timestamp tag.
_TIMESTAMP_RE = re.compile(r"\[(\d{1,3}):(\d{2})(?:\.(\d{1,3}))?\]")
# A metadata tag like [ar:...], [ti:...], [length:...] — letters then a colon.
_METADATA_RE = re.compile(r"^\[[a-zA-Z]+:")


def _fraction_to_ticks(frac: str) -> int:
    """Convert the hundredths/milliseconds fraction string to ticks.

    "5" -> .5s, "50" -> .50s, "250" -> .250s. Normalize to a float second
    fraction by dividing by 10**len, then scale to ticks.
    """
    if not frac:
        return 0
    value = int(frac) / (10 ** len(frac))
    return round(value * TICKS_PER_SECOND)


def parse_lrc(text):
    """Parse LRC ``text`` into ``[(line, start_ticks|None), ...]``.

    * ``[mm:ss.xx]`` timestamps become ticks (100ns units).
    * A line may carry several timestamps (repeated chorus) — one entry each.
    * A line with no timestamp is emitted with ``None`` ticks (plain lyric).
    * Metadata tags (``[ar:]``, ``[ti:]``, ``[length:]``, …) are skipped.
    * Truly blank lines are dropped; a timed-but-empty line (instrumental gap)
      is kept with empty text so the widget can show the gap.
    """
    if not text:
        return []

    out = []
    for raw in text.splitlines():
        line = raw.rstrip("\r")
        stripped = line.strip()
        if not stripped:
            continue
        # Pure metadata tag (e.g. [ar:Foo]) with no following lyric -> skip.
        if _METADATA_RE.match(stripped) and not _TIMESTAMP_RE.match(stripped):
            continue

        # Collect all leading timestamps, then the trailing lyric text.
        timestamps = []
        rest = line
        while True:
            m = _TIMESTAMP_RE.match(rest)
            if not m:
                break
            minutes, seconds, frac = m.group(1), m.group(2), m.group(3)
            ticks = (
                (int(minutes) * 60 + int(seconds)) * TICKS_PER_SECOND
                + _fraction_to_ticks(frac)
            )
            timestamps.append(ticks)
            rest = rest[m.end():]

        if timestamps:
            lyric = rest.strip()
            for ticks in timestamps:
                out.append((lyric, ticks))
        else:
            # No timestamp at all -> plain (untimed) lyric line.
            out.append((stripped, None))
    return out


def _split_plain(text):
    """Split plain-text lyrics into untimed ``(line, None)`` entries."""
    out = []
    for raw in text.splitlines():
        line = raw.rstrip("\r").strip()
        if line:
            out.append((line, None))
    return out


class _NetworkError(Exception):
    """Internal: an HTTP call raised (DNS/timeout/connection).

    Distinct from a content miss (404 / empty body). A network error aborts
    the whole fallback chain — retrying the next stage against the same dead
    network would only stack timeouts — whereas a miss falls through to the
    next stage.
    """


def _norm(value):
    """Strip and collapse internal whitespace; ``None``/empty -> ``None``."""
    if not value:
        return None
    collapsed = " ".join(str(value).split())
    return collapsed or None


def _lyrics_from_record(record):
    """Extract parsed lyrics from one LRCLIB record (synced preferred)."""
    if not isinstance(record, dict):
        return None
    synced = record.get("syncedLyrics") or ""
    if synced.strip():
        lines = parse_lrc(synced)
        if lines:
            return lines
    plain = record.get("plainLyrics") or ""
    if plain.strip():
        lines = _split_plain(plain)
        if lines:
            return lines
    return None


def _http_json(http_get, url, params, headers):
    """GET ``url`` and return decoded JSON, or ``None`` on a content miss.

    Raises ``_NetworkError`` if the request itself fails (so the caller can
    abort the chain). A 404 / non-200 / bad-JSON body is a *miss* -> ``None``.
    """
    try:
        resp = http_get(url, params=params, headers=headers, timeout=_TIMEOUT)
    except Exception as exc:
        logger.debug("lrclib request failed", exc_info=True)
        raise _NetworkError(str(exc)) from exc

    status = getattr(resp, "status_code", None)
    if status == 404:
        return None
    if status != 200:
        logger.debug("lrclib unexpected status %s", status)
        return None
    try:
        return resp.json()
    except Exception:
        logger.debug("lrclib bad json", exc_info=True)
        return None


def _best_search_candidate(records, duration_secs):
    """Pick the best record from a search result array.

    LRCLIB's search is fuzzy: a query for a track it does *not* have can still
    return a different song from the same album/artist. Returning that song's
    lyrics is worse than "no lyrics," so duration is used as a **gate**, not
    just a tiebreak:

    * When we have a duration, only candidates within
      ``±_DURATION_TOLERANCE_SECS`` are eligible — the closest such match
      (that yields lyrics) wins, and if none qualify we return ``None`` rather
      than guess a wrong-length song.
    * When we have no duration to gate on, fall back to the first record that
      yields any lyrics.
    """
    if not isinstance(records, list):
        return None

    if duration_secs:
        try:
            want = float(duration_secs)
        except (TypeError, ValueError):
            want = None
    else:
        want = None

    if want is not None:
        best = None
        best_delta = None
        for rec in records:
            if not isinstance(rec, dict):
                continue
            try:
                delta = abs(float(rec.get("duration")) - want)
            except (TypeError, ValueError):
                continue
            if delta > _DURATION_TOLERANCE_SECS:
                continue
            lines = _lyrics_from_record(rec)
            if not lines:
                continue
            if best_delta is None or delta < best_delta:
                best, best_delta = lines, delta
        return best

    for rec in records:
        lines = _lyrics_from_record(rec)
        if lines:
            return lines
    return None


def fetch_lrclib(
    artist, title, album=None, duration_secs=None, http_get=None
):
    """Fetch lyrics for a track from LRCLIB with a robust fallback chain.

    Returns ``[(line, start_ticks|None), ...]`` on a hit (synced preferred,
    plain fallback), or ``None`` on a miss. Never raises.

    LRCLIB's ``/api/get`` matches metadata *exactly*: a deluxe-edition album
    name, a duration off by a few seconds, or a stray apostrophe makes a
    known-good track 404. So we fall through:

    1. ``/api/get`` with every field we have (artist, title, album, duration).
    2. ``/api/get`` with artist + title only (drops the strict album/duration).
    3. ``/api/search?q=<artist title>`` -> pick the best candidate (duration
       within tolerance + has lyrics, else first with any lyrics).

    A *content* miss (404 / empty) at one stage falls through to the next; a
    *network* error aborts the chain (the next stage would hit the same dead
    network). ``http_get`` is injectable for tests; defaults to ``requests.get``.
    """
    if http_get is None:
        import requests
        http_get = requests.get

    artist = _norm(artist)
    title = _norm(title)
    album = _norm(album)
    if not artist or not title:
        return None

    headers = {"User-Agent": _USER_AGENT}

    try:
        # Stage 1: exact get with all available fields.
        params = {"artist_name": artist, "track_name": title}
        if album:
            params["album_name"] = album
        if duration_secs:
            params["duration"] = duration_secs
        data = _http_json(http_get, _LRCLIB_GET_URL, params, headers)
        lines = _lyrics_from_record(data)
        if lines:
            return lines

        # Stage 2: relax to artist + title only (skip if stage 1 was already
        # that — no extra album/duration constraint to drop).
        if album or duration_secs:
            params = {"artist_name": artist, "track_name": title}
            data = _http_json(http_get, _LRCLIB_GET_URL, params, headers)
            lines = _lyrics_from_record(data)
            if lines:
                return lines

        # Stage 3: fuzzy search, rank candidates by duration proximity.
        params = {"q": f"{artist} {title}"}
        records = _http_json(http_get, _LRCLIB_SEARCH_URL, params, headers)
        lines = _best_search_candidate(records, duration_secs)
        if lines:
            return lines
    except _NetworkError:
        return None

    return None
