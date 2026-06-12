# model_presets.py
#
# Copyright (C) 2026 Tyler Reece
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Provider default presets for the AI Discovery UI.

Pure-Python, gi-free. Backs both the onboarding AI page and the Preferences AI
group: when a provider is picked the endpoint row is prefilled with that
provider's default, and the model becomes a dropdown of that provider's known
models with a Custom escape hatch. Single source of truth for both surfaces.

Anthropic model IDs are exact/current (no date suffixes). The OpenAI list is a
curated best-effort; the Custom option covers everything else, including local /
Ollama-style endpoints.
"""

# Provider -> default API base URL.
DEFAULT_ENDPOINTS = {
    "anthropic": "https://api.anthropic.com",
    "openai": "https://api.openai.com/v1",
}

# Provider -> ordered model choices. The first entry is the default selection
# when nothing is saved (cheap + fast for this workload).
PROVIDER_MODELS = {
    "anthropic": [
        "claude-haiku-4-5",
        "claude-sonnet-4-6",
        "claude-opus-4-8",
        "claude-fable-5",
    ],
    "openai": ["gpt-4o-mini", "gpt-4o", "gpt-4.1-mini", "gpt-4.1"],
}

# Sentinel for the "Custom…" escape hatch (free-text model entry).
CUSTOM = "custom"

# Default-endpoint values, used to detect "field holds *some* provider default"
# so switching providers swaps the prefill but a hand-typed endpoint is left.
_DEFAULT_VALUES = set(DEFAULT_ENDPOINTS.values())


def prefill_endpoint(provider, current_text):
    """Return the endpoint to set, or None to leave the field alone.

    Returns ``provider``'s default endpoint when ``current_text`` is empty (or
    whitespace) OR equals a *different* provider's default (so switching
    providers swaps defaults). Returns None when the field already holds this
    provider's own default (no-op) or the user has typed a custom endpoint.
    """
    default = DEFAULT_ENDPOINTS.get(provider)
    if default is None:
        return None
    stripped = (current_text or "").strip()
    if not stripped:
        return default
    if stripped == default:
        return None
    if stripped in _DEFAULT_VALUES:
        # Holds a different provider's default → swap to this provider's.
        return default
    # Hand-typed / custom endpoint → leave it.
    return None


def model_choices(provider):
    """Ordered list of known model IDs for ``provider`` (empty if unknown).

    The UI appends its own trailing "Custom…" label; this returns only the
    real model IDs.
    """
    return list(PROVIDER_MODELS.get(provider, []))


def initial_model_index(provider, current_model):
    """Index to select in the model combo for ``current_model``.

    - Saved model present in the list → its index.
    - Saved model set but not in the list → the Custom index (``len(choices)``).
    - Nothing saved → 0 (first model; cheap+fast default).
    - Unknown provider → 0.
    """
    choices = model_choices(provider)
    if not choices:
        return 0
    if not current_model:
        return 0
    try:
        return choices.index(current_model)
    except ValueError:
        return len(choices)  # Custom slot
