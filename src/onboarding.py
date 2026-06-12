# onboarding.py
#
# Copyright (C) 2026 Tyler Reece
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Onboarding dialog (Adw.Dialog + Adw.NavigationView).

A thin GTK presenter over :class:`~lib.onboarding_flow.OnboardingFlow`. All
network work runs on worker threads; UI updates are marshalled back with
``GLib.idle_add``. On completion it applies the flow's ``persistence_plan()``
to SecretStore + GSettings and invokes ``on_complete(client)`` so the window
can transition into the logged-in runtime.
"""

import logging
import time
from gettext import gettext as _

from gi.repository import Adw, GLib, Gtk

from .ai_model_widgets import AIModelRows
from .lib import utils
from .lib.jellyfin.client import JellyfinClient, JellyfinError
from .lib.jellyfin.sync import LibrarySync
from .lib.onboarding_flow import OnboardingFlow, Step, quick_connect_expired

logger = logging.getLogger(__name__)

_QC_POLL_SECONDS = 2


@Gtk.Template(resource_path="/io/github/tylerreece/timbre/ui/onboarding.ui")
class JTOnboarding(Adw.Dialog):
    __gtype_name__ = "JTOnboarding"

    nav_view = Gtk.Template.Child()

    server_url_row = Gtk.Template.Child()
    server_banner = Gtk.Template.Child()
    server_connect_button = Gtk.Template.Child()
    server_spinner = Gtk.Template.Child()

    quick_connect_code = Gtk.Template.Child()
    quick_connect_web_link = Gtk.Template.Child()
    quick_connect_expired_banner = Gtk.Template.Child()

    password_banner = Gtk.Template.Child()
    username_row = Gtk.Template.Child()
    password_row = Gtk.Template.Child()
    password_signin_button = Gtk.Template.Child()
    password_spinner = Gtk.Template.Child()

    libraries_group = Gtk.Template.Child()
    libraries_continue_button = Gtk.Template.Child()
    libraries_banner = Gtk.Template.Child()
    libraries_empty = Gtk.Template.Child()

    ai_provider_row = Gtk.Template.Child()
    ai_endpoint_row = Gtk.Template.Child()
    ai_model_combo = Gtk.Template.Child()
    ai_model_row = Gtk.Template.Child()
    ai_key_row = Gtk.Template.Child()

    sync_progress = Gtk.Template.Child()
    sync_status = Gtk.Template.Child()

    def __init__(self, secret_store, settings, db, on_complete, **kwargs):
        super().__init__(**kwargs)
        self._secret_store = secret_store
        self._settings = settings
        self._db = db
        self._on_complete = on_complete

        existing_device_id = settings.get_string("device-id") or None
        self.flow = OnboardingFlow(existing_device_id=existing_device_id)

        self._client = None
        self.completed = False  # True once sign-in finished (read by window)
        self._alive = True  # cleared on close; gates run_async callbacks
        self._qc_cancelled = False  # set when QC polling is cancelled
        self._qc_state = None  # QCState during quick connect
        self._qc_timeout_id = None
        self._qc_started_at = None  # time.monotonic() when QC code was shown
        self._library_switches = {}  # id -> Adw.SwitchRow

        # Provider combo order: 0 None, 1 OpenAI-compatible, 2 Anthropic.
        self._ai_rows = AIModelRows(
            provider_row=self.ai_provider_row,
            endpoint_row=self.ai_endpoint_row,
            model_combo=self.ai_model_combo,
            model_entry=self.ai_model_row,
            provider_for_index={0: "none", 1: "openai", 2: "anthropic"},
        )
        self.ai_provider_row.connect(
            "notify::selected", self._ai_rows.on_provider_changed
        )

    # ------------------------------------------------------------------ #
    # Lifecycle                                                          #
    # ------------------------------------------------------------------ #

    def do_closed(self):
        # Dialog dismissed (user closed, or _sync_done closed it): stop any
        # Quick Connect polling and gate in-flight run_async callbacks.
        self._alive = False
        self._cancel_qc_polling()

    # ------------------------------------------------------------------ #
    # Step 1: Server                                                     #
    # ------------------------------------------------------------------ #

    @Gtk.Template.Callback("on_server_connect")
    def on_server_connect(self, *args):
        raw = self.server_url_row.get_text()
        if not self.flow.is_server_url_valid(raw):
            self._show_server_error(_("Enter a server URL."))
            return
        url = self.flow.normalize_server_url(raw)
        self.server_banner.set_revealed(False)
        self.server_spinner.set_visible(True)
        self.server_connect_button.set_sensitive(False)

        device_id = self.flow.device_id

        def work():
            client = JellyfinClient(url, device_id=device_id)
            info = client.public_info()
            qc_enabled = client.quick_connect_enabled()
            return client, info, qc_enabled

        def on_done(result):
            client, info, qc_enabled = result
            self._server_ok(client, url, info, qc_enabled)

        utils.run_async(
            work,
            on_done=on_done,
            on_error=lambda exc: self._server_failed(str(exc)),
            owner=self,
        )

    def _server_failed(self, message):
        self.server_spinner.set_visible(False)
        self.server_connect_button.set_sensitive(True)
        self._show_server_error(_("Could not reach that server."))
        logger.info("server validation failed: %s", message)
        return False

    def _show_server_error(self, text):
        self.server_banner.set_title(text)
        self.server_banner.set_revealed(True)

    def _server_ok(self, client, url, info, qc_enabled):
        self.server_spinner.set_visible(False)
        self.server_connect_button.set_sensitive(True)
        self._client = client
        self.flow.set_server_validated(url, info)
        self.flow.set_quick_connect_enabled(qc_enabled)
        self._enter_auth_step()
        return False

    # ------------------------------------------------------------------ #
    # Step 2: Auth                                                       #
    # ------------------------------------------------------------------ #

    def _enter_auth_step(self):
        if self.flow.auth_mode == "quick_connect":
            self._start_quick_connect()
        else:
            self.nav_view.push_by_tag("password")

    def _start_quick_connect(self, *, push=True):
        if push:
            self.nav_view.push_by_tag("quick-connect")
        # Fresh attempt: clear any prior expiry state.
        self._qc_cancelled = False
        self.quick_connect_expired_banner.set_revealed(False)
        self.quick_connect_code.set_label("------")

        def on_error(exc):
            logger.info("quick connect initiate failed: %s", exc)
            self._fallback_to_password()

        utils.run_async(
            self._client.quick_connect_initiate,
            on_done=self._quick_connect_started,
            on_error=on_error,
            owner=self,
        )

    @Gtk.Template.Callback("on_quick_connect_new_code")
    def on_quick_connect_new_code(self, *args):
        # Re-initiate Quick Connect with a fresh code (we're already on the page).
        self._cancel_qc_polling()
        self._start_quick_connect(push=False)

    def _quick_connect_started(self, state):
        self._qc_state = state
        self._qc_started_at = time.monotonic()
        self.quick_connect_code.set_label(state.code or "------")
        # Direct link to the Jellyfin web UI page where the code is entered
        # (Settings → Quick Connect). The SPA serves any #/ route, so this is
        # stable across 10.9+.
        if self._client is not None:
            self.quick_connect_web_link.set_uri(
                f"{self._client.base}/web/#/quickconnect"
            )
        self._qc_timeout_id = GLib.timeout_add_seconds(
            _QC_POLL_SECONDS, self._poll_quick_connect
        )
        return False

    def _poll_quick_connect(self):
        if self._qc_state is None or self._qc_cancelled:
            return False

        # Client-side expiry: stop polling once the server-side code lifetime has
        # certainly elapsed (it may have expired earlier — a poll 404 below also
        # ends polling).
        if quick_connect_expired(self._qc_started_at, time.monotonic()):
            self._quick_connect_code_expired()
            return False

        secret = self._qc_state.secret

        def work():
            # Returns: ("auth", AuthResult) on success, ("pending", None) while
            # the user hasn't approved yet, or ("expired", None) when the server
            # 404'd the code (it forgot it). Other transient errors -> pending,
            # so a blip doesn't kill the flow.
            try:
                authenticated = self._client.quick_connect_poll(secret)
            except JellyfinError as exc:
                if exc.status == 404:
                    return ("expired", None)
                return ("pending", None)
            except Exception:  # noqa: BLE001
                return ("pending", None)
            if not authenticated:
                return ("pending", None)
            return ("auth", self._client.authenticate_quick_connect(secret))

        def on_done(result):
            outcome, payload = result
            if outcome == "auth":
                self._authenticated(payload)
            elif outcome == "expired":
                self._quick_connect_code_expired()

        def on_error(exc):
            logger.info("quick connect auth failed: %s", exc)

        utils.run_async(work, on_done=on_done, on_error=on_error, owner=self)
        return True  # keep polling until authenticated / expired

    def _quick_connect_code_expired(self):
        # Stop polling and prompt for a fresh code.
        logger.info("quick connect code expired")
        self._cancel_qc_polling()
        self.quick_connect_code.set_label("------")
        self.quick_connect_expired_banner.set_revealed(True)
        return False

    @Gtk.Template.Callback("on_use_password")
    def on_use_password(self, *args):
        self.flow.use_password_instead()
        self._cancel_qc_polling()
        self.nav_view.push_by_tag("password")
        return True  # stop default LinkButton URI handling

    def _fallback_to_password(self):
        self.flow.use_password_instead()
        self.nav_view.push_by_tag("password")
        return False

    def _cancel_qc_polling(self):
        self._qc_cancelled = True
        if self._qc_timeout_id is not None:
            GLib.source_remove(self._qc_timeout_id)
            self._qc_timeout_id = None
        self._qc_state = None
        self._qc_started_at = None

    @Gtk.Template.Callback("on_password_signin")
    def on_password_signin(self, *args):
        username = self.username_row.get_text()
        password = self.password_row.get_text()
        if not self.flow.are_password_credentials_valid(username, password):
            self.password_banner.set_title(_("Enter a username."))
            self.password_banner.set_revealed(True)
            return
        self.password_banner.set_revealed(False)
        self.password_spinner.set_visible(True)
        self.password_signin_button.set_sensitive(False)

        utils.run_async(
            lambda: self._client.authenticate(username, password),
            on_done=self._authenticated,
            on_error=lambda exc: self._password_failed(str(exc)),
            owner=self,
        )

    def _password_failed(self, message):
        self.password_spinner.set_visible(False)
        self.password_signin_button.set_sensitive(True)
        self.password_banner.set_title(_("Sign in failed."))
        self.password_banner.set_revealed(True)
        logger.info("password sign in failed: %s", message)
        return False

    def _authenticated(self, res):
        # Ignore stale in-flight auth results. A QC poll worker can complete
        # after the flow already advanced past AUTH (e.g. a second poll result,
        # or password sign-in won the race). Combined with run_async's
        # owner-guard (drops results once the dialog is closed) this makes
        # post-cancellation worker results no-ops.
        if self.flow.step > Step.AUTH:
            logger.debug("ignoring stale auth result: flow already past AUTH")
            return False
        self._cancel_qc_polling()
        self.flow.set_authenticated(
            token=res.token, user_id=res.user_id, server_id=res.server_id
        )
        # Client already has state applied by authenticate*; fetch libraries.
        self._load_libraries()
        return False

    # ------------------------------------------------------------------ #
    # Step 3: Libraries                                                  #
    # ------------------------------------------------------------------ #

    def _load_libraries(self):
        utils.run_async(
            self._client.music_libraries,
            on_done=self._libraries_loaded,
            on_error=self._libraries_failed,
            owner=self,
        )

    def _ensure_libraries_page(self):
        """Push the libraries page if it isn't already the visible page.

        Library fetch is kicked off from the auth page (QC/password); the result
        paths (rows, empty-state, error banner) all surface on the libraries
        page, so make sure it's shown without re-pushing on retry.
        """
        visible = self.nav_view.get_visible_page()
        if visible is None or visible.get_tag() != "libraries":
            self.nav_view.push_by_tag("libraries")

    def _clear_library_rows(self):
        for lib_id, row in self._library_switches.items():
            self.libraries_group.remove(row)
        self._library_switches.clear()

    def _libraries_failed(self, exc):
        # ERROR path: the fetch raised. Do NOT advance and do NOT treat it as
        # "zero libraries" — surface a retryable error on the libraries page so
        # the user can re-fetch or go Back to re-check the server.
        logger.info("library fetch failed: %s", exc)
        self._clear_library_rows()
        self.libraries_empty.set_visible(False)
        self.libraries_group.set_visible(False)
        self.libraries_continue_button.set_visible(False)
        self.libraries_banner.set_revealed(True)
        self._ensure_libraries_page()
        return False

    @Gtk.Template.Callback("on_libraries_retry")
    def on_libraries_retry(self, *args):
        self.libraries_banner.set_revealed(False)
        self._load_libraries()

    @Gtk.Template.Callback("on_libraries_back")
    def on_libraries_back(self, *args):
        # Re-check the server: pop back to the server step.
        self.nav_view.pop_to_tag("server")

    def _libraries_loaded(self, libs):
        lib_dicts = [{"id": lib.id, "name": lib.name} for lib in libs]
        self.flow.set_libraries(lib_dicts)

        # A successful fetch clears any prior error banner.
        self.libraries_banner.set_revealed(False)

        if self.flow.step == Step.AI:
            # Exactly one library -> auto-skipped. Go straight to AI.
            self._enter_ai_step()
            return False

        if not self.flow.has_libraries:
            # ZERO music libraries: show an empty-state with Back. Do NOT
            # silently auto-advance to AI/sync (syncing nothing is a dead end).
            self._clear_library_rows()
            self.libraries_group.set_visible(False)
            self.libraries_continue_button.set_visible(False)
            self.libraries_empty.set_visible(True)
            self._ensure_libraries_page()
            return False

        # Two or more: build switch rows for the user to choose.
        self.libraries_empty.set_visible(False)
        self.libraries_group.set_visible(True)
        self.libraries_continue_button.set_visible(True)
        self._clear_library_rows()
        for lib in lib_dicts:
            row = Adw.SwitchRow(title=lib["name"], active=True)
            row.connect("notify::active", self._on_library_toggled, lib["id"])
            self.libraries_group.add(row)
            self._library_switches[lib["id"]] = row
        self._update_libraries_continue()
        self._ensure_libraries_page()
        return False

    def _on_library_toggled(self, row, _param, lib_id):
        self.flow.set_library_selected(lib_id, row.get_active())
        self._update_libraries_continue()

    def _update_libraries_continue(self):
        self.libraries_continue_button.set_sensitive(
            self.flow.can_confirm_libraries()
        )

    @Gtk.Template.Callback("on_libraries_continue")
    def on_libraries_continue(self, *args):
        self.flow.confirm_libraries()
        self._enter_ai_step()

    # ------------------------------------------------------------------ #
    # Step 4: AI                                                         #
    # ------------------------------------------------------------------ #

    def _enter_ai_step(self):
        # Provider defaults to None on a fresh onboarding; build the model
        # widgets for whatever the provider combo currently shows.
        self._ai_rows.load_model("")
        self.nav_view.push_by_tag("ai")

    @Gtk.Template.Callback("on_ai_skip")
    def on_ai_skip(self, *args):
        self.flow.skip_ai()
        self._start_initial_sync()

    @Gtk.Template.Callback("on_ai_continue")
    def on_ai_continue(self, *args):
        provider_map = {0: "none", 1: "openai", 2: "anthropic"}
        provider = provider_map.get(self.ai_provider_row.get_selected(), "none")
        self.flow.set_ai(
            provider=provider,
            endpoint=self.ai_endpoint_row.get_text(),
            model=self._ai_rows.current_model(),
            api_key=self.ai_key_row.get_text(),
        )
        self._start_initial_sync()

    # ------------------------------------------------------------------ #
    # Step 5: Initial sync                                               #
    # ------------------------------------------------------------------ #

    # Stage order + relative weight of each sync stage in the overall bar.
    # The tracks stage dominates a real library (thousands of rows) so it is
    # weighted heaviest; the bar therefore visibly moves through it instead of
    # parking while the long tracks walk runs. Weights are cumulative fractions
    # of the bar each stage spans.
    _SYNC_STAGES = ("artists", "albums", "tracks", "playlists", "genres")
    _SYNC_WEIGHTS = {
        "artists": 0.10,
        "albums": 0.20,
        "tracks": 0.55,
        "playlists": 0.10,
        "genres": 0.05,
    }
    _SYNC_LABELS = {
        "artists": _("Syncing artists…"),
        "albums": _("Syncing albums…"),
        "tracks": _("Syncing tracks…"),
        "playlists": _("Syncing playlists…"),
        "genres": _("Syncing genres…"),
    }

    def _start_initial_sync(self):
        self._persist()
        self.nav_view.push_by_tag("sync")

        library_ids = self.flow.selected_library_ids
        sync = LibrarySync(self._client, self._db)

        # progress_cb fires from worker threads (one per library); the fraction
        # is computed from a cumulative stage weighting so a multi-page stage
        # advances the bar within its own band.
        self._sync_stage_base = self._cumulative_stage_bases()

        def progress_cb(stage, done, total):
            GLib.idle_add(self._sync_progress, stage, done, total)

        def work():
            try:
                sync.full_sync(library_ids, progress_cb=progress_cb)
                sync.seed_history()
            except Exception:  # noqa: BLE001
                logger.exception("initial sync failed")

        utils.run_async(work, on_done=lambda _r: self._sync_done(), owner=self)

    def _cumulative_stage_bases(self):
        """Map each stage -> (band_start, band_width) over the [0,1] bar."""
        bases = {}
        acc = 0.0
        for stage in self._SYNC_STAGES:
            width = self._SYNC_WEIGHTS.get(stage, 0.0)
            bases[stage] = (acc, width)
            acc += width
        return bases

    def _sync_progress(self, stage, done, total):
        """Advance the bar + label on every callback (per page and per stage).

        The bar never goes backwards: with multiple library worker threads the
        callbacks for different stages can interleave, so the displayed fraction
        is clamped monotonic. The label always reflects the stage of the latest
        callback so the user sees the long tracks stage scroll by.
        """
        if not self._alive:
            # Dialog closed mid-sync: the idle callbacks marshalled from worker
            # threads must not touch the (disposed) progress widgets.
            return False
        base, width = self._sync_stage_base.get(stage, (0.0, 0.0))
        within = (done / total) if total else 1.0
        within = min(max(within, 0.0), 1.0)
        fraction = base + width * within
        # Monotonic: don't let an interleaved earlier-stage callback rewind it.
        current = self.sync_progress.get_fraction()
        if fraction > current:
            self.sync_progress.set_fraction(fraction)
        self.sync_status.set_label(
            self._SYNC_LABELS.get(stage, _("Syncing %s…") % stage)
        )
        return False

    def _sync_done(self):
        self.sync_progress.set_fraction(1.0)
        self.sync_status.set_label(_("Done"))
        self.flow.mark_done()
        client = self._client
        self.completed = True
        self.close()
        if self._on_complete is not None:
            self._on_complete(client)
        return False

    # ------------------------------------------------------------------ #
    # Persistence                                                        #
    # ------------------------------------------------------------------ #

    def _persist(self):
        plan = self.flow.persistence_plan()
        secrets = {
            key.replace("-", "_"): value
            for key, value in plan["secrets"].items()
            if value is not None
        }
        if secrets:
            self._secret_store.save(**secrets)
        s = plan["settings"]
        self._settings.set_string("server-url", s["server-url"])
        self._settings.set_string("device-id", s["device-id"])
        self._settings.set_strv("selected-libraries", s["selected-libraries"])
        self._settings.set_string("ai-provider", s["ai-provider"])
        self._settings.set_string("ai-endpoint", s["ai-endpoint"])
        self._settings.set_string("ai-model", s["ai-model"])
