# provider.py
#
# Copyright (C) 2026 Tyler Reece
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""LLM JSON-completion providers for the AI discovery layer.

No gi/GTK imports — pure ``requests``-backed HTTP so it stays headless-testable
against the conftest fake-HTTP-server fixture.

The single contract every provider satisfies is :meth:`AIProvider.complete_json`
— send a system + user prompt, get back parsed JSON (a dict or list). On a
malformed-JSON response the provider retries ONCE with a "Return ONLY valid
JSON" nudge appended to the user message; on any HTTP error (>=400) or a second
parse failure it raises :class:`AIError`. Callers in :mod:`discovery` translate
that into a deterministic local fallback, so AI failure never reaches the UI.
"""

import json
import logging
from abc import ABC, abstractmethod

import requests

logger = logging.getLogger(__name__)

# All provider HTTP calls use this timeout (seconds).
_TIMEOUT = 30

# Appended to the user message on the single retry after a JSON-parse failure.
_JSON_NUDGE = "\n\nReturn ONLY valid JSON, no prose."


class AIError(Exception):
    """Raised on any AI-call failure: HTTP >= 400, transport error, or a
    JSON-parse failure that survived the one retry."""


def _strip_fences(text: str) -> str:
    """Strip a leading/trailing markdown code fence from ``text`` if present.

    Handles ```` ```json ... ``` ```` and bare ```` ``` ... ``` ```` blocks.
    Returns the inner text stripped; a non-fenced string is returned trimmed.
    """
    s = (text or "").strip()
    if not s.startswith("```"):
        return s
    # Drop the first line (``` or ```json) and any trailing fence line.
    lines = s.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _parse_json(text: str):
    """Parse ``text`` (fences stripped) as JSON; raise ValueError on failure."""
    return json.loads(_strip_fences(text))


class AIProvider(ABC):
    """Abstract base: turn a system+user prompt into parsed JSON."""

    @abstractmethod
    def complete_json(self, system: str, user: str, max_tokens: int = 2000,
                      cache_prefix: str | None = None):
        """Return parsed JSON (dict | list).

        Raises :class:`AIError` on HTTP/parse failure (one retry with a
        'Return ONLY valid JSON' nudge before giving up).

        ``cache_prefix`` is an optional large, STABLE block of context (e.g. the
        candidate catalog) that callers want to reuse byte-for-byte across a
        burst of calls. When provided it is placed as a cacheable PREFIX (ahead
        of the volatile ``user`` instruction) so prompt caching can hit it:
        Anthropic marks it with ``cache_control: ephemeral``; OpenAI-compatible
        endpoints cache long identical prefixes automatically. The volatile
        instruction — and the JSON-retry nudge — stay in the suffix so the
        cached prefix is identical between the first call and the retry.
        """
        raise NotImplementedError


class OpenAIProvider(AIProvider):
    """OpenAI-compatible ``/chat/completions`` provider.

    Works with any OpenAI-style endpoint (OpenAI, local llama.cpp servers,
    OpenRouter, …). Includes ``response_format={"type":"json_object"}`` by
    default; if the server rejects it with a 400 mentioning ``response_format``
    the request is retried once without that field (older/compatible servers).
    """

    def __init__(self, endpoint: str, model: str, api_key: str):
        self.endpoint = (endpoint or "").rstrip("/") or "https://api.openai.com/v1"
        self.model = model
        self.api_key = api_key

    def _post(self, messages, *, use_response_format):
        body = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self._max_tokens,
        }
        if use_response_format:
            body["response_format"] = {"type": "json_object"}
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        try:
            resp = requests.post(
                f"{self.endpoint}/chat/completions",
                json=body,
                headers=headers,
                timeout=_TIMEOUT,
            )
        except requests.exceptions.RequestException as e:
            raise AIError(f"transport error: {e}") from e
        return resp

    def complete_json(self, system: str, user: str, max_tokens: int = 2000,
                      cache_prefix: str | None = None):
        self._max_tokens = max_tokens
        # OpenAI-compatible endpoints (OpenAI, llama.cpp, OpenRouter, …) cache
        # long identical PREFIXES automatically — no API param to set. So when a
        # cache_prefix is supplied, prepend it to the user content (catalog
        # FIRST, volatile instruction LAST) so the stable bytes form the prefix
        # the server can reuse across the burst of calls that share them. The
        # JSON-retry below re-sends the identical prefix + nudged suffix, so the
        # retry is itself a cache hit.
        if cache_prefix:
            user_content = f"{cache_prefix}\n\n{user}"
        else:
            user_content = user
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ]
        use_rf = True
        resp = self._post(messages, use_response_format=use_rf)
        # A 400 complaining about response_format -> retry once without it.
        if resp.status_code == 400 and "response_format" in (resp.text or "").lower():
            use_rf = False
            resp = self._post(messages, use_response_format=use_rf)
        if resp.status_code >= 400:
            raise AIError(f"HTTP {resp.status_code}: {(resp.text or '')[:200]}")
        content = self._extract(resp)
        try:
            return _parse_json(content)
        except ValueError:
            logger.debug("OpenAI JSON parse failed; retrying with nudge")
        # One retry with the nudge appended to the VOLATILE suffix only, leaving
        # the cache_prefix byte-identical so the retry re-hits the cached prefix.
        messages[-1] = {"role": "user", "content": user_content + _JSON_NUDGE}
        resp = self._post(messages, use_response_format=use_rf)
        if resp.status_code >= 400:
            raise AIError(f"HTTP {resp.status_code}: {(resp.text or '')[:200]}")
        content = self._extract(resp)
        try:
            return _parse_json(content)
        except ValueError as e:
            raise AIError(f"unparseable JSON after retry: {e}") from e

    @staticmethod
    def _extract(resp) -> str:
        try:
            data = resp.json()
            return data["choices"][0]["message"]["content"] or ""
        except (ValueError, KeyError, IndexError, TypeError) as e:
            raise AIError(f"unexpected response shape: {e}") from e


class AnthropicProvider(AIProvider):
    """Anthropic Messages API provider (``/v1/messages``).

    Uses ``x-api-key`` + ``anthropic-version: 2023-06-01`` headers and the
    system/messages body shape. Tyler's configured model is
    ``claude-haiku-4-5`` (the GA id, no date suffix).
    """

    def __init__(self, endpoint: str, model: str, api_key: str):
        self.endpoint = (endpoint or "").rstrip("/") or "https://api.anthropic.com"
        self.model = model
        self.api_key = api_key
        # Token usage from the most recent response (Anthropic returns it). Read
        # by diagnostics; never logged automatically.
        self._last_usage = None

    def _post(self, system, user, max_tokens, cache_prefix=None):
        # When a cache_prefix is supplied, build `system` as a LIST of content
        # blocks: the stable instruction text, then the large stable prefix
        # (the catalog) carrying cache_control: ephemeral. This caches
        # system+catalog together with the breakpoint on the catalog block; the
        # volatile instruction stays in the user message so it never breaks the
        # cached prefix. Anthropic accepts a string OR a list for `system`, so
        # the plain-string path (cache_prefix=None) is preserved unchanged.
        # ephemeral cache_control is GA on the standard /v1/messages endpoint —
        # no beta header required.
        if cache_prefix:
            system_field = [
                {"type": "text", "text": system},
                {
                    "type": "text",
                    "text": cache_prefix,
                    "cache_control": {"type": "ephemeral"},
                },
            ]
        else:
            system_field = system
        body = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system_field,
            "messages": [{"role": "user", "content": user}],
        }
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        try:
            resp = requests.post(
                f"{self.endpoint}/v1/messages",
                json=body,
                headers=headers,
                timeout=_TIMEOUT,
            )
        except requests.exceptions.RequestException as e:
            raise AIError(f"transport error: {e}") from e
        if resp.status_code >= 400:
            raise AIError(f"HTTP {resp.status_code}: {(resp.text or '')[:200]}")
        return resp

    def complete_json(self, system: str, user: str, max_tokens: int = 2000,
                      cache_prefix: str | None = None):
        resp = self._post(system, user, max_tokens, cache_prefix=cache_prefix)
        content = self._extract(resp)
        try:
            return _parse_json(content)
        except ValueError:
            logger.debug("Anthropic JSON parse failed; retrying with nudge")
        # Retry: only the volatile user suffix gains the nudge; system +
        # cache_prefix are byte-identical, so the retry re-reads the cache.
        resp = self._post(system, user + _JSON_NUDGE, max_tokens,
                          cache_prefix=cache_prefix)
        content = self._extract(resp)
        try:
            return _parse_json(content)
        except ValueError as e:
            raise AIError(f"unparseable JSON after retry: {e}") from e

    def _extract(self, resp) -> str:
        try:
            data = resp.json()
            self._last_usage = data.get("usage")
            return data["content"][0]["text"] or ""
        except (ValueError, KeyError, IndexError, TypeError) as e:
            raise AIError(f"unexpected response shape: {e}") from e


def make_provider(provider: str, endpoint: str, model: str, api_key: str):
    """Build an :class:`AIProvider`, or ``None`` when AI is disabled.

    Returns ``None`` for ``provider == "none"`` (or any unrecognised provider)
    and whenever ``api_key`` is empty — both mean "no AI", so every caller can
    treat a ``None`` factory result as the signal to use its local fallback.
    """
    if not api_key or provider in (None, "", "none"):
        return None
    if provider == "openai":
        return OpenAIProvider(endpoint, model, api_key)
    if provider == "anthropic":
        return AnthropicProvider(endpoint, model, api_key)
    return None
