# client.py
#
# Copyright (C) 2026 Tyler Reece
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Pure-Python Jellyfin REST client.

No gi/GTK imports — this module is intentionally headless-testable. Targets
Jellyfin 10.9+ (validated against a live 10.11.5 server).

Error contract:
    * An HTTP response with status >= 400 raises ``JellyfinError`` carrying that
      status code.
    * A transport-level failure (DNS, connection refused, timeout, etc. — any
      ``requests.exceptions.RequestException``) raises ``JellyfinNetworkError``
      (a ``JellyfinError`` subclass) with ``status == 0``. Callers catching
      ``JellyfinError`` therefore catch both.
"""

import threading
from dataclasses import dataclass
from urllib.parse import urlencode

import requests


# --------------------------------------------------------------------------- #
# Result / value types (locked interface — later phases import these by name). #
# --------------------------------------------------------------------------- #

@dataclass
class AuthResult:
    token: str
    user_id: str
    server_id: str


@dataclass
class Library:
    id: str
    name: str


@dataclass
class QCState:
    code: str
    secret: str


@dataclass
class LyricLine:
    text: str
    start_ticks: "int | None"


class JellyfinError(Exception):
    """Raised on any Jellyfin HTTP error (status >= 400).

    Attributes:
        status: the HTTP status code (int).
        message: the (truncated) response body / error text.
    """

    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(f"[{status}] {message}")


class JellyfinNetworkError(JellyfinError):
    """Raised on transport-level failures (connection refused, DNS, timeout).

    A subclass of ``JellyfinError`` with ``status == 0`` so callers catching
    ``JellyfinError`` also catch transport failures, while code that cares can
    distinguish "the server answered with an error" from "we never reached the
    server."
    """

    def __init__(self, status: int, message: str):
        super().__init__(status, message)


_TIMEOUT = 15

# Upper bound on a single image fetch. Guards against a misbehaving / hostile
# server (or a wrong endpoint returning an HTML page) blowing up memory.
_MAX_IMAGE_BYTES = 8 * 1024 * 1024  # 8 MiB


class JellyfinClient:
    """Pure-Python Jellyfin REST client.

    Safe to share one JellyfinClient across worker threads; HTTP sessions are
    thread-local (each thread lazily gets its own ``requests.Session``).
    """

    def __init__(self, server_url, device_id, client="Timbre", version="0.1"):
        self.base = server_url.rstrip("/")
        self.device_id = device_id
        self.client = client
        self.version = version

        self.token = None
        self.user_id = None
        self.server_id = None

        self._local = threading.local()

    @property
    def _session(self) -> requests.Session:
        """Return this thread's ``requests.Session``, creating it on first use."""
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            self._local.session = session
        return session

    # ------------------------------------------------------------------ #
    # Core request plumbing.                                             #
    # ------------------------------------------------------------------ #

    def _auth_header(self):
        parts = []
        if self.token:
            parts.append(f'Token="{self.token}"')
        parts.append(f'Client="{self.client}"')
        parts.append(f'Device="{self.device_id}"')
        parts.append(f'DeviceId="{self.device_id}"')
        parts.append(f'Version="{self.version}"')
        return "MediaBrowser " + ", ".join(parts)

    def _req(self, method, path, params=None, json_body=None, timeout=None):
        """Issue a request, applying the module-level error contract.

        Raises ``JellyfinError`` on any HTTP response with status >= 400, and
        ``JellyfinNetworkError`` (status 0) on a transport-level failure (any
        ``requests.exceptions.RequestException``).

        Returns parsed JSON when the response carries a JSON content-type,
        else None (covers 204 No Content and empty bodies).

        ``timeout`` overrides the module default (seconds) for callers with
        interactive latency needs, e.g. the onboarding reachability probe.
        """
        url = self.base + path
        headers = {"Authorization": self._auth_header()}
        try:
            resp = self._session.request(
                method,
                url,
                params=params,
                json=json_body,
                headers=headers,
                timeout=timeout or _TIMEOUT,
            )
        except requests.exceptions.RequestException as e:
            raise JellyfinNetworkError(0, str(e)) from e
        if resp.status_code >= 400:
            raise JellyfinError(resp.status_code, resp.text[:200])
        ctype = resp.headers.get("Content-Type", "")
        if "json" in ctype.lower():
            return resp.json()
        return None

    def _req_raw(self, method, path, params=None) -> bytes:
        """Like ``_req`` but returns the raw response body bytes.

        Applies the same error contract (>=400 -> JellyfinError, transport
        failure -> JellyfinNetworkError). Used for binary image payloads, so it
        also enforces two guards on a 200: the Content-Type must be ``image/*``
        and the body must be <= ``_MAX_IMAGE_BYTES``. A violation raises
        ``JellyfinError`` (status 0) rather than returning a bogus blob.
        """
        url = self.base + path
        headers = {"Authorization": self._auth_header()}
        try:
            resp = self._session.request(
                method, url, params=params, headers=headers, timeout=_TIMEOUT
            )
        except requests.exceptions.RequestException as e:
            raise JellyfinNetworkError(0, str(e)) from e
        if resp.status_code >= 400:
            raise JellyfinError(resp.status_code, resp.text[:200])
        ctype = resp.headers.get("Content-Type", "")
        if not ctype.lower().startswith("image/"):
            raise JellyfinError(
                0, f"expected image/* response, got {ctype!r}"
            )
        content = resp.content
        if len(content) > _MAX_IMAGE_BYTES:
            raise JellyfinError(
                0,
                f"image exceeds {_MAX_IMAGE_BYTES} byte limit "
                f"({len(content)} bytes)",
            )
        return content

    # ------------------------------------------------------------------ #
    # Server discovery (no auth).                                        #
    # ------------------------------------------------------------------ #

    def public_info(self) -> dict:
        """GET /System/Info/Public — server identity, no auth required.

        Used by onboarding to validate a server URL the user typed before any
        credentials exist. Returns the parsed JSON dict (ServerName, Version,
        Id, ...).

        Uses a short 5s timeout: this is a reachability probe driving an
        interactive spinner, so an unreachable host should fail fast rather
        than sit on the default 15s connect timeout.
        """
        return self._req("GET", "/System/Info/Public", timeout=5) or {}

    # ------------------------------------------------------------------ #
    # Authentication.                                                    #
    # ------------------------------------------------------------------ #

    def _apply_auth(self, data) -> AuthResult:
        token = data.get("AccessToken")
        user_id = (data.get("User") or {}).get("Id")
        server_id = data.get("ServerId")
        self.token = token
        self.user_id = user_id
        self.server_id = server_id
        return AuthResult(token=token, user_id=user_id, server_id=server_id)

    def authenticate(self, username, password) -> AuthResult:
        data = self._req(
            "POST",
            "/Users/AuthenticateByName",
            json_body={"Username": username, "Pw": password},
        )
        return self._apply_auth(data)

    def restore(self, token, user_id, server_id) -> bool:
        """Validate a stored token via GET /Users/Me.

        On success sets client state and returns True. A 401 means the token
        is no longer valid -> returns False (no exception). Any other error
        propagates as JellyfinError.
        """
        self.token = token
        self.user_id = user_id
        self.server_id = server_id
        try:
            self._req("GET", "/Users/Me")
        except JellyfinError as e:
            if e.status == 401:
                self.token = None
                self.user_id = None
                self.server_id = None
                return False
            raise
        return True

    # ------------------------------------------------------------------ #
    # Quick Connect.                                                     #
    # ------------------------------------------------------------------ #

    def quick_connect_enabled(self) -> bool:
        return bool(self._req("GET", "/QuickConnect/Enabled"))

    def quick_connect_initiate(self) -> QCState:
        data = self._req("POST", "/QuickConnect/Initiate")
        return QCState(code=data.get("Code"), secret=data.get("Secret"))

    def quick_connect_poll(self, secret) -> bool:
        data = self._req("GET", "/QuickConnect/Connect", params={"secret": secret})
        return bool((data or {}).get("Authenticated"))

    def authenticate_quick_connect(self, secret) -> AuthResult:
        data = self._req(
            "POST",
            "/Users/AuthenticateWithQuickConnect",
            json_body={"Secret": secret},
        )
        return self._apply_auth(data)

    # ------------------------------------------------------------------ #
    # Libraries / items.                                                 #
    # ------------------------------------------------------------------ #

    def music_libraries(self) -> "list[Library]":
        data = self._req("GET", "/UserViews", params={"userId": self.user_id})
        out = []
        for item in (data or {}).get("Items", []):
            if item.get("CollectionType") == "music":
                out.append(Library(id=item.get("Id"), name=item.get("Name")))
        return out

    def items_page(
        self,
        parent_id,
        item_types,
        start,
        limit=500,
        fields="Genres,DateCreated,MediaSources,ParentId",
    ) -> "tuple[list[dict], int]":
        params = {
            "parentId": parent_id,
            "includeItemTypes": item_types,
            "recursive": "true",
            "startIndex": start,
            "limit": limit,
            "fields": fields,
            "userId": self.user_id,
        }
        data = self._req("GET", "/Items", params=params)
        items = (data or {}).get("Items", [])
        total = (data or {}).get("TotalRecordCount", 0)
        return items, total

    def artists(self, parent_id) -> "list[dict]":
        # Request the Overview field so server-side artist bios (from metadata
        # providers) come back on each item; the sync layer maps these into the
        # local artists.bio (bio_source='jellyfin') so Jellyfin's own bios are
        # respected over AI-generated ones.
        params = {
            "parentId": parent_id,
            "userId": self.user_id,
            "fields": "Overview",
        }
        data = self._req("GET", "/Artists", params=params)
        return (data or {}).get("Items", [])

    def genres(self, parent_id) -> "list[dict]":
        """Return the music genres under a library (GET /MusicGenres)."""
        params = {"parentId": parent_id, "userId": self.user_id}
        data = self._req("GET", "/MusicGenres", params=params)
        return (data or {}).get("Items", [])

    # ------------------------------------------------------------------ #
    # Lyrics / instant mix.                                              #
    # ------------------------------------------------------------------ #

    def lyrics(self, item_id) -> "list[LyricLine]":
        try:
            data = self._req("GET", f"/Audio/{item_id}/Lyrics")
        except JellyfinError as e:
            if e.status == 404:
                return []
            raise
        out = []
        for line in (data or {}).get("Lyrics", []):
            out.append(
                LyricLine(
                    text=line.get("Text", ""),
                    start_ticks=line.get("Start"),
                )
            )
        return out

    def instant_mix(self, item_id, limit=50) -> "list[dict]":
        params = {"userId": self.user_id, "limit": limit}
        data = self._req("GET", f"/Items/{item_id}/InstantMix", params=params)
        return (data or {}).get("Items", [])

    # ------------------------------------------------------------------ #
    # Metadata edit (admin).                                             #
    # ------------------------------------------------------------------ #

    def get_item(self, item_id) -> "dict | None":
        """Fetch a single item's FULL BaseItemDto (GET /Items/{itemId}).

        Returns the parsed dict, or None when the body is empty. Raises
        ``JellyfinError`` on HTTP error. Used by :meth:`update_overview` for
        the read half of a read-modify-write metadata edit, so the POST back
        carries every field unchanged.
        """
        return self._req("GET", f"/Items/{item_id}")

    def update_overview(self, item_id, text) -> None:
        """Write ``text`` into an item's Overview (server-side bio).

        Jellyfin's item-update endpoint REPLACES the item metadata with the
        posted DTO — posting a sparse body would wipe every other field — so
        this is a GET-modify-POST cycle: fetch the full item via
        :meth:`get_item`, set Overview=text on that DTO, then POST the FULL
        item JSON back to ``/Items/{itemId}``. A 204 No Content is expected.

        Raises ``JellyfinError`` on failure. Non-admin users typically get a
        403 here (they lack the metadata-edit permission); callers handle that.
        """
        item = self.get_item(item_id) or {}
        item["Overview"] = text
        self._req("POST", f"/Items/{item_id}", json_body=item)

    # ------------------------------------------------------------------ #
    # Favorites / playback reporting.                                   #
    # ------------------------------------------------------------------ #

    def set_favorite(self, item_id, fav: bool) -> None:
        method = "POST" if fav else "DELETE"
        self._req(method, f"/UserFavoriteItems/{item_id}")

    def report_start(self, track_id) -> None:
        self._req(
            "POST",
            "/Sessions/Playing",
            json_body={"ItemId": track_id, "PositionTicks": 0},
        )

    def report_progress(self, track_id, position_ticks, paused: bool) -> None:
        self._req(
            "POST",
            "/Sessions/Playing/Progress",
            json_body={
                "ItemId": track_id,
                "PositionTicks": position_ticks,
                "IsPaused": paused,
            },
        )

    def report_stop(self, track_id, position_ticks) -> None:
        self._req(
            "POST",
            "/Sessions/Playing/Stopped",
            json_body={"ItemId": track_id, "PositionTicks": position_ticks},
        )

    # ------------------------------------------------------------------ #
    # URL builders (pure — no request issued).                           #
    # ------------------------------------------------------------------ #

    def stream_url(self, track_id, max_bitrate=None) -> str:
        params = {
            "userId": self.user_id,
            "deviceId": self.device_id,
            "api_key": self.token,
            "container": "opus,mp3,aac,m4a|aac,flac,webma,webm|webma,wav,ogg",
            "transcodingContainer": "mp4",
            "transcodingProtocol": "hls",
            "audioCodec": "aac",
        }
        if max_bitrate:
            params["maxStreamingBitrate"] = max_bitrate
        return f"{self.base}/Audio/{track_id}/universal?{urlencode(params)}"

    def image_url(self, item_id, tag, max_width) -> str:
        # NOTE: this builds an UNAUTHENTICATED image URL (no api_key / token).
        # It works for servers that allow anonymous image access and is used
        # for the MPRIS "mpris:artUrl" field, which must be a plain URL a
        # third-party MPRIS consumer can fetch. On servers that require auth
        # for images, MPRIS art may not render — that is an accepted tradeoff.
        # In-app image loading goes through fetch_image_bytes() instead, which
        # sends the MediaBrowser auth header.
        params = {"maxWidth": max_width, "quality": 90}
        if tag:
            params["tag"] = tag
        return f"{self.base}/Items/{item_id}/Images/Primary?{urlencode(params)}"

    # ------------------------------------------------------------------ #
    # Authenticated image fetch (sends the MediaBrowser auth header).     #
    # ------------------------------------------------------------------ #

    def fetch_image_bytes(self, item_id, tag, max_width) -> bytes:
        """Fetch a primary image's bytes WITH the MediaBrowser auth header.

        Unlike ``image_url`` (a pure URL builder used for MPRIS art), this
        issues an authenticated GET so it works on servers that require auth
        for image access. Raises ``JellyfinError`` on HTTP error (e.g. 404 no
        image) and ``JellyfinNetworkError`` on transport failure.
        """
        params = {"maxWidth": max_width, "quality": 90}
        if tag:
            params["tag"] = tag
        return self._req_raw(
            "GET", f"/Items/{item_id}/Images/Primary", params=params
        )
