# secret_storage.py
#
# Copyright 2025 Nokse <nokse@posteo.com>
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

"""libsecret-backed credential storage for Timbre.

Stores Jellyfin auth credentials (and the AI API key) in the GNOME Keyring via
libsecret. Each field is stored as its own secret keyed by a schema attribute,
so callers can ``save(token="…", user_id="…")`` and ``load()`` them back.

If the Secret service is unavailable (e.g. a headless test environment with no
keyring/D-Bus session) the store logs a warning and transparently falls back to
an in-memory dict so the app and the test suite never crash.
"""

import logging

logger = logging.getLogger(__name__)

SCHEMA_ID = "io.github.tylerreece.timbre"

# Attribute keys (also the field names accepted by save()/returned by load()).
ATTR_KEYS = ("server-url", "user-id", "server-id", "token", "ai-api-key")

# Map convenient kwargs (underscored) -> attribute keys (hyphenated).
_FIELD_TO_ATTR = {key.replace("-", "_"): key for key in ATTR_KEYS}


class SecretStore:
    """Per-field libsecret credential store with an in-memory fallback."""

    def __init__(self) -> None:
        self._schema = None
        self._secret = None
        self._memory: dict = {}

        try:
            import gi

            gi.require_version("Secret", "1")
            from gi.repository import Secret

            self._secret = Secret
            self._schema = Secret.Schema.new(
                SCHEMA_ID,
                Secret.SchemaFlags.NONE,
                {key: Secret.SchemaAttributeType.STRING for key in ATTR_KEYS},
            )
            # Probe the Secret service so we can fall back early if it's absent.
            Secret.Service.get_sync(Secret.ServiceFlags.NONE, None)
        except Exception:
            logger.warning(
                "Secret service unavailable — falling back to in-memory "
                "credential storage (credentials will not persist)",
                exc_info=True,
            )
            self._secret = None
            self._schema = None

    @property
    def available(self) -> bool:
        """True when the libsecret backend is active (else in-memory fallback)."""
        return self._secret is not None and self._schema is not None

    @staticmethod
    def _normalize(field: str) -> str:
        """Accept either hyphenated attribute keys or underscored kwargs."""
        if field in ATTR_KEYS:
            return field
        if field in _FIELD_TO_ATTR:
            return _FIELD_TO_ATTR[field]
        raise KeyError(f"unknown credential field: {field!r}")

    def save(self, **fields) -> None:
        """Store one or more credential fields.

        Example: ``store.save(token="abc", user_id="u1")``.
        """
        for field, value in fields.items():
            attr = self._normalize(field)
            if value is None:
                continue
            value = str(value)
            if not self.available:
                self._memory[attr] = value
                continue
            self._secret.password_store_sync(
                self._schema,
                {attr: attr},
                self._secret.COLLECTION_DEFAULT,
                f"{SCHEMA_ID}:{attr}",
                value,
                None,
            )

    def load(self) -> dict:
        """Return a dict of all stored credential fields (hyphenated keys).

        Missing fields are omitted from the result.
        """
        out: dict = {}
        for attr in ATTR_KEYS:
            if not self.available:
                if attr in self._memory:
                    out[attr] = self._memory[attr]
                continue
            try:
                value = self._secret.password_lookup_sync(
                    self._schema, {attr: attr}, None
                )
            except Exception:
                logger.exception("Failed to load credential field %s", attr)
                value = None
            if value is not None:
                out[attr] = value
        return out

    def clear(self) -> None:
        """Remove all stored credential fields."""
        if not self.available:
            self._memory.clear()
            return
        for attr in ATTR_KEYS:
            try:
                self._secret.password_clear_sync(self._schema, {attr: attr}, None)
            except Exception:
                logger.exception("Failed to clear credential field %s", attr)
