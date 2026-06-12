# preferences.py
#
# Copyright (C) 2026 Tyler Reece
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Minimal Phase 4 preferences dialog.

Audio (sink, ALSA device, max bitrate, normalize, quadratic), Playback
(background-play), Server (read-only URL + Log out), AI (provider/endpoint/
model/key). Test-connection lands in Phase 8.
"""

import logging
from gettext import gettext as _

from gi.repository import Adw, Gio, Gtk

from .ai_model_widgets import AIModelRows
from .lib import utils

logger = logging.getLogger(__name__)

# Max-bitrate combo index -> stored bitrate value (bps; 0 = unlimited).
_BITRATE_VALUES = [0, 320000, 192000, 128000]
# AI provider combo index -> stored provider string.
_AI_PROVIDERS = ["none", "openai", "anthropic"]


@Gtk.Template(resource_path="/io/github/tylerreece/timbre/ui/preferences.ui")
class JTPreferences(Adw.PreferencesDialog):
    __gtype_name__ = "JTPreferences"

    sink_row = Gtk.Template.Child()
    alsa_device_row = Gtk.Template.Child()
    bitrate_row = Gtk.Template.Child()
    normalize_row = Gtk.Template.Child()
    quadratic_volume_row = Gtk.Template.Child()
    background_row = Gtk.Template.Child()
    external_lyrics_row = Gtk.Template.Child()
    server_url_row = Gtk.Template.Child()
    ai_provider_row = Gtk.Template.Child()
    ai_endpoint_row = Gtk.Template.Child()
    ai_model_combo = Gtk.Template.Child()
    ai_model_row = Gtk.Template.Child()
    ai_key_row = Gtk.Template.Child()
    push_bios_row = Gtk.Template.Child()
    ai_test_row = Gtk.Template.Child()

    def __init__(self, window, secret_store, alsa_devices, on_logout, **kwargs):
        super().__init__(**kwargs)
        self._window = window
        self._settings = window.settings
        self._secret_store = secret_store
        self._alsa_devices = alsa_devices
        self._on_logout = on_logout

        # Provider combo index -> provider key (same order as _AI_PROVIDERS).
        self._ai_rows = AIModelRows(
            provider_row=self.ai_provider_row,
            endpoint_row=self.ai_endpoint_row,
            model_combo=self.ai_model_combo,
            model_entry=self.ai_model_row,
            provider_for_index=dict(enumerate(_AI_PROVIDERS)),
        )

        self._populate_alsa()
        self._load_values()
        self._connect_handlers()

    # ------------------------------------------------------------------ #
    # Setup                                                              #
    # ------------------------------------------------------------------ #

    def _populate_alsa(self):
        model = Gtk.StringList()
        for dev in self._alsa_devices:
            model.append(dev["name"])
        self.alsa_device_row.set_model(model)

    def _load_values(self):
        self.sink_row.set_selected(self._settings.get_int("preferred-sink"))

        current_alsa = self._settings.get_string("alsa-device")
        for i, dev in enumerate(self._alsa_devices):
            if dev["hw_device"] == current_alsa:
                self.alsa_device_row.set_selected(i)
                break
        self._update_alsa_visibility()

        bitrate = self._settings.get_int("max-bitrate")
        self.bitrate_row.set_selected(
            _BITRATE_VALUES.index(bitrate) if bitrate in _BITRATE_VALUES else 0
        )

        self.normalize_row.set_active(self._settings.get_boolean("normalize"))
        self.quadratic_volume_row.set_active(
            self._settings.get_boolean("quadratic-volume")
        )
        self.background_row.set_active(
            self._settings.get_boolean("background-play")
        )
        self.external_lyrics_row.set_active(
            self._settings.get_boolean("external-lyrics")
        )

        url = self._settings.get_string("server-url")
        self.server_url_row.set_subtitle(url or _("Not connected"))

        provider = self._settings.get_string("ai-provider")
        self.ai_provider_row.set_selected(
            _AI_PROVIDERS.index(provider) if provider in _AI_PROVIDERS else 0
        )
        self.ai_endpoint_row.set_text(self._settings.get_string("ai-endpoint"))
        # Build the model dropdown for the saved provider; a saved model not in
        # the provider's list selects Custom and shows the entry with its value.
        self._ai_rows.load_model(self._settings.get_string("ai-model"))
        stored = self._secret_store.load()
        self._original_ai_key = stored.get("ai-api-key", "")
        self.ai_key_row.set_text(self._original_ai_key)

    def _connect_handlers(self):
        self.sink_row.connect("notify::selected", self._on_sink_changed)
        self.alsa_device_row.connect("notify::selected", self._on_alsa_changed)
        self.bitrate_row.connect("notify::selected", self._on_bitrate_changed)

        # normalize / quadratic-volume persist via settings.bind; the live
        # player-side update still goes through the window on toggle.
        self._settings.bind(
            "normalize",
            self.normalize_row,
            "active",
            Gio.SettingsBindFlags.DEFAULT,
        )
        self._settings.bind(
            "quadratic-volume",
            self.quadratic_volume_row,
            "active",
            Gio.SettingsBindFlags.DEFAULT,
        )
        self.normalize_row.connect("notify::active", self._on_normalize_changed)
        self.quadratic_volume_row.connect(
            "notify::active", self._on_quadratic_changed
        )
        self._settings.bind(
            "background-play",
            self.background_row,
            "active",
            Gio.SettingsBindFlags.DEFAULT,
        )
        self._settings.bind(
            "external-lyrics",
            self.external_lyrics_row,
            "active",
            Gio.SettingsBindFlags.DEFAULT,
        )
        self._settings.bind(
            "push-bios",
            self.push_bios_row,
            "active",
            Gio.SettingsBindFlags.DEFAULT,
        )

        # AI provider/endpoint/model are applied on dialog close (not per
        # keystroke); the API key is written to SecretStore on close only when
        # it actually changed.
        self.ai_provider_row.connect("notify::selected", self._on_ai_provider_changed)
        self.ai_test_row.connect("activated", self._on_test_connection)
        self.connect("closed", self._on_closed)

    # ------------------------------------------------------------------ #
    # Audio handlers                                                     #
    # ------------------------------------------------------------------ #

    def _update_alsa_visibility(self):
        # ALSA sink is index 2 (AudioSink.ALSA).
        self.alsa_device_row.set_visible(self.sink_row.get_selected() == 2)

    def _on_sink_changed(self, *args):
        sink = self.sink_row.get_selected()
        self._update_alsa_visibility()
        if hasattr(self._window, "change_audio_sink"):
            self._window.change_audio_sink(sink)

    def _on_alsa_changed(self, *args):
        idx = self.alsa_device_row.get_selected()
        if 0 <= idx < len(self._alsa_devices):
            device = self._alsa_devices[idx]["hw_device"]
            self._settings.set_string("alsa-device", device)
            if hasattr(self._window, "change_alsa_device"):
                self._window.change_alsa_device(device)

    def _on_bitrate_changed(self, *args):
        idx = self.bitrate_row.get_selected()
        if 0 <= idx < len(_BITRATE_VALUES):
            self._settings.set_int("max-bitrate", _BITRATE_VALUES[idx])

    def _on_normalize_changed(self, *args):
        if hasattr(self._window, "change_normalization"):
            self._window.change_normalization(self.normalize_row.get_active())

    def _on_quadratic_changed(self, *args):
        if hasattr(self._window, "change_quadratic_volume"):
            self._window.change_quadratic_volume(
                self.quadratic_volume_row.get_active()
            )

    # ------------------------------------------------------------------ #
    # AI handlers                                                        #
    # ------------------------------------------------------------------ #

    def _on_ai_provider_changed(self, *args):
        # Provider is a discrete combo selection (not free-text), so applying it
        # immediately is fine; endpoint/model/key are applied on close.
        provider = _AI_PROVIDERS[self.ai_provider_row.get_selected()]
        self._settings.set_string("ai-provider", provider)
        # Prefill the endpoint and rebuild the model dropdown for the provider.
        self._ai_rows.on_provider_changed()

    def _on_test_connection(self, *args):
        """Build a provider from the *current dialog fields* and ping it.

        Runs off the main thread (network) via run_async; a tiny prompt asks for
        ``{"ok": true}`` and the result is surfaced as a toast. Uses the live
        field values (not yet-persisted settings) so the user can test before
        closing the dialog.
        """
        from .lib.ai import make_provider

        provider_name = _AI_PROVIDERS[self.ai_provider_row.get_selected()]
        endpoint = self.ai_endpoint_row.get_text()
        model = self._ai_rows.current_model()
        key = self.ai_key_row.get_text()
        provider = make_provider(provider_name, endpoint, model, key)
        if provider is None:
            utils.send_toast(_("Configure a provider and API key first"), 3)
            return

        self.ai_test_row.set_sensitive(False)

        def work():
            return provider.complete_json(
                "You are a connectivity probe. Reply with JSON only.",
                'Respond with exactly {"ok": true}.',
                max_tokens=20,
            )

        def done(_result):
            self.ai_test_row.set_sensitive(True)
            utils.send_toast(_("✓ {model} responded").format(model=model), 3)

        def on_error(exc):
            self.ai_test_row.set_sensitive(True)
            utils.send_toast(_("Test failed: {err}").format(err=str(exc)[:80]), 4)

        utils.run_async(work, on_done=done, on_error=on_error, owner=self)

    def _on_closed(self, *args):
        # Apply endpoint/model on close (avoids a settings write per keystroke).
        self._settings.set_string("ai-endpoint", self.ai_endpoint_row.get_text())
        self._settings.set_string("ai-model", self._ai_rows.current_model())
        # Write the API key to SecretStore only when it actually changed.
        key = self.ai_key_row.get_text()
        if key != self._original_ai_key:
            self._secret_store.save(ai_api_key=key)
            self._original_ai_key = key

    # ------------------------------------------------------------------ #
    # Server / logout                                                    #
    # ------------------------------------------------------------------ #

    @Gtk.Template.Callback("on_logout")
    def on_logout(self, *args):
        self._secret_store.clear()
        self._settings.set_string("server-url", "")
        self._settings.set_strv("selected-libraries", [])
        self.close()
        if self._on_logout is not None:
            self._on_logout()
