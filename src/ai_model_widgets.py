# ai_model_widgets.py
#
# Copyright (C) 2026 Tyler Reece
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Shared GTK glue for the AI provider/endpoint/model rows.

Single source of truth for the behavior shared by the onboarding AI page and
the Preferences AI group: picking a provider prefills the endpoint and turns
the model row into a dropdown of that provider's models with a Custom escape
hatch. The pure logic lives in :mod:`lib.ai.model_presets`; this module only
drives Adw widgets from it.

:class:`AIModelRows` is constructed with the four already-built template
widgets (provider combo, endpoint entry, model combo, custom-model entry) and a
``provider_for_index`` mapping the provider combo's selection index to a
provider key (``"none"`` / ``"openai"`` / ``"anthropic"``). Callers connect the
provider combo's ``notify::selected`` to :meth:`on_provider_changed` and read
the chosen model via :meth:`current_model`.
"""

from gettext import gettext as _

from .lib.ai import model_presets

# Trailing label appended to every provider's model list.
_CUSTOM_LABEL = _("Custom…")


class AIModelRows:
    """Drives endpoint prefill + model dropdown/custom toggling."""

    def __init__(
        self,
        *,
        provider_row,
        endpoint_row,
        model_combo,
        model_entry,
        provider_for_index,
    ):
        self._provider_row = provider_row
        self._endpoint_row = endpoint_row
        self._model_combo = model_combo
        self._model_entry = model_entry
        self._provider_for_index = provider_for_index
        # Choices currently loaded into the combo (excludes the Custom label).
        self._choices = []
        self._model_combo.connect("notify::selected", self._on_model_combo_changed)

    # ------------------------------------------------------------------ #
    # Provider                                                           #
    # ------------------------------------------------------------------ #

    def _provider_name(self):
        return self._provider_for_index.get(
            self._provider_row.get_selected(), "none"
        )

    def on_provider_changed(self, *args):
        """Provider combo changed: prefill endpoint, rebuild model dropdown."""
        provider = self._provider_name()

        new_endpoint = model_presets.prefill_endpoint(
            provider, self._endpoint_row.get_text()
        )
        if new_endpoint is not None:
            self._endpoint_row.set_text(new_endpoint)

        # Switching provider resets the model to that provider's default.
        self._rebuild_model_combo(provider, current_model="")

    # ------------------------------------------------------------------ #
    # Model                                                              #
    # ------------------------------------------------------------------ #

    def load_model(self, current_model):
        """Build the model dropdown for the current provider + saved model.

        A saved model not in the provider's list selects Custom and shows the
        entry pre-filled with the saved value.
        """
        self._rebuild_model_combo(self._provider_name(), current_model)

    def _rebuild_model_combo(self, provider, current_model):
        self._choices = model_presets.model_choices(provider)

        if not self._choices:
            # Provider "none" (or unknown): hide both model widgets.
            self._model_combo.set_visible(False)
            self._model_entry.set_visible(False)
            self._set_combo_strings([])
            return

        self._model_combo.set_visible(True)
        self._set_combo_strings(self._choices + [_CUSTOM_LABEL])

        index = model_presets.initial_model_index(provider, current_model)
        self._model_combo.set_selected(index)
        if index == len(self._choices):
            # Custom: reveal entry with the saved (or empty) value.
            self._model_entry.set_visible(True)
            self._model_entry.set_text(current_model or "")
        else:
            self._model_entry.set_visible(False)
            self._model_entry.set_text(self._choices[index])

    def _on_model_combo_changed(self, *args):
        if not self._choices:
            return
        index = self._model_combo.get_selected()
        if index == len(self._choices):
            # Custom selected: reveal the free-text entry, keep its value.
            self._model_entry.set_visible(True)
        else:
            self._model_entry.set_visible(False)
            self._model_entry.set_text(self._choices[index])

    def _set_combo_strings(self, strings):
        from gi.repository import Gtk

        model = Gtk.StringList()
        for s in strings:
            model.append(s)
        self._model_combo.set_model(model)

    # ------------------------------------------------------------------ #
    # Read-out                                                           #
    # ------------------------------------------------------------------ #

    def current_model(self):
        """The model string to persist (combo selection or custom entry)."""
        if not self._choices:
            return self._model_entry.get_text()
        index = self._model_combo.get_selected()
        if index == len(self._choices):
            return self._model_entry.get_text()
        if 0 <= index < len(self._choices):
            return self._choices[index]
        return self._model_entry.get_text()
