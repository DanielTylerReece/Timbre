# __init__.py
#
# Copyright (C) 2026 Tyler Reece
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Timbre AI discovery layer (Phase 8).

Pure-Python, gi-free package: a user-configured LLM endpoint powers track radio,
daily mixes, personal radios, AI-ranked artist popularity, and artist bios. The
cardinal rule — *the model never invents tracks* — is enforced by the catalog +
validation helpers in :mod:`catalog` and the feature orchestration in
:mod:`discovery`; the UI never depends on AI success (every feature has a
deterministic local fallback).
"""

from .provider import (
    AIError,
    AIProvider,
    AnthropicProvider,
    OpenAIProvider,
    make_provider,
)

__all__ = [
    "AIError",
    "AIProvider",
    "AnthropicProvider",
    "OpenAIProvider",
    "make_provider",
]
