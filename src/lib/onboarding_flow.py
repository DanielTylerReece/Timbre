# onboarding_flow.py
#
# Copyright (C) 2026 Tyler Reece
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Pure-Python onboarding state machine.

No gi/GTK imports — this is the testable brain behind the onboarding dialog.
The GTK ``Onboarding`` view (src/onboarding.py) is a thin presenter: it asks
this object what step to show, hands user input back in, and reads the final
``persistence_plan()`` to write SecretStore + GSettings.

Step order::

    SERVER -> AUTH -> LIBRARIES -> AI -> SYNC -> DONE

The LIBRARIES step is auto-skipped when the server exposes exactly one music
library (it is auto-selected). Auth mode (quick_connect vs password) is derived
from ``quick_connect_enabled`` but the user can force password mode.
"""

import uuid
from enum import IntEnum


class Step(IntEnum):
    SERVER = 0
    AUTH = 1
    LIBRARIES = 2
    AI = 3
    SYNC = 4
    DONE = 5


_VALID_AI_PROVIDERS = ("none", "openai", "anthropic")

# Jellyfin expires a Quick Connect code server-side after a few minutes. Stop
# polling client-side at this bound too, so a code that the server has already
# forgotten doesn't poll forever.
QUICK_CONNECT_TIMEOUT_SECONDS = 5 * 60


def quick_connect_expired(start_monotonic, now_monotonic, *, code_not_found=False):
    """Decide whether to stop polling a Quick Connect code.

    Pure + clock-injected so it's testable without real time. Returns ``True``
    when polling should stop because the code has expired — either because the
    client-side timeout elapsed, or because the server reported the code is
    gone (a 404 on poll, surfaced here as ``code_not_found``).

    ``start_monotonic`` / ``now_monotonic`` are ``time.monotonic()`` readings.
    """
    if code_not_found:
        return True
    return (now_monotonic - start_monotonic) >= QUICK_CONNECT_TIMEOUT_SECONDS


class OnboardingFlow:
    """Owns onboarding step progression, validation, and the persistence plan."""

    def __init__(self, existing_device_id: str | None = None):
        self.step = Step.SERVER

        self._device_id = existing_device_id or None

        # Server.
        self.server_url: str | None = None
        self.server_info: dict = {}

        # Auth.
        self._quick_connect_enabled = False
        self._force_password = False
        self.token: str | None = None
        self.user_id: str | None = None
        self.server_id: str | None = None

        # Libraries: ordered list of {"id", "name"} dicts + a selection set.
        self.libraries: list[dict] = []
        self._selected: dict[str, bool] = {}

        # AI plan.
        self.ai_plan: dict = {
            "provider": "none",
            "endpoint": "",
            "model": "",
            "api_key": "",
        }

    # ------------------------------------------------------------------ #
    # Device id (generated once, persisted).                             #
    # ------------------------------------------------------------------ #

    @property
    def device_id(self) -> str:
        if not self._device_id:
            self._device_id = str(uuid.uuid4())
        return self._device_id

    # ------------------------------------------------------------------ #
    # Server step.                                                       #
    # ------------------------------------------------------------------ #

    @staticmethod
    def normalize_server_url(raw: str) -> str:
        """Trim, default to http://, strip a trailing slash."""
        url = (raw or "").strip()
        if not url:
            return ""
        if "://" not in url:
            url = "http://" + url
        return url.rstrip("/")

    def is_server_url_valid(self, raw: str) -> bool:
        return bool((raw or "").strip())

    def set_server_validated(self, server_url: str, info: dict) -> None:
        """Record a successfully reached server and advance to AUTH."""
        self.server_url = self.normalize_server_url(server_url)
        self.server_info = info or {}
        # Adopt the server's reported Id if we don't already have one from auth.
        if not self.server_id:
            self.server_id = self.server_info.get("Id")
        self.step = Step.AUTH

    # ------------------------------------------------------------------ #
    # Auth step.                                                         #
    # ------------------------------------------------------------------ #

    def set_quick_connect_enabled(self, enabled: bool) -> None:
        self._quick_connect_enabled = bool(enabled)

    @property
    def auth_mode(self) -> str:
        """'quick_connect' or 'password'."""
        if self._force_password or not self._quick_connect_enabled:
            return "password"
        return "quick_connect"

    def use_password_instead(self) -> None:
        self._force_password = True

    def are_password_credentials_valid(self, username: str, password: str) -> bool:
        # A username is required; an empty password is allowed (some servers
        # have password-less accounts).
        return bool((username or "").strip())

    def set_authenticated(self, token, user_id, server_id) -> None:
        """Record successful auth and advance to LIBRARIES."""
        self.token = token
        self.user_id = user_id
        self.server_id = server_id or self.server_id
        self.step = Step.LIBRARIES

    # ------------------------------------------------------------------ #
    # Libraries step.                                                    #
    # ------------------------------------------------------------------ #

    @property
    def has_libraries(self) -> bool:
        """True once at least one music library is available."""
        return bool(self.libraries)

    def set_libraries(self, libraries) -> None:
        """Provide available music libraries (dicts/objects with id+name).

        All default to selected. Step transition by count:

        * **0 libraries** — the server exposes no music library. Stay on
          LIBRARIES (the view renders an empty-state with a Back action). We do
          NOT auto-advance to AI/sync: sync over zero libraries is a dead end.
        * **exactly 1** — auto-selected and the LIBRARIES step is auto-skipped
          straight to AI.
        * **2+** — stay on LIBRARIES for the user to choose.
        """
        self.libraries = [
            {"id": _g(lib, "id"), "name": _g(lib, "name")} for lib in libraries
        ]
        self._selected = {lib["id"]: True for lib in self.libraries if lib["id"]}

        if len(self.libraries) == 1:
            # Exactly one: nothing to choose. Auto-advance.
            self.step = Step.AI
        else:
            # Zero (empty-state, no auto-advance) or many (user chooses).
            self.step = Step.LIBRARIES

    def set_library_selected(self, library_id: str, selected: bool) -> None:
        if library_id in self._selected:
            self._selected[library_id] = bool(selected)

    @property
    def selected_library_ids(self) -> list:
        """Selected library ids, in original library order."""
        return [
            lib["id"]
            for lib in self.libraries
            if lib["id"] and self._selected.get(lib["id"])
        ]

    def can_confirm_libraries(self) -> bool:
        return len(self.selected_library_ids) >= 1

    def confirm_libraries(self) -> None:
        if not self.can_confirm_libraries():
            return
        self.step = Step.AI

    # ------------------------------------------------------------------ #
    # AI step.                                                           #
    # ------------------------------------------------------------------ #

    def skip_ai(self) -> None:
        self.ai_plan = {"provider": "none", "endpoint": "", "model": "", "api_key": ""}
        self.step = Step.SYNC

    def set_ai(self, provider, endpoint, model, api_key) -> None:
        provider = provider if provider in _VALID_AI_PROVIDERS else "none"
        self.ai_plan = {
            "provider": provider,
            "endpoint": endpoint or "",
            "model": model or "",
            "api_key": api_key or "",
        }
        self.step = Step.SYNC

    # ------------------------------------------------------------------ #
    # Sync / done.                                                       #
    # ------------------------------------------------------------------ #

    def mark_done(self) -> None:
        self.step = Step.DONE

    # ------------------------------------------------------------------ #
    # Persistence plan.                                                  #
    # ------------------------------------------------------------------ #

    def persistence_plan(self) -> dict:
        """Return the SecretStore + GSettings writes the view should apply.

        ``secrets`` -> SecretStore.save(**); ``settings`` -> Gio.Settings.
        """
        secrets = {
            "server-url": self.server_url,
            "token": self.token,
            "user-id": self.user_id,
            "server-id": self.server_id,
        }
        if self.ai_plan.get("api_key"):
            secrets["ai-api-key"] = self.ai_plan["api_key"]

        settings = {
            "server-url": self.server_url or "",
            "device-id": self.device_id,
            "selected-libraries": list(self.selected_library_ids),
            "ai-provider": self.ai_plan.get("provider", "none"),
            "ai-endpoint": self.ai_plan.get("endpoint", ""),
            "ai-model": self.ai_plan.get("model", ""),
        }
        return {"secrets": secrets, "settings": settings}


def _g(obj, attr, default=None):
    """Read ``attr`` from a dict (by key) or an object (by attribute)."""
    if isinstance(obj, dict):
        return obj.get(attr, default)
    return getattr(obj, attr, default)
