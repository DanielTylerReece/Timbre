# discovery.py
#
# Copyright (C) 2026 Tyler Reece
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""AI discovery features — the orchestration layer above provider + catalog.

Every public method returns a plain dict the UI consumes uniformly and NEVER
raises on AI failure: each feature has a deterministic local fallback (Jellyfin
instant_mix for track radio; play-count / top-artist / decade heuristics for
mixes, radios, and artist popularity). The cardinal rule is enforced here — the
AI is only ever asked to *select and order ids from a candidate catalog*, and
every returned id is validated against the DB (``catalog.validate_ids``) before
it reaches the player; an under-50%-valid or failed call is discarded for the
fallback.

No gi imports. Clock (`now`) and the provider factory are injected so freshness
windows and AI behaviour are deterministic in tests.
"""

import logging
from datetime import datetime, timezone

from . import catalog
from .provider import AIError

logger = logging.getLogger(__name__)

# Minimum fraction of AI-returned ids that must validate against the DB to
# accept the AI result (else fall back). Cardinal-rule threshold.
_MIN_VALID_FRACTION = 0.5

# Personal radios rebuild weekly; mixes rebuild daily (date-keyed via meta).
_RADIO_MAX_AGE_DAYS = 7

_MIX_COUNT = 4
_RADIO_COUNT = 3
_MIX_SIZE = 20
_RADIO_SIZE = 20

_SYSTEM_SELECT = (
    "You are a music curator. You are given a numbered CANDIDATES catalog of "
    "tracks (one per line as 'id\\tartist\\ttitle\\tgenres\\tyear'). Select "
    "ONLY track ids that appear in CANDIDATES — never invent or modify an id. "
    "Return strictly valid JSON and nothing else."
)


def _parse_dt(s):
    """Parse an ISO timestamp (tolerant of a trailing 'Z')."""
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


class Discovery:
    """AI discovery feature set bound to a DB + Jellyfin client + provider."""

    def __init__(self, db, client, provider_factory, now=None,
                 push_bios=None):
        self.db = db
        self.client = client
        self._provider_factory = provider_factory
        self._now = now or (lambda: datetime.now(timezone.utc))
        # Zero-arg callable returning the live "push AI bios to Jellyfin"
        # preference (or a bool). A callable keeps the setting live (no
        # restart) while keeping this module gi-free. Defaults to off.
        if push_bios is None:
            self._push_bios = lambda: False
        elif callable(push_bios):
            self._push_bios = push_bios
        else:
            self._push_bios = lambda: bool(push_bios)

    # ------------------------------------------------------------------ #
    # Internal helpers                                                   #
    # ------------------------------------------------------------------ #

    def _provider(self):
        try:
            return self._provider_factory()
        except Exception:  # noqa: BLE001 — factory failure == no AI
            logger.debug("provider factory failed", exc_info=True)
            return None

    def _today(self):
        return self._now().date().isoformat()

    def _select_ids(self, provider, candidates, instruction, max_tokens=2000):
        """Ask the model to pick ids from ``candidates``; validate against DB.

        Returns ``(valid_ids, ok)``. ``ok`` is False when the call failed, the
        response had no usable ids, or under 50% validated — the caller then
        falls back. ``candidates`` is the catalog track-dict list.
        """
        cat_text = catalog.format_catalog(candidates)
        user = f"{instruction}\n\nCANDIDATES:\n{cat_text}"
        try:
            data = provider.complete_json(
                _SYSTEM_SELECT, user, max_tokens=max_tokens
            )
        except AIError:
            logger.info("AI select failed; falling back", exc_info=True)
            return [], False
        except Exception:  # noqa: BLE001 — any provider bug -> fallback
            logger.warning("AI select unexpected error; falling back",
                           exc_info=True)
            return [], False
        ids = self._extract_ids(data)
        if not ids:
            return [], False
        valid, frac = catalog.validate_ids(self.db, ids)
        if frac < _MIN_VALID_FRACTION or not valid:
            logger.info("AI result discarded (%.0f%% valid)", frac * 100)
            return [], False
        return valid, True

    @staticmethod
    def _extract_ids(data):
        """Pull a list of track ids out of a model response (dict or list)."""
        if isinstance(data, dict):
            ids = data.get("track_ids") or data.get("ids") or []
        elif isinstance(data, list):
            ids = data
        else:
            ids = []
        return [str(i) for i in ids if isinstance(i, (str, int))]

    # ------------------------------------------------------------------ #
    # Track radio                                                        #
    # ------------------------------------------------------------------ #

    def track_radio(self, seed_track_id, n=30):
        """Ordered track radio seeded by ``seed_track_id``.

        AI path (cardinal rule) with a Jellyfin ``instant_mix`` fallback.
        Session-scoped cache (kind ``track_radio``, key = seed): a same-day hit
        is reused; a stale (different-day) row is rebuilt.
        Returns ``{"source": "ai"|"instant_mix", "tracks": [...]}``.
        """
        cached = self.db.ai_cache_get("track_radio", seed_track_id)
        if cached is not None and cached.get("day") == self._today():
            tracks = self.db.tracks_by_ids(cached.get("track_ids", []))
            if tracks:
                return {"source": cached.get("source", "ai"), "tracks": tracks}

        provider = self._provider()
        if provider is not None:
            candidates = catalog.candidates_for_seed(self.db, seed_track_id)
            instruction = (
                f"Build a {n}-track radio playlist that flows from the seed "
                f"track id '{seed_track_id}'. Order by listening flow. Exclude "
                f"the seed itself. Return JSON {{\"track_ids\": [...]}}."
            )
            ids, ok = self._select_ids(provider, candidates, instruction)
            ids = [i for i in ids if i != seed_track_id][:n]
            if ok and ids:
                self._cache_set("track_radio", seed_track_id,
                                {"track_ids": ids, "source": "ai",
                                 "day": self._today()})
                return {"source": "ai", "tracks": self.db.tracks_by_ids(ids)}

        return self._track_radio_fallback(seed_track_id, n)

    def _track_radio_fallback(self, seed_track_id, n):
        Track = _import_track_model()

        tracks = []
        if self.client is not None:
            try:
                items = self.client.instant_mix(seed_track_id, limit=n)
                tracks = [Track.from_item(it) for it in items]
            except Exception:  # noqa: BLE001 — UI must never see this
                logger.info("instant_mix fallback failed", exc_info=True)
                tracks = []
        return {"source": "instant_mix", "tracks": tracks}

    def extend_radio(self, seed_track_id, exclude_ids, n=15):
        """Next radio batch seeded by ``seed_track_id``, excluding ``exclude_ids``.

        No caching (always a fresh continuation). Falls back to instant_mix
        filtered by the exclude set. Returns the same dict shape as track_radio.
        """
        exclude = set(exclude_ids or ())
        exclude.add(seed_track_id)
        provider = self._provider()
        if provider is not None:
            candidates = [
                c for c in catalog.candidates_for_seed(self.db, seed_track_id)
                if c["id"] not in exclude
            ]
            instruction = (
                f"Continue a radio station seeded by track id "
                f"'{seed_track_id}'. Pick the next {n} tracks. Do NOT repeat "
                f"any already-played id. Return JSON {{\"track_ids\": [...]}}."
            )
            ids, ok = self._select_ids(provider, candidates, instruction)
            ids = [i for i in ids if i not in exclude][:n]
            if ok and ids:
                return {"source": "ai", "tracks": self.db.tracks_by_ids(ids)}

        # Fallback: instant_mix filtered by the exclude set.
        res = self._track_radio_fallback(seed_track_id, n + len(exclude))
        res["tracks"] = [
            t for t in res["tracks"]
            if getattr(t, "id", None) not in exclude
        ][:n]
        return res

    # ------------------------------------------------------------------ #
    # Daily mixes                                                        #
    # ------------------------------------------------------------------ #

    def daily_mixes(self, force=False):
        """Four daily mixes ``[{name, description, track_ids}]``.

        Rebuilt only when ``meta['mixes_built_date'] != today``; otherwise served
        from the ``mix`` cache (keys '1'..'4'). Falls back to heuristic mixes
        when no provider is configured or the AI calls fail.

        ``force=True`` (the Custom-mixes refresh button) bypasses the same-day
        stamp and rebuilds unconditionally — AI when a provider is configured,
        else the heuristic set, same as the normal path. A successful rebuild
        re-stamps today's date so the daily logic stays coherent; if the rebuild
        yields nothing (it should not — fallback always fills) the previous
        cached set and its stamp are left untouched so the section never blanks.
        """
        today = self._today()
        if not force and self.db.meta_get("mixes_built_date") == today:
            cached = self._load_cached_set("mix", _MIX_COUNT)
            if cached is not None:
                return cached

        provider = self._provider()
        mixes = None
        if provider is not None:
            mixes = self._build_mixes_ai(provider)
        if not mixes:
            mixes = self._mixes_fallback()
        # Defensive: a forced rebuild must never overwrite the cache (and stamp)
        # with an empty result — keep the prior mixes intact on the no-op case.
        if not mixes:
            return self.daily_mixes_cached()

        for i, m in enumerate(mixes, start=1):
            self._cache_set("mix", str(i), m)
        self.db.meta_set("mixes_built_date", today)
        return mixes

    def daily_mixes_cached(self):
        """Cheap, network-free daily mixes for the first page paint.

        Returns the cached ``mix`` set (keys '1'..'4') if present — *even when
        stale* (a different day) — otherwise the deterministic heuristic set.
        NEVER constructs a provider or makes a network call. Pair with
        :meth:`needs_mixes_rebuild` to decide whether to kick a background
        :meth:`daily_mixes` refresh after the page renders.
        """
        cached = self._load_cached_set("mix", _MIX_COUNT)
        if cached is not None:
            return cached
        return self._mixes_fallback()

    def needs_mixes_rebuild(self):
        """True when the daily mixes were last built before today."""
        return self.db.meta_get("mixes_built_date") != self._today()

    # The four daily-mix themes (name hint + description hint). The order and
    # names are load-bearing: the UI and the 'mix' cache keys '1'..'4' assume
    # exactly four mixes in this order.
    _MIX_THEMES = (
        ("Heavy rotation", "Your most-played, freshly sequenced"),
        ("Rediscover", "Favorites you haven't heard in a while"),
        ("Deep cuts", "Lesser-played gems from artists you love"),
        ("On shuffle", "A broad spread across your library"),
    )

    def _build_mixes_ai(self, provider):
        """Build all four daily mixes in ONE provider call.

        The candidate catalog is serialized exactly once (the whole point — a
        prior version sent it once per theme, 4x per rebuild). The single prompt
        lists all four themes and asks for every mix in one JSON response; each
        mix's ids are then validated and degraded independently, matching the
        per-mix safety of the old per-theme path exactly.
        """
        candidates = catalog.candidates_for_profile(self.db)
        if not candidates:
            return None
        summary = catalog.profile_summary(self.db)
        themes = self._MIX_THEMES

        theme_lines = "\n".join(
            f"{i}. \"{name}\" — {desc}"
            for i, (name, desc) in enumerate(themes, start=1)
        )
        instruction = (
            f"Listener profile:\n{summary}\n\n"
            f"Build {_MIX_COUNT} distinct daily mixes, one for each theme below. "
            f"Give each mix about {_MIX_SIZE} track ids. Keep the four mixes "
            "meaningfully distinct from one another rather than overlapping "
            "heavily. Pick ids ONLY from CANDIDATES — never invent or modify an "
            "id.\n\n"
            f"Themes (return the mixes in this order):\n{theme_lines}\n\n"
            "Return JSON of the form "
            "{\"mixes\": [{\"name\": str, \"description\": str, "
            "\"track_ids\": [...]}, ...]} with one object per theme, in order."
        )
        # Bump max_tokens: four mixes × ~20 ids in one response needs more room
        # than the per-mix default (2000) used by single-mix prompts. ~80 GUID
        # ids + names/descriptions is ~1.5k tokens, so 4000 leaves ~2x headroom.
        # If the model still clips the JSON, complete_json's parse fails and we
        # fall through to the heuristic set below (truncation collapses ALL four
        # mixes to the fallback rather than salvaging the parseable prefix —
        # safe, just less granular than the per-mix degrade on a valid response).
        try:
            data = provider.complete_json(
                _SYSTEM_SELECT, _wrap(instruction, candidates), max_tokens=4000
            )
        except Exception:  # noqa: BLE001 — any provider bug -> heuristic set
            logger.info("AI daily-mix build failed", exc_info=True)
            return None

        raw_mixes = self._extract_mixes(data)

        out = []
        for i, (name_hint, desc_hint) in enumerate(themes):
            raw = raw_mixes[i] if i < len(raw_mixes) else None
            out.append(self._mix_from_raw(raw, name_hint, desc_hint, _MIX_SIZE))

        # If every mix collapsed to no ids, signal failure so the caller uses
        # the richer heuristic set instead (same as the old all-empty check).
        if all(not m["track_ids"] for m in out):
            return None
        return out

    @staticmethod
    def _extract_mixes(data):
        """Pull the ordered list of raw mix objects out of a model response.

        Accepts ``{"mixes": [...]}`` (preferred) or a bare top-level list, and
        tolerates a single-mix dict by wrapping it. Anything else -> empty list,
        which the caller pads to four theme-hint slots.
        """
        if isinstance(data, dict):
            mixes = data.get("mixes")
            if isinstance(mixes, list):
                return mixes
            # A lone mix dict (no wrapper) — treat as one mix.
            if "track_ids" in data or "ids" in data:
                return [data]
            return []
        if isinstance(data, list):
            return data
        return []

    def _mix_from_raw(self, raw, name_hint, desc_hint, size):
        """Validate+degrade one raw mix object exactly like the old per-mix path.

        Returns ``{name, description, track_ids}``. The model's name/description
        win when present, else the theme hint (same precedence as before). If the
        ids are empty or under ``_MIN_VALID_FRACTION`` valid, degrades to the
        theme hint with empty ``track_ids`` (same as the old _build_named_mix).
        """
        ids = self._extract_ids(raw)
        valid, frac = catalog.validate_ids(self.db, ids)
        valid = valid[:size]
        if frac < _MIN_VALID_FRACTION or not valid:
            return {"name": name_hint, "description": desc_hint, "track_ids": []}
        name = name_hint
        description = desc_hint
        if isinstance(raw, dict):
            name = (raw.get("name") or name_hint).strip() or name_hint
            description = (raw.get("description") or desc_hint).strip() or \
                desc_hint
        return {"name": name, "description": description, "track_ids": valid}

    def _build_named_mix(self, provider, candidates, instruction,
                         name_hint, desc_hint, size):
        """Run one name+description+ids mix prompt; validate; degrade safely.

        Still used by :meth:`_build_radios_ai` (one prompt per radio station).
        The daily-mix path no longer uses it — it builds all four mixes in a
        single call via :meth:`_build_mixes_ai`.
        """
        try:
            data = provider.complete_json(_SYSTEM_SELECT, _wrap(instruction,
                                                                 candidates))
        except Exception:  # noqa: BLE001
            logger.info("AI mix build failed", exc_info=True)
            return {"name": name_hint, "description": desc_hint, "track_ids": []}
        return self._mix_from_raw(data, name_hint, desc_hint, size)

    def _mixes_fallback(self):
        """Four deterministic heuristic mixes from local play data."""
        db = self.db
        out = []

        # 1. Heavy rotation — top played.
        top = db.top_tracks(_MIX_SIZE)
        out.append({
            "name": "Heavy rotation",
            "description": "Your most-played tracks",
            "track_ids": [t["id"] for t in top],
        })

        # 2. Rediscover — forgotten favorites (older high-count), else recents.
        forgotten = db.forgotten_favorites(_MIX_SIZE, now=self._now())
        if not forgotten:
            forgotten = db.recent_tracks(_MIX_SIZE)
        out.append({
            "name": "Rediscover",
            "description": "Favorites you haven't heard in a while",
            "track_ids": [t["id"] for t in forgotten],
        })

        # 3. Decade mix — the library's most-represented decade.
        decade_ids = []
        decade_name = "Decade mix"
        decades = db.decades()
        if decades:
            start = decades[0][0]
            rows = db.decade_top_tracks(start, _MIX_SIZE)
            decade_ids = [r["id"] for r in rows]
            decade_name = f"{start}s mix"
        out.append({
            "name": decade_name,
            "description": "A trip through a favorite era",
            "track_ids": decade_ids,
        })

        # 4. Shuffle — random spread.
        rnd = db.read(
            lambda c: [
                r["id"] for r in c.execute(
                    "SELECT id FROM tracks ORDER BY RANDOM() LIMIT ?",
                    (_MIX_SIZE,),
                ).fetchall()
            ]
        )
        out.append({
            "name": "On shuffle",
            "description": "A broad spread across your library",
            "track_ids": rnd,
        })
        return out

    # ------------------------------------------------------------------ #
    # Personal radios                                                    #
    # ------------------------------------------------------------------ #

    def personal_radios(self):
        """Three personal radio stations ``[{name, description, track_ids}]``.

        Cached weekly (``personal_radio`` keys '1'..'3'); rebuilt when the
        cached rows are older than 7 days. Heuristic fallback (top artists /
        genres) when no provider or on AI failure.
        """
        if self._radios_fresh():
            cached = self._load_cached_set("personal_radio", _RADIO_COUNT)
            if cached is not None:
                return cached

        provider = self._provider()
        radios = None
        if provider is not None:
            radios = self._build_radios_ai(provider)
        if not radios:
            radios = self._radios_fallback()

        built_at = self._now().isoformat()
        for i, r in enumerate(radios, start=1):
            self._cache_set("personal_radio", str(i), {**r, "built_at": built_at})
        return radios

    def personal_radios_cached(self):
        """Cheap, network-free personal radios for the first page paint.

        Returns the cached ``personal_radio`` set (keys '1'..'3') if present —
        *even when stale* (older than the weekly window) — otherwise the
        heuristic set. NEVER constructs a provider or makes a network call. Pair
        with :meth:`needs_radios_rebuild` to decide whether to kick a background
        :meth:`personal_radios` refresh after the page renders.
        """
        cached = self._load_cached_set("personal_radio", _RADIO_COUNT)
        if cached is not None:
            return cached
        return self._radios_fallback()

    def needs_radios_rebuild(self):
        """True when the personal radios are missing or older than the window."""
        return not self._radios_fresh()

    def _radios_fresh(self):
        # Freshness is keyed off a build timestamp stored *in* the payload
        # (using the injected clock) rather than the db's wall-clock created_at,
        # so the weekly window is deterministic under a fake clock.
        payload = self.db.ai_cache_get("personal_radio", "1")
        if not payload:
            return False
        built = _parse_dt(payload.get("built_at"))
        if built is None:
            return False
        age = self._now() - built
        return age.days < _RADIO_MAX_AGE_DAYS

    def _top_artist_seeds(self, k):
        """Top-k (artist_id, artist_name) by aggregate play_count."""
        return self.db.read(
            lambda c: [
                (r["artist_id"], r["artist_name"])
                for r in c.execute(
                    "SELECT artist_id, artist_name, SUM(play_count) AS p "
                    "FROM tracks WHERE artist_id IS NOT NULL "
                    "GROUP BY artist_id ORDER BY p DESC, "
                    "artist_name COLLATE NOCASE LIMIT ?",
                    (k,),
                ).fetchall()
            ]
        )

    def _build_radios_ai(self, provider):
        seeds = self._top_artist_seeds(_RADIO_COUNT)
        if not seeds:
            return None
        candidates = catalog.candidates_for_profile(self.db)
        out = []
        for artist_id, artist_name in seeds:
            name_hint = f"{artist_name or 'Artist'} Radio"
            desc_hint = f"Inspired by {artist_name or 'your top artists'}"
            instruction = (
                f"Build a {_RADIO_SIZE}-track radio station inspired by the "
                f"artist '{artist_name}'. Mix their tracks with similar artists "
                f"from CANDIDATES. Return JSON "
                f"{{\"name\": str, \"description\": str, \"track_ids\": [...]}}."
            )
            out.append(self._build_named_mix(
                provider, candidates, instruction, name_hint, desc_hint,
                _RADIO_SIZE,
            ))
        if all(not r["track_ids"] for r in out):
            return None
        return out

    def _radios_fallback(self):
        seeds = self._top_artist_seeds(_RADIO_COUNT)
        out = []
        for artist_id, artist_name in seeds:
            rows = self.db.artist_top_tracks(artist_id, _RADIO_SIZE)
            out.append({
                "name": f"{artist_name or 'Artist'} Radio",
                "description": f"Inspired by {artist_name or 'your top artists'}",
                "track_ids": [r["id"] for r in rows],
            })
        # Pad to 3 with genre/decade-based stations if we have fewer artists.
        while len(out) < _RADIO_COUNT:
            rnd = self.db.read(
                lambda c: [
                    r["id"] for r in c.execute(
                        "SELECT id FROM tracks ORDER BY RANDOM() LIMIT ?",
                        (_RADIO_SIZE,),
                    ).fetchall()
                ]
            )
            out.append({
                "name": "Discovery Radio",
                "description": "A spread across your library",
                "track_ids": rnd,
            })
        return out

    # ------------------------------------------------------------------ #
    # Artist popularity + bio                                            #
    # ------------------------------------------------------------------ #

    def _artist_fingerprint(self, artist_id):
        """A deterministic fingerprint of an artist's track set.

        ``"<count>:<max date_created>"`` from one cheap SQL query. It changes iff
        a sync adds (or removes) a track by this artist or moves their newest
        ``date_created`` forward — i.e. exactly when their popularity ranking has
        new material to consider. Used to invalidate the cached ``artist_top``
        ranking without an expiry window (see :meth:`artist_top_cached`).
        """
        row = self.db.read(
            lambda c: c.execute(
                "SELECT COUNT(*) AS n, MAX(date_created) AS mx "
                "FROM tracks WHERE artist_id=?",
                (artist_id,),
            ).fetchone()
        )
        count = row["n"] if row else 0
        mx = (row["mx"] if row else None) or ""
        return f"{count}:{mx}"

    def artist_top_tracks_ai(self, artist_id, n=10):
        """AI-ranked popularity order of an artist's OWN tracks.

        Catalog = all of the artist's tracks; the model is asked for popularity
        order. Cached (kind ``artist_top``, key=artist_id, no time expiry) with a
        track-set fingerprint so the cache self-invalidates when a sync adds new
        material — busted manually by :meth:`refresh_artist`. Fallback: local
        play_count order. Returns ``{"source": "ai"|"play_count", "tracks": []}``.
        """
        cached = self.db.ai_cache_get("artist_top", artist_id)
        if cached is not None and \
                cached.get("fingerprint") == self._artist_fingerprint(artist_id):
            ids = cached.get("track_ids", [])
            tracks = self.db.tracks_by_ids(ids)
            if tracks:
                return {"source": cached.get("source", "ai"), "tracks": tracks}

        own = self.db.read(
            lambda c: [
                dict(r) for r in c.execute(
                    "SELECT * FROM tracks WHERE artist_id=?", (artist_id,)
                ).fetchall()
            ]
        )
        provider = self._provider()
        if provider is not None and own:
            instruction = (
                "Rank these tracks by overall popularity / recognisability, "
                "most popular first. Use ONLY ids from CANDIDATES. Return JSON "
                "{\"track_ids\": [...]}."
            )
            ids, ok = self._select_ids(provider, own, instruction)
            if ok and ids:
                self._cache_set("artist_top", artist_id, {
                    "track_ids": ids, "source": "ai",
                    "fingerprint": self._artist_fingerprint(artist_id),
                })
                return {"source": "ai", "tracks": self.db.tracks_by_ids(ids)}

        # Fallback: db play_count order.
        ranked = self.db.artist_top_tracks(artist_id, n)
        return {"source": "play_count", "tracks": ranked}

    def artist_top_cached(self, artist_id):
        """Cached AI popularity ranking for ``artist_id`` — db-only, no network.

        Returns ``{"source", "tracks"}`` when an ``artist_top`` cache row exists,
        is still current for the artist's track set, and resolves to tracks;
        ``None`` otherwise. NEVER constructs a provider or fetches synchronously.

        Cache validity is gated by a deterministic fingerprint (track count +
        newest ``date_created``) stored in the payload at ranking time. When a
        sync adds new material by this artist the fingerprint diverges and this
        returns ``None`` (a miss) so the page's existing cold-cache background
        path re-ranks WITH the new tracks — the bio is unaffected (bios don't go
        stale with new releases). A legacy row written before fingerprints
        existed has none stored, so it reads as a miss exactly once; the ensuing
        re-rank re-stores the payload WITH a fingerprint and the cache self-heals.
        """
        cached = self.db.ai_cache_get("artist_top", artist_id)
        if cached is None:
            return None
        # Fingerprint gate: a missing (legacy) or diverged fingerprint -> miss.
        if cached.get("fingerprint") != self._artist_fingerprint(artist_id):
            return None
        tracks = self.db.tracks_by_ids(cached.get("track_ids", []))
        if not tracks:
            return None
        return {"source": cached.get("source", "ai"), "tracks": tracks}

    def artist_bio_cached(self, artist_id):
        """Stored artist bio (``artists.bio``) or "" — db-only, no network."""
        row = self.db.read(
            lambda c: c.execute(
                "SELECT bio FROM artists WHERE id=?", (artist_id,)
            ).fetchone()
        )
        if row is None:
            return ""
        return row["bio"] or ""

    def artist_bio(self, artist_id):
        """Return a 200–300 word artist bio (stored in ``artists.bio``).

        Returns the stored db bio if present (no AI call). Otherwise prompts the
        provider with the artist's name + their album titles for grounding,
        stores the result, and returns it. Returns "" when no provider or on
        failure — the UI just hides the bio section.
        """
        row = self.db.read(
            lambda c: c.execute(
                "SELECT name, bio FROM artists WHERE id=?", (artist_id,)
            ).fetchone()
        )
        if row is None:
            return ""
        if row["bio"]:
            return row["bio"]

        provider = self._provider()
        if provider is None:
            return ""

        name = row["name"] or "this artist"
        albums = self.db.read(
            lambda c: [
                r["name"] for r in c.execute(
                    "SELECT name FROM albums WHERE album_artist_id=? "
                    "ORDER BY year LIMIT 20",
                    (artist_id,),
                ).fetchall()
            ]
        )
        album_line = ", ".join(albums) if albums else "(unknown)"
        system = (
            "You write concise, factual music-artist biographies. State only "
            "well-known facts; do not fabricate. When you are not confident "
            "about a fact, project, or release, OMIT it entirely — do not hedge, "
            "do not write meta-commentary about uncertainty or verification, and "
            "do not mention what you do not know. Return strictly valid JSON."
        )
        user = (
            f"Write a 200-300 word biography of the music artist '{name}', "
            "covering their background, style, and notable releases. "
            f"Albums in the user's library: {album_line}. "
            "Include only facts you are confident about; silently omit anything "
            "uncertain rather than noting that it could not be verified. "
            f"Return JSON {{\"bio\": str}}."
        )
        try:
            data = provider.complete_json(system, user, max_tokens=600)
        except Exception:  # noqa: BLE001
            logger.info("artist_bio AI call failed", exc_info=True)
            return ""
        bio = ""
        if isinstance(data, dict):
            bio = (data.get("bio") or "").strip()
        elif isinstance(data, str):
            bio = data.strip()
        if not bio:
            return ""
        self.db.set_artist_bio(artist_id, bio)
        self._maybe_push_bio(artist_id, bio)
        return bio

    def _maybe_push_bio(self, artist_id, bio):
        """Push a freshly generated AI bio up to Jellyfin's Overview, if enabled.

        Gated by the live ``push-bios`` preference. Runs on the same worker
        thread as the bio generation (this is already off the main thread when
        called from the page/Update fetch). A ``JellyfinError`` — including a
        403 for non-admin users who lack the metadata-edit permission — is
        caught and logged; the local bio is unaffected. No toast is raised.
        """
        try:
            if not self._push_bios():
                return
        except Exception:  # noqa: BLE001 — a bad flag getter must not break bio
            logger.debug("push-bios flag getter failed", exc_info=True)
            return
        if self.client is None:
            return
        try:
            self.client.update_overview(artist_id, bio)
        except Exception:  # noqa: BLE001 — push is best-effort (e.g. 403)
            logger.warning("failed to push AI bio to Jellyfin", exc_info=True)

    def refresh_artist(self, artist_id):
        """Bust the artist's AI caches and re-fetch popularity + bio."""
        # Drop cached AI popularity + stored bio so the next reads rebuild.
        self.db.write(
            lambda c: c.execute(
                "DELETE FROM ai_cache WHERE kind='artist_top' AND key=?",
                (artist_id,),
            )
        )
        self.db.write(
            lambda c: c.execute(
                "UPDATE artists SET bio=NULL, bio_updated_at=NULL, "
                "bio_source=NULL WHERE id=?",
                (artist_id,),
            )
        )
        self.artist_top_tracks_ai(artist_id)
        self.artist_bio(artist_id)

    # ------------------------------------------------------------------ #
    # Cache plumbing                                                     #
    # ------------------------------------------------------------------ #

    def _cache_set(self, kind, key, payload):
        try:
            self.db.ai_cache_set(kind, key, payload)
        except Exception:  # noqa: BLE001 — cache writes are best-effort
            logger.debug("ai_cache_set failed", exc_info=True)

    def _load_cached_set(self, kind, count):
        """Load keys '1'..'count' for ``kind``; None if any are missing."""
        out = []
        for i in range(1, count + 1):
            payload = self.db.ai_cache_get(kind, str(i))
            if payload is None:
                return None
            out.append(payload)
        return out


def _wrap(instruction, candidates):
    """Compose the user prompt: instruction + the formatted candidate catalog."""
    return f"{instruction}\n\nCANDIDATES:\n{catalog.format_catalog(candidates)}"


def _import_track_model():
    """Import the ``Track`` model, tolerating both package layouts.

    In the app, this module lives at ``…lib.ai.discovery`` so the sibling is
    ``..jellyfin.models``. In the headless test harness, conftest puts
    ``src/lib`` on ``sys.path`` so ``jellyfin`` is importable top-level. Try the
    relative import first, then the flat one.
    """
    try:
        from ..jellyfin.models import Track  # type: ignore
    except ImportError:
        from jellyfin.models import Track  # type: ignore
    return Track
