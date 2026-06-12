# home_page.py
#
# Copyright (C) 2026 Tyler Reece
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""The Home page — the post-login landing surface.

Top-to-bottom (design spec):
  1. Recents grid (2x3 wide cards)         — recents_mixed(6)
  2. Your favorite artists (carousel)      — favorite_artists(16)
  3. Recently played (carousel)            — recent_albums(16)
  4. Albums you'll enjoy (carousel)        — albums_by_playcount(16, exclude=#3)
  5. Custom mixes / Personal radio         — AI placeholders (provider==none)
  7. New albums (carousel)                 — new_albums(16)
  8. Top tracks (6 track rows)             — top_tracks(6)
  9. Your forgotten favorites (track rows) — forgotten_favorites(6)
 10. Your listening history (month cards)  — months()

PERFORMANCE: every section's SQLite query runs in the single ``_load_async``
worker pass (no per-section run_async storm); ``_load_finish`` only builds
widgets. Sections with no data hide entirely. All data comes from SQLite; images
load via the cache (pool=True) inside the card widgets, never the network here.
"""

import logging
from gettext import gettext as _

from gi.repository import Gtk

from ..lib import home_rows, utils
from ..widgets import HTCollageCardWidget, HTWideCardWidget
from ..widgets.carousel_widget import HTCarouselWidget
from .page import Page

logger = logging.getLogger(__name__)

# How many items each section previews before "More".
_PREVIEW = 16
# Recents grid: 2 rows x 3 cols of wide cards.
_RECENTS = 6
_RECENTS_COLS = 3
# Track-row sections show six.
_TRACK_ROWS = 6


class HTHomePage(Page):
    """The user's Home landing page (all sections read from SQLite)."""

    __gtype_name__ = "HTHomePage"

    def disconnect_all(self, *args):
        """Sweep the per-section AI carousel signal scopes, then the base set.

        The mixes/radios carousels park their card "clicked" handlers in
        ``_mix_signals`` / ``_radio_signals`` (not ``self.signals``) so a
        background refresh can rebuild just one section. Without this sweep those
        handlers — and the closures they hold (track-id lists / seed ids) —
        would survive pop and pin the page.
        """
        for scope_name in ("_mix_signals", "_radio_signals"):
            scope = getattr(self, scope_name, None)
            if not scope:
                continue
            for obj, signal_id in scope:
                if obj.handler_is_connected(signal_id):
                    obj.disconnect(signal_id)
            scope.clear()
        super().disconnect_all(*args)

    def _load_async(self) -> None:
        # Single worker pass: ALL section queries here, no per-section async.
        # CRITICAL: this pass must be network-free. Every AI section is served
        # from the cheap cached/heuristic accessors (pure SQLite) so the whole
        # page paints instantly; the real (network) AI rebuild is kicked AFTER
        # render by _maybe_refresh_ai — see _load_finish.
        db = utils.db
        self.recents = db.recents_mixed(_RECENTS)
        self.fav_artists = db.favorite_artists(_PREVIEW)
        self.recent_albums = db.recent_albums(_PREVIEW)
        recent_ids = [a["id"] for a in self.recent_albums]
        self.enjoy_albums = db.albums_by_playcount(_PREVIEW, exclude_ids=recent_ids)
        self.new_albums = db.new_albums(_PREVIEW)
        self.top = db.top_tracks(_TRACK_ROWS)
        self.forgotten = db.forgotten_favorites(_TRACK_ROWS)
        self.months = db.months()
        # Month-card collage art: the most-played albums for each month, fetched
        # here in the single worker pass (the months list is small) so
        # _load_finish only builds widgets. Keyed by "YYYY-MM".
        self.month_albums = {
            yyyy_mm: db.month_top_albums(yyyy_mm, 4) for yyyy_mm, _plays in self.months
        }
        self.ai_provider = "none"
        if utils.settings is not None:
            self.ai_provider = utils.settings.get_string("ai-provider")

        # AI discovery sections (rows 5/6), STALE-WHILE-REVALIDATE. The worker
        # pass uses ONLY the cache-only/heuristic accessors (no provider, no
        # network) so first paint is instant even on the day's first launch.
        # _maybe_refresh_ai() then rebuilds these two carousels in the
        # background when a refresh is due. artist-radio cards are seeded from
        # the top artists' top track (pure db).
        self.ai_mixes = []
        self.ai_radios = []
        self.ai_artist_radios = []
        # Per-section signal scopes for the swappable AI carousels (their card
        # "clicked" handlers). Kept off ``self.signals`` so a background rebuild
        # can disconnect+clear just that section without disturbing the rest of
        # the page; swept at pop via disconnect_all (see below).
        self._mix_signals = []
        self._radio_signals = []
        # The live Custom-mixes carousel (set in _build_mix_carousel) — held so
        # the forced-refresh handler can toggle its spinner state.
        self._mix_carousel = None
        if self.ai_provider != "none" and utils.discovery is not None:
            try:
                self.ai_mixes = utils.discovery.daily_mixes_cached()
            except Exception:  # noqa: BLE001 — UI must never crash on AI
                logger.exception("daily_mixes_cached failed; hiding section")
            try:
                self.ai_radios = utils.discovery.personal_radios_cached()
            except Exception:  # noqa: BLE001
                logger.exception("personal_radios_cached failed; hiding section")
            self.ai_artist_radios = self._top_artist_radio_seeds(db)
        # Collage album art for the AI cards, resolved here in the single
        # (network-free) worker pass so _load_finish only builds widgets.
        self._compute_ai_collage_albums(db)

    def _compute_ai_collage_albums(self, db):
        """Resolve the collage album art for every AI card (off the main thread).

        Runs in the network-free ``_load_async`` worker pass so ``_load_finish``
        only builds widgets:
          * each mix / radio -> ``db.albums_for_tracks(track_ids)`` (the albums
            its tracks belong to, ranked by membership)
          * each artist-radio seed -> that artist's own albums (``artist_albums``
            capped to 4)
        Cards whose items have no album art keep the text-only fallback (the
        collage card hides its grid).
        """
        self.ai_mix_albums = [
            db.albums_for_tracks(m.get("track_ids") or []) for m in self.ai_mixes
        ]
        self.ai_radio_albums = [
            db.albums_for_tracks(r.get("track_ids") or []) for r in self.ai_radios
        ]
        for seed in self.ai_artist_radios:
            seed["albums"] = db.artist_albums(seed["artist_id"])[:4]

    def _top_artist_radio_seeds(self, db):
        """Top-3 artists by playcount + each one's top track (the radio seed)."""
        rows = db.read(lambda c: c.execute(
            "SELECT artist_id, artist_name, SUM(play_count) AS p FROM tracks "
            "WHERE artist_id IS NOT NULL GROUP BY artist_id "
            "ORDER BY p DESC, artist_name COLLATE NOCASE LIMIT 3"
        ).fetchall())
        seeds = []
        for r in rows:
            top = db.artist_top_tracks(r["artist_id"], 1)
            if top:
                seeds.append({
                    "artist_id": r["artist_id"],
                    "artist_name": r["artist_name"] or _("Artist"),
                    "seed_track_id": top[0]["id"],
                })
        return seeds

    def _load_finish(self) -> None:
        self.set_tag("home")
        self.set_title(_("Home"))

        db = utils.db

        # 1. Recents grid ----------------------------------------------- #
        self._build_recents_grid(self.recents)

        # 2. Your favorite artists -------------------------------------- #
        self.new_carousel_for(
            _("Your favorite artists"), self.fav_artists,
            more_function=lambda offset, limit: db.favorite_artists()[
                offset:offset + limit
            ],
        )

        # 3. Recently played -------------------------------------------- #
        self.new_carousel_for(
            _("Recently played"), self.recent_albums,
            more_function=lambda offset, limit: db.recent_albums(offset + limit)[
                offset:
            ],
        )

        # 4. Albums you'll enjoy ---------------------------------------- #
        recent_ids = [a["id"] for a in self.recent_albums]
        self.new_carousel_for(
            _("Albums you'll enjoy"), self.enjoy_albums,
            more_function=lambda offset, limit: db.albums_by_playcount(
                offset + limit, exclude_ids=recent_ids
            )[offset:],
        )

        # 5/6. AI sections — live when a provider is configured, else the
        # invitation placeholder (provider == none). The two AI carousels live
        # in dedicated section boxes so the post-render background refresh can
        # swap their contents in place without rebuilding the whole page. ---- #
        self._mixes_box = None
        self._radios_box = None
        if home_rows.ai_placeholder_visible(self.ai_provider):
            self._build_ai_placeholder(
                _("Custom mixes"),
                _("Set up an AI provider in Preferences to enable"),
            )
            self._build_ai_placeholder(
                _("Personal radio stations"),
                _("Set up an AI provider in Preferences to enable"),
            )
        else:
            self._mixes_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            self.content.append(self._mixes_box)
            self._build_mix_carousel(
                self._mixes_box, _("Custom mixes"), self.ai_mixes,
            )
            self._radios_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            self.content.append(self._radios_box)
            self._build_radio_carousel(
                self._radios_box, _("Personal radio stations"),
                self.ai_radios, self.ai_artist_radios,
            )

        # 7. New albums ------------------------------------------------- #
        self.new_carousel_for(
            _("New albums"), self.new_albums,
            more_function=lambda offset, limit: db.albums_page(
                offset, limit, sort="date_created"
            ),
        )

        # 8. Top tracks (six rows) -------------------------------------- #
        self.new_track_list_for(
            _("Top tracks"), self.top,
            more_function=lambda offset, limit: db.top_tracks(offset + limit)[
                offset:
            ],
        )

        # 9. Your forgotten favorites ----------------------------------- #
        self.new_track_list_for(
            _("Your forgotten favorites"), self.forgotten,
            more_function=lambda offset, limit: db.forgotten_favorites(
                offset + limit
            )[offset:],
        )

        # 10. Your listening history (month cards) ---------------------- #
        self._build_history(self.months)

        # Stale-while-revalidate: now that the (cached/heuristic) page is on
        # screen, kick the real AI rebuild in the background if it's due.
        self._maybe_refresh_ai()

    # ------------------------------------------------------------------ #
    # AI background refresh (stale-while-revalidate)                      #
    # ------------------------------------------------------------------ #

    def _maybe_refresh_ai(self):
        """Kick a background rebuild of the mixes/radios carousels when due.

        Runs ONLY when a provider is configured and the cached payload is stale
        (daily for mixes, weekly for radios). The real (network) builders run on
        a worker thread; on completion the two carousels are swapped in place.
        Owner-guarded via ``run_async`` so a late callback after pop is dropped.
        On failure the stale/heuristic content is kept silently (logged).
        """
        if self.ai_provider == "none" or utils.discovery is None:
            return
        disc = utils.discovery
        try:
            need_mixes = disc.needs_mixes_rebuild()
            need_radios = disc.needs_radios_rebuild()
        except Exception:  # noqa: BLE001
            logger.exception("AI rebuild predicate failed; keeping cached")
            return
        if not (need_mixes or need_radios):
            return

        # Subtle "(updating…)" affordance on the titles being refreshed.
        if need_mixes and self._mixes_box is not None:
            self._set_section_updating(self._mixes_box, True)
        if need_radios and self._radios_box is not None:
            self._set_section_updating(self._radios_box, True)

        def work():
            mixes = disc.daily_mixes() if need_mixes else None
            radios = disc.personal_radios() if need_radios else None
            return mixes, radios

        def done(result):
            mixes, radios = result
            if mixes is not None and self._mixes_box is not None:
                self.ai_mixes = mixes
                self._rebuild_mixes_section()
            if radios is not None and self._radios_box is not None:
                self.ai_radios = radios
                self._rebuild_radios_section()

        def on_error(_exc):
            # Keep the stale/heuristic carousels; just clear the updating hint.
            logger.info("AI background refresh failed; keeping cached content")
            if self._mixes_box is not None:
                self._set_section_updating(self._mixes_box, False)
            if self._radios_box is not None:
                self._set_section_updating(self._radios_box, False)

        utils.run_async(work, on_done=done, on_error=on_error, owner=self)

    def _set_section_updating(self, box, updating):
        """Append/strip a dim '(updating…)' suffix on a section box's title."""
        title_label = self._section_title_label(box)
        if title_label is None:
            return
        base = getattr(title_label, "_ht_base_title", None)
        if base is None:
            base = title_label.get_label()
            title_label._ht_base_title = base
        title_label.set_label(
            _("{title}  (updating…)").format(title=base) if updating else base
        )

    @staticmethod
    def _section_title_label(box):
        """The title Gtk.Label of an AI section (the carousel's title)."""
        outer = box.get_first_child()  # the HTCarouselWidget
        if outer is None:
            return None
        label = getattr(outer, "title_label", None)
        if label is not None:
            return label
        # Fallback: walk for the first Label (older scaffold shape).
        child = outer.get_first_child()
        while child is not None:
            if isinstance(child, Gtk.Label):
                return child
            child = child.get_next_sibling()
        return None

    def _on_mixes_refresh(self, _carousel):
        """Force-rebuild the four Custom mixes now (refresh button clicked).

        Bypasses the once-per-day ``mixes_built_date`` stamp via
        ``daily_mixes(force=True)`` — AI when a provider is configured, else the
        heuristic fallback (so the button works with no provider). The rebuild
        runs on a worker thread (NEVER db/network on the main thread); on success
        the mixes section is swapped in place (which re-creates the carousel with
        a fresh, non-spinning button). Both callbacks clear the spinner state;
        on error the previous cached mixes are kept untouched.
        """
        if utils.discovery is None or self._mix_carousel is None:
            return
        self._mix_carousel.set_refreshing(True)
        disc = utils.discovery

        def work():
            return disc.daily_mixes(force=True)

        def done(mixes):
            # The teardown+rebuild replaces the carousel (and its button), so the
            # new button starts in the cleared state; nothing else to reset.
            # All-empty mixes (library emptied since page build) would tear down
            # the carousel and then early-return in _build_mix_carousel, leaving
            # a blank section with no refresh affordance — keep the old one.
            usable = mixes is not None and any(
                m.get("track_ids") for m in mixes
            )
            if usable and self._mixes_box is not None:
                self.ai_mixes = mixes
                self._rebuild_mixes_section()
            elif self._mix_carousel is not None:
                self._mix_carousel.set_refreshing(False)

        def on_error(_exc):
            logger.info("Custom mixes force-refresh failed; keeping cached")
            if self._mix_carousel is not None:
                self._mix_carousel.set_refreshing(False)

        utils.run_async(work, on_done=done, on_error=on_error, owner=self)

    def _rebuild_mixes_section(self):
        # Refresh landed new mixes -> re-resolve their collage album art.
        self.ai_mix_albums = [
            utils.db.albums_for_tracks(m.get("track_ids") or [])
            for m in self.ai_mixes
        ]
        self._teardown_section(self._mixes_box, self._mix_signals)
        self._build_mix_carousel(
            self._mixes_box, _("Custom mixes"), self.ai_mixes,
        )

    def _rebuild_radios_section(self):
        self.ai_radio_albums = [
            utils.db.albums_for_tracks(r.get("track_ids") or [])
            for r in self.ai_radios
        ]
        self._teardown_section(self._radios_box, self._radio_signals)
        self._build_radio_carousel(
            self._radios_box, _("Personal radio stations"),
            self.ai_radios, self.ai_artist_radios,
        )

    def _teardown_section(self, box, scope):
        """Disconnect the section's tracked card signals and empty its box.

        The AI cards are ``HTCollageCardWidget`` (IDisconnectable). This walks
        the box for collage cards and calls ``disconnect_all`` on each (releasing
        their gesture + image-pool guards), sweeps the per-section ``scope``
        signals (the cards' ``activated`` handlers), then removes the box
        children. It must NOT touch ``self.disconnectables`` (which holds the
        rest of the page's cards/rows).
        """
        # The section's first child is now an HTCarouselWidget; its
        # disconnect_all recursively tears down the collage cards it tracks.
        # The card walk below stays as a belt-and-suspenders for any card not
        # parented under a carousel (older shapes / future variants).
        child = box.get_first_child()
        while child is not None:
            if hasattr(child, "disconnect_all"):
                child.disconnect_all()
            child = child.get_next_sibling()
        for card in self._collage_cards_in(box):
            card.disconnect_all()
        for obj, signal_id in scope:
            if obj.handler_is_connected(signal_id):
                obj.disconnect(signal_id)
        scope.clear()
        child = box.get_first_child()
        while child is not None:
            box.remove(child)
            child = box.get_first_child()

    @staticmethod
    def _collage_cards_in(widget):
        """Depth-first collect every HTCollageCardWidget under ``widget``."""
        found = []
        stack = [widget]
        while stack:
            w = stack.pop()
            if isinstance(w, HTCollageCardWidget):
                found.append(w)
            child = w.get_first_child() if hasattr(w, "get_first_child") else None
            while child is not None:
                stack.append(child)
                child = child.get_next_sibling()
        return found

    # ------------------------------------------------------------------ #
    # Section builders                                                   #
    # ------------------------------------------------------------------ #

    def _build_recents_grid(self, items):
        """2x3 grid of wide cards — explicit rows of ``_RECENTS_COLS`` each.

        Chunking is done by the tested ``home_rows.recents_grid_rows`` helper so
        the row layout is deterministic (3 per row, partial last row), rather
        than relying on FlowBox's responsive reflow.
        """
        if not items:
            return
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin_start=12,
                      margin_end=12, margin_top=12, spacing=6)
        for chunk in home_rows.recents_grid_rows(items, _RECENTS_COLS):
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                          homogeneous=True, spacing=6)
            for item in chunk:
                card = HTWideCardWidget(item)
                self.disconnectables.append(card)
                row.append(card)
            box.append(row)
        self.content.append(box)

    def _build_ai_placeholder(self, title, subtitle):
        """A dimmed section header inviting AI setup (shown when provider=none)."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin_start=12,
                      margin_end=12, margin_top=12, spacing=2,
                      css_classes=["dim-label"])
        box.append(Gtk.Label(label=title, xalign=0, css_classes=["title-3"]))
        box.append(Gtk.Label(label=subtitle, xalign=0, css_classes=["caption"]))
        self.content.append(box)

    # ------------------------------------------------------------------ #
    # AI section builders (rows 5/6)                                     #
    # ------------------------------------------------------------------ #

    def _ai_carousel(self, title, cards):
        """Standard HTCarouselWidget hosting pre-built AI collage cards.

        Replaces the old hand-rolled label+scroller scaffold so the AI rows
        get the SAME header chrome (bold title, prev/next arrows) and margins
        as every other home section. The carousel registers the cards in its
        own disconnectables; section teardown calls its ``disconnect_all``.
        """
        carousel = HTCarouselWidget(title)
        carousel.set_card_widgets(cards)
        return carousel

    def _collage_card(self, title, subtitle, albums):
        """A clickable mix/radio collage card (2x2 art + bold name + caption).

        Same hard 1:1 160px collage as the listening-history month cards. The
        card's argless ``activated`` signal is wired by the caller into the
        relevant per-section scope (no page-pinning bound method held by the
        live tree); the card is itself IDisconnectable and torn down on section
        teardown.
        """
        return HTCollageCardWidget(
            title=title, subtitle=subtitle, albums=albums or []
        )

    def _build_mix_carousel(self, parent, title, mixes):
        """Horizontal carousel of daily-mix collage cards; click -> track list.

        Appends into ``parent`` (the dedicated mixes section box) so a
        background refresh can swap it in place. Card "activated" handlers are
        tracked in the per-section ``_mix_signals`` scope. Collage art is the
        precomputed ``self.ai_mix_albums`` (aligned 1:1 with ``self.ai_mixes``).
        """
        mixes = mixes or []
        albums = getattr(self, "ai_mix_albums", [])
        pairs = [
            (m, albums[i] if i < len(albums) else [])
            for i, m in enumerate(mixes)
            if m.get("track_ids")
        ]
        if not pairs:
            return
        cards = []
        for mix, mix_albums in pairs:
            name = mix.get("name") or _("Mix")
            description = mix.get("description") or ""
            card = self._collage_card(name, description, mix_albums)
            ids = list(mix.get("track_ids") or [])
            self._mix_signals.append((
                card,
                card.connect(
                    "activated", self._open_track_ids, name, description, ids
                ),
            ))
            cards.append(card)
        carousel = self._ai_carousel(title, cards)
        # Custom mixes is the only section with a force-rebuild affordance.
        # The refresh button emits a signal (no bound page method stored on the
        # widget); the handler goes in the per-section ``_mix_signals`` scope so
        # an in-place rebuild and page teardown both disconnect it.
        carousel.enable_refresh(True)
        self._mix_carousel = carousel
        self._mix_signals.append((
            carousel,
            carousel.connect("refresh-clicked", self._on_mixes_refresh),
        ))
        parent.append(carousel)

    def _build_radio_carousel(self, parent, title, radios, artist_radios):
        """Personal-radio collage cards + per-top-artist "X Radio" cards.

        Radio-station cards push a resolved track list (the cached mix); their
        collage uses ``self.ai_radio_albums`` (aligned with ``self.ai_radios``).
        Artist-radio cards seed ``discovery.track_radio`` from that artist's top
        track and play the result; their collage uses the seed's own
        ``albums``. Appends into ``parent`` (the dedicated radios section box);
        card handlers go in the per-section ``_radio_signals`` scope.
        """
        radios = radios or []
        albums = getattr(self, "ai_radio_albums", [])
        radio_pairs = [
            (r, albums[i] if i < len(albums) else [])
            for i, r in enumerate(radios)
            if r.get("track_ids")
        ]
        artist_radios = artist_radios or []
        if not radio_pairs and not artist_radios:
            return
        cards = []
        for radio, radio_albums in radio_pairs:
            name = radio.get("name") or _("Radio")
            description = radio.get("description") or ""
            card = self._collage_card(name, description, radio_albums)
            ids = list(radio.get("track_ids") or [])
            self._radio_signals.append((
                card,
                card.connect(
                    "activated", self._open_track_ids, name, description, ids
                ),
            ))
            cards.append(card)
        for seed in artist_radios:
            label = _("{artist} Radio").format(artist=seed["artist_name"])
            card = self._collage_card(
                label, _("AI track radio"), seed.get("albums") or [])
            self._radio_signals.append((
                card,
                card.connect(
                    "activated", self._play_artist_radio,
                    seed["seed_track_id"], label,
                ),
            ))
            cards.append(card)
        parent.append(self._ai_carousel(title, cards))

    def _open_track_ids(self, _btn, title, description, ids):
        """Open an AI mix / radio station as an album-style detail page.

        Both the Custom-mix cards and the Personal-radio-station cards route
        here (the artist-radio cards play immediately via ``_play_artist_radio``
        instead). The mix page resolves the ids -> ordered tracks + the collage
        album art itself (db-only) in its ``_load_async``.
        """
        from .mix_page import HTMixPage

        page = HTMixPage(title, description, ids)
        page.load()
        utils.navigation_view.push(page)

    def _play_artist_radio(self, _btn, seed_track_id, label):
        """Seed an AI track radio from an artist's top track and play it."""
        def work():
            return utils.discovery.track_radio(seed_track_id)

        def done(result):
            tracks = result.get("tracks") or []
            if tracks:
                utils.player_object.play_this(tracks, 0)
                utils.send_toast(_("Playing {name}").format(name=label), 2)

        utils.run_async(work, on_done=done, owner=self)

    def _build_history(self, months):
        """Horizontal carousel of clickable month cards."""
        if not months:
            return
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin_start=12,
                      margin_end=12, margin_top=12, margin_bottom=12)
        box.append(Gtk.Label(
            label=_("Your listening history"), xalign=0,
            css_classes=["title-3"], margin_bottom=6,
        ))
        scroller = Gtk.ScrolledWindow(
            vscrollbar_policy=Gtk.PolicyType.NEVER,
            hscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
        )
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        for yyyy_mm, plays in months:
            albums = self.month_albums.get(yyyy_mm, [])
            # A month tile is just a collage card with month-formatted labels;
            # stash the month key + label on the card so _open_month can read
            # them off the (argless) activated signal's emitter.
            label = home_rows.month_label(yyyy_mm)
            card = HTCollageCardWidget(
                title=label,
                subtitle=home_rows.month_plays_label(plays),
                albums=albums,
            )
            card.yyyy_mm = yyyy_mm
            card.label = label
            self.disconnectables.append(card)
            self.signals.append((
                card, card.connect("activated", self._open_month)
            ))
            row.append(card)
        scroller.set_child(row)
        box.append(scroller)
        self.content.append(box)

    def _open_month(self, card):
        """Push a titled track list of the month's top tracks.

        The month card emits the generic argless ``activated``; the month key +
        label are read off the card itself.
        """
        from .from_function_page import HTFromFunctionPage

        page = HTFromFunctionPage(card.label, item_type="track")
        page.set_items(utils.db.month_top_tracks(card.yyyy_mm, 50))
        page.load()
        utils.navigation_view.push(page)
