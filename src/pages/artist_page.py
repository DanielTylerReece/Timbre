# artist_page.py
#
# Copyright 2023 Nokse
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

from gi.repository import Adw, Gtk

from ..lib import utils
from ..widgets.card_widget import item_get
from .page import Page

logger = logging.getLogger(__name__)

# Popular tracks shown inline on the artist page; the "More" pill opens the full
# top-20 list. The AI ranking is fetched/cached at the wider depth so the More
# page can show 20 without a second model call.
_TOP_TRACKS = 5
_POPULAR_MORE = 20


class HTArtistPage(Page):
    """Artist detail: header, top tracks, Albums + Appears-on carousels."""

    __gtype_name__ = "HTArtistPage"

    def _load_async(self) -> None:
        db = utils.db
        row = db.read(
            lambda c: c.execute(
                "SELECT * FROM artists WHERE id=?", (self.id,)
            ).fetchone()
        )
        self.artist = dict(row) if row else {"id": self.id, "name": _("Artist")}
        self.artist["kind"] = "artist"
        self.albums = db.artist_albums(self.id)
        self.appears_on = db.artist_appears_on(self.id)

        # AI "Popular" + bio, STALE-WHILE-REVALIDATE. The worker pass is
        # network-free: it reads ONLY the cache-only accessors (cached AI
        # ranking / stored bio) and otherwise renders the db play_count order.
        # When AI is enabled but NOTHING is cached yet, a background refresh is
        # kicked after render (see _maybe_refresh_ai) to populate it for next
        # time; an existing cache is never auto-refreshed — the Update button is
        # the manual refresh path. An AI failure never blocks the page build.
        self._ai_enabled = (
            utils.settings is not None
            and utils.settings.get_string("ai-provider") != "none"
        )
        # ``top_tracks_full`` is the wider ranked list (up to _POPULAR_MORE) that
        # backs the "More" -> top-20 page; ``top_tracks`` is its first
        # _TOP_TRACKS shown inline. Both come from the same ordering (AI ranking
        # when cached, else db play_count) so the More page is a strict
        # continuation of the inline rows.
        self.top_tracks_full = db.artist_top_tracks(self.id, _POPULAR_MORE)
        self.top_tracks = self.top_tracks_full[:_TOP_TRACKS]
        self.popular_source = "play_count"
        self.bio = self.artist.get("bio") or ""
        self._ai_needs_fetch = False
        if self._ai_enabled and utils.discovery is not None:
            disc = utils.discovery
            try:
                cached = disc.artist_top_cached(self.id)
            except Exception:  # noqa: BLE001
                logger.exception("artist_top_cached failed; using play_count")
                cached = None
            if cached and cached.get("tracks"):
                self.top_tracks_full = cached["tracks"][:_POPULAR_MORE]
                self.top_tracks = self.top_tracks_full[:_TOP_TRACKS]
                self.popular_source = cached.get("source", "ai")
            try:
                cached_bio = disc.artist_bio_cached(self.id)
            except Exception:  # noqa: BLE001
                logger.exception("artist_bio_cached failed")
                cached_bio = ""
            if cached_bio:
                self.bio = cached_bio
            # Nothing AI-cached for this artist yet -> fetch in the background
            # after render (do NOT block first paint, do NOT auto-refresh an
            # existing cache). ``cached_bio`` reads ``artists.bio`` regardless
            # of provenance, so a Jellyfin-sourced bio (bio_source='jellyfin')
            # counts as "have bio" and does NOT trigger an AI request — the
            # server's own bio is respected.
            self._ai_needs_fetch = not (cached and cached.get("tracks")) \
                or not cached_bio

    def _load_finish(self) -> None:
        name = item_get(self.artist, "name") or _("Artist")
        self.set_title(name)

        builder = Gtk.Builder.new_from_resource(
            "/io/github/tylerreece/timbre/ui/pages_ui/artist_page_template.ui"
        )
        self.append(builder.get_object("_main"))

        builder.get_object("_name_label").set_label(name)
        builder.get_object("_first_subtitle_label").set_label(_("Artist"))

        play_btn = builder.get_object("_play_button")
        shuffle_btn = builder.get_object("_shuffle_button")
        self.signals.extend([
            (play_btn, play_btn.connect("clicked", self.on_play_button_clicked)),
            (shuffle_btn,
             shuffle_btn.connect("clicked", self.on_shuffle_button_clicked)),
        ])

        # Favorite (follow) button — write-through to server + db.
        self._fav_button = builder.get_object("_follow_button")
        self._fav_state = bool(item_get(self.artist, "is_favorite"))
        self._update_fav_icon(self._fav_state)
        self.signals.append((
            self._fav_button,
            self._fav_button.connect("clicked", self._on_favorite_clicked),
        ))

        share_btn = builder.get_object("_share_button")
        self.signals.append(
            (share_btn, share_btn.connect("clicked", self._on_share_clicked))
        )

        # Update button: refresh AI bio + popularity. Shown only when an AI
        # provider is configured (else there is nothing to refresh).
        self._update_button = builder.get_object("_update_button")
        self._update_button.set_visible(bool(self._ai_enabled))
        if self._ai_enabled:
            self.signals.append((
                self._update_button,
                self._update_button.connect("clicked", self._on_update_clicked),
            ))

        avatar = builder.get_object("_avatar")
        item_id = item_get(self.artist, "id")
        tag = item_get(self.artist, "image_tag")
        if item_id:
            utils.run_async(
                lambda: utils.add_avatar_from_tag(avatar, item_id, tag, 320),
                owner=self,
            )

        # Popular tracks — AI-ranked when available, else db play_count order.
        # Show 5 inline; the "More" pill opens the top-20 as "{Artist} — Popular"
        # (the same ranked list, sliced). The More page plays in context.
        popular_title = (
            _("Popular") if self.popular_source == "ai" else _("Top Tracks")
        )
        more_fn = None
        if len(self.top_tracks_full) > len(self.top_tracks):
            more_fn = self._popular_more_function
        self.new_track_list_for(
            popular_title, self.top_tracks, more_function=more_fn,
            more_title=_("{} — Popular").format(name),
        )

        self.new_carousel_for(_("Albums"), self.albums)
        self.new_carousel_for(_("Appears On"), self.appears_on)

        # Bio section. Renders artists.bio when present; otherwise, when an AI
        # bio is about to be fetched in the background, a "Generating bio…"
        # placeholder so the user knows content is coming.
        self._build_bio_section(self.bio)

        # Stale-while-revalidate: the page is now on screen from cached/db data.
        # If this artist has no AI cache yet, populate it in the background.
        self._maybe_fetch_ai()

    def _popular_more_function(self, offset, limit):
        """Paged slice of the full ranked Popular list for the "More" page.

        The top-20 (AI-ranked when cached, else db play_count) was already
        resolved in ``_load_async`` as ``top_tracks_full``; this just slices it
        so the from-function track-list page renders the same ordering the inline
        rows continue from. Returns [] past the end (the list is finite — 20
        tracks max — so the auto-load stops after the first page).
        """
        return self.top_tracks_full[offset:offset + limit]

    def _maybe_fetch_ai(self):
        """Background-populate this artist's AI cache when nothing is cached.

        Runs ONLY on a cold cache (``_ai_needs_fetch``) so the first view of an
        artist still renders instantly (db play_count + any stored bio) while
        the AI ranking + bio fetch happens off the main thread. On completion
        the page sections are rebuilt from the now-populated cache. An existing
        cache is never auto-refreshed here — the Update button is that path.
        Owner-guarded; a failure keeps the db/play_count content silently.
        """
        if not self._ai_needs_fetch or utils.discovery is None:
            return
        disc = utils.discovery
        artist_id = self.id

        def work():
            # These run the real (network) builders and persist to the AI cache;
            # the subsequent reload reads them back via the cache-only accessors.
            try:
                # _POPULAR_MORE depth so the "More" page (top-20) has a full
                # fallback ranking too; the AI path caches all the artist's
                # tracks regardless and only the fallback honours this limit.
                disc.artist_top_tracks_ai(artist_id, _POPULAR_MORE)
            except Exception:  # noqa: BLE001
                logger.info("background artist_top_tracks_ai failed")
            try:
                disc.artist_bio(artist_id)
            except Exception:  # noqa: BLE001
                logger.info("background artist_bio failed")
            return True

        def done(_result):
            # Only one cold-cache fetch per page lifetime.
            self._ai_needs_fetch = False
            self._reload_sections()

        def on_error(_exc):
            # Clear the fetch flag and reload so a "Generating bio…" placeholder
            # doesn't spin forever; a failed fetch just rebuilds with no bio
            # section (silent, logged).
            logger.info("background AI fetch failed; keeping cached content")
            self._ai_needs_fetch = False
            self._reload_sections()

        utils.run_async(work, on_done=done, on_error=on_error, owner=self)

    def _build_bio_section(self, bio):
        """Render the artist bio at the bottom, High Tide style.

        Bold "Bio" header + the FULL wrapped text, always visible. (An
        earlier version collapsed >300-char bios into a Gtk.Expander, which
        hid effectively every bio behind a tiny "Read more" row.)

        When there is no bio yet but an AI bio is about to be fetched in the
        background (``_ai_needs_fetch`` while AI is enabled), render the header
        plus a spinner + "Generating bio…" placeholder instead of nothing. The
        background fetch's ``done``/``on_error`` both call ``_reload_sections``,
        which rebuilds with the real bio (success) or no section (failure), so
        the spinner never spins forever. With AI disabled or nothing being
        fetched, an absent bio renders nothing — unchanged behaviour.
        """
        if not bio:
            if getattr(self, "_ai_enabled", False) and \
                    getattr(self, "_ai_needs_fetch", False):
                self._build_bio_placeholder()
            return
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin_start=12,
                      margin_end=12, margin_top=18, margin_bottom=24)
        box.append(Gtk.Label(label=_("Bio"), xalign=0,
                             css_classes=["title-3"], margin_bottom=12))
        box.append(Gtk.Label(label=bio, xalign=0, wrap=True, selectable=True,
                             css_classes=["dim-label"]))
        self.content.append(box)

    def _build_bio_placeholder(self):
        """Bio header + spinner + "Generating bio…" while the AI fetch runs."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin_start=12,
                      margin_end=12, margin_top=18, margin_bottom=24)
        box.append(Gtk.Label(label=_("Bio"), xalign=0,
                             css_classes=["title-3"], margin_bottom=12))
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.append(Adw.Spinner())
        row.append(Gtk.Label(label=_("Generating bio…"), xalign=0,
                             css_classes=["dim-label"]))
        box.append(row)
        self.content.append(box)

    def _on_update_clicked(self, _btn):
        """Bust the artist's AI caches, re-fetch bio + popularity, reload."""
        if utils.discovery is None:
            return
        self._update_button.set_sensitive(False)

        def work():
            utils.discovery.refresh_artist(self.id)
            return True

        def done(_result):
            utils.send_toast(_("Artist refreshed"), 2)
            # Reload the whole page so Popular + bio rebuild from fresh data.
            self._reload_sections()

        def on_error(_exc):
            if hasattr(self, "_update_button"):
                self._update_button.set_sensitive(True)
            utils.send_toast(_("Refresh failed"), 2)

        utils.run_async(work, on_done=done, on_error=on_error, owner=self)

    def _reload_sections(self):
        """Tear down the built content and re-run the page load pipeline."""
        self.disconnect_all()
        self._section_signals = []
        child = self.content.get_first_child()
        while child is not None:
            self.content.remove(child)
            child = self.content.get_first_child()
        self.content_stack.set_visible_child_name("loading")
        self.load()

    # ------------------------------------------------------------------ #
    # Actions                                                            #
    # ------------------------------------------------------------------ #

    def on_play_button_clicked(self, btn):
        utils.player_object.play_this(self.top_tracks, 0)

    def on_shuffle_button_clicked(self, btn):
        utils.player_object.shuffle_this(self.top_tracks)

    def _update_fav_icon(self, state):
        self._fav_button.set_icon_name(
            "heart-filled-symbolic" if state else "heart-outline-thick-symbolic"
        )

    def _on_favorite_clicked(self, btn):
        item_id = item_get(self.artist, "id")
        if item_id is None or utils.client is None:
            return
        new_state = utils.toggle_favorite(
            "artist", item_id, self._fav_state,
            owner=self, on_applied=self._fav_applied,
        )
        self._fav_state = new_state
        self._update_fav_icon(new_state)

    def _fav_applied(self, state):
        self._fav_state = bool(state)
        self._update_fav_icon(self._fav_state)

    def _on_share_clicked(self, *args):
        item_id = item_get(self.artist, "id") or ""
        server_url = ""
        server_id = ""
        if utils.settings is not None:
            server_url = utils.settings.get_string("server-url")
        if utils.client is not None:
            server_id = getattr(utils.client, "server_id", "") or ""
        link = "%s/web/#/details?id=%s&serverId=%s" % (
            server_url, item_id, server_id
        )
        from gi.repository import Gdk
        Gdk.Display.get_default().get_clipboard().set(link)
        utils.send_toast(_("Link copied"), 2)
