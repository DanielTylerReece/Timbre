# discord_rpc.py
#
# Copyright (C) 2026 Tyler Reece
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Minimal Discord Rich Presence over the local IPC socket — no dependencies.

Pure-Python (no ``gi``, no ``pypresence``) implementation of just enough of the
Discord IPC protocol to publish a now-playing activity. Tyler's distro install
must stay pacman-clean, so this talks the wire protocol directly.

Wire protocol (Discord IPC, local unix socket)
-----------------------------------------------
Each frame is ``<op:int32-LE><length:int32-LE><json-utf8>``.

* op 0  HANDSHAKE   ``{"v": 1, "client_id": "<app id>"}``
* op 1  FRAME       a command, e.g. SET_ACTIVITY:
                    ``{"cmd": "SET_ACTIVITY",
                       "args": {"pid": <pid>, "activity": {...} | null},
                       "nonce": "<uuid>"}``
* op 2  CLOSE       ``{}`` — politely closes the connection.

The socket lives at one of:
  * ``$XDG_RUNTIME_DIR/discord-ipc-{0..9}``                       (native)
  * ``$XDG_RUNTIME_DIR/app/com.discordapp.Discord/discord-ipc-*`` (flatpak)
  * ``$XDG_RUNTIME_DIR/snap.discord/discord-ipc-*``               (snap)

Design contract
---------------
* **Fail-silent.** No Discord socket -> disabled quietly. A live socket that
  dies -> we drop it and lazily reconnect on the *next* update (never spin).
* **All socket IO runs on a private worker thread** so the player never blocks.
  The public API (:meth:`update`, :meth:`clear`, :meth:`close`) only enqueues.
* **Coalesced.** The caller drives ``update`` on song change / play-pause and a
  periodic resync; the worker only re-sends SET_ACTIVITY when the payload
  actually changed, so a 15s resync with no real change is a no-op on the wire.

The protocol-shaping helpers (:func:`encode_frame`, :func:`decode_frame`,
:func:`build_activity`, :func:`handshake_payload`, :func:`set_activity_payload`)
are pure and importable without a socket, so they unit-test cleanly.
"""

import json
import logging
import os
import socket
import struct
import threading
import time
import uuid

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Discord application ("client") id.                                          #
# --------------------------------------------------------------------------- #
# Discord REQUIRES a real application id for Rich Presence — there is no
# anonymous/auth-free id. This default is a placeholder: if it is not a valid
# registered application, Discord will reject the handshake and we disable
# ourselves silently (exactly the fail-silent path), so a wrong value here never
# breaks playback. Tyler can register his own in ~2 minutes at
# https://discord.com/developers/applications (New Application -> copy the
# "Application ID") and drop it in here (or set the TIMBRE_DISCORD_CLIENT_ID env
# var, which overrides the constant).
DISCORD_CLIENT_ID = "1234567890123456789"

# Frame header: two little-endian unsigned 32-bit ints (op, length).
_HEADER = struct.Struct("<II")

# IPC opcodes.
OP_HANDSHAKE = 0
OP_FRAME = 1
OP_CLOSE = 2
OP_PING = 3
OP_PONG = 4

# Cap a single frame's JSON payload (defensive; Discord frames are tiny).
_MAX_FRAME_BYTES = 64 * 1024


def client_id() -> str:
    """The effective Discord application id (env override wins)."""
    return os.environ.get("TIMBRE_DISCORD_CLIENT_ID") or DISCORD_CLIENT_ID


# --------------------------------------------------------------------------- #
# Pure protocol helpers (no socket — unit-testable).                          #
# --------------------------------------------------------------------------- #


def encode_frame(op: int, payload: dict) -> bytes:
    """Encode one IPC frame: ``<op:i32-LE><len:i32-LE><json>``."""
    data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return _HEADER.pack(op, len(data)) + data


def decode_frame(buf: bytes):
    """Decode one IPC frame from ``buf``.

    Returns ``(op, payload_dict, consumed_bytes)``. Raises ``ValueError`` if the
    buffer does not yet hold a complete frame (header + declared length) or the
    payload is not valid JSON — callers treat any error as a dead socket.
    """
    if len(buf) < _HEADER.size:
        raise ValueError("short header")
    op, length = _HEADER.unpack_from(buf, 0)
    if length > _MAX_FRAME_BYTES:
        raise ValueError("frame too large")
    end = _HEADER.size + length
    if len(buf) < end:
        raise ValueError("incomplete frame body")
    payload = json.loads(buf[_HEADER.size:end].decode("utf-8")) if length else {}
    return op, payload, end


def handshake_payload(cid: str) -> dict:
    """The op-0 HANDSHAKE body."""
    return {"v": 1, "client_id": str(cid)}


def set_activity_payload(pid: int, activity, nonce: str = None) -> dict:
    """The op-1 SET_ACTIVITY command body.

    ``activity`` of ``None`` clears the presence (Discord contract for an empty
    activity).
    """
    return {
        "cmd": "SET_ACTIVITY",
        "args": {"pid": int(pid), "activity": activity},
        "nonce": nonce or str(uuid.uuid4()),
    }


def build_activity(
    track_name,
    artist_name,
    *,
    duration_secs=None,
    position_secs=None,
    now=None,
    large_image=None,
    large_text=None,
) -> dict:
    """Build a Discord activity dict for a now-playing track.

    * ``details`` = track name; ``state`` = artist (Discord renders both lines).
    * When duration + position are known, emit ``timestamps.end`` so Discord
      shows a counting-down "time left" bar. ``start`` is also emitted so the
      elapsed segment is correct.
    * ``large_image`` is only set when a caller passes a reachable https URL
      (see the artwork note in the RPC client); omitted otherwise — Discord
      shows the activity fine with no asset.

    Returns a dict with empty/None fields pruned so two equal logical states
    encode to byte-identical JSON (clean coalescing).
    """
    activity = {}
    # Discord requires non-empty strings; fall back so a frame is never invalid.
    activity["details"] = (track_name or "Unknown track")[:128]
    if artist_name:
        activity["state"] = artist_name[:128]

    if duration_secs and duration_secs > 0:
        base = int(now if now is not None else time.time())
        pos = max(0, int(position_secs or 0))
        pos = min(pos, int(duration_secs))
        start = base - pos
        end = start + int(duration_secs)
        activity["timestamps"] = {"start": start, "end": end}

    if large_image:
        assets = {"large_image": large_image}
        if large_text:
            assets["large_text"] = large_text[:128]
        activity["assets"] = assets

    return activity


# --------------------------------------------------------------------------- #
# Socket discovery.                                                           #
# --------------------------------------------------------------------------- #


def _candidate_socket_paths():
    """Yield candidate Discord IPC socket paths, native + sandboxed."""
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime:
        return
    bases = [
        runtime,
        os.path.join(runtime, "app", "com.discordapp.Discord"),
        os.path.join(runtime, "snap.discord"),
    ]
    for base in bases:
        for i in range(10):
            yield os.path.join(base, f"discord-ipc-{i}")


# --------------------------------------------------------------------------- #
# The threaded RPC client.                                                    #
# --------------------------------------------------------------------------- #

# Command tags posted onto the worker queue.
_CMD_UPDATE = "update"
_CMD_CLEAR = "clear"
_CMD_STOP = object()


class DiscordRPC:
    """Fail-silent Discord Rich Presence client with a private worker thread.

    Lifecycle: construct (idle, no socket touched), then drive with
    :meth:`update` / :meth:`clear`; call :meth:`close` once on shutdown. Every
    public method only enqueues work — all socket IO happens on the worker.
    """

    def __init__(self, cid: str = None, connect=None, clock=time.time):
        """Args:
        cid: Discord application id (defaults to :func:`client_id`).
        connect: injectable ``() -> socket`` factory (tests pass a fake that
            talks to an in-process unix-socket server). Defaults to the real
            socket-discovery connector.
        clock: monotonic-ish wall clock (seconds) for timestamps; injectable.
        """
        self._cid = str(cid or client_id())
        self._connect = connect or self._default_connect
        self._clock = clock

        self._sock = None
        self._handshaked = False
        self._logged_connect = False
        # Last activity dict actually pushed to Discord (for coalescing). The
        # sentinel distinguishes "never sent" from "sent None (cleared)".
        self._last_activity = _CMD_STOP

        self._q = []
        self._cv = threading.Condition()
        self._closed = False
        self._worker = threading.Thread(
            target=self._run, name="timbre-discord-rpc", daemon=True
        )
        self._worker.start()

    # ------------------------------------------------------------------ #
    # Public API (main thread — enqueue only, never blocks on IO).        #
    # ------------------------------------------------------------------ #

    def update(self, track_name, artist_name, duration_secs=None,
               position_secs=None, large_image=None, large_text=None):
        """Publish/refresh the now-playing activity (coalesced on the worker)."""
        activity = build_activity(
            track_name,
            artist_name,
            duration_secs=duration_secs,
            position_secs=position_secs,
            now=self._clock(),
            large_image=large_image,
            large_text=large_text,
        )
        self._post((_CMD_UPDATE, activity))

    def clear(self):
        """Clear the activity (e.g. on stop / toggle-off)."""
        self._post((_CMD_CLEAR, None))

    def close(self):
        """Stop the worker and drop the socket. Idempotent."""
        with self._cv:
            if self._closed:
                return
            self._closed = True
            self._q.append((_CMD_STOP, None))
            self._cv.notify_all()
        self._worker.join(timeout=3)

    # ------------------------------------------------------------------ #
    # Worker.                                                            #
    # ------------------------------------------------------------------ #

    def _post(self, item):
        with self._cv:
            if self._closed:
                return
            # Coalesce: only the latest UPDATE/CLEAR matters, so collapse any
            # pending non-stop command into this one (keeps the worker from
            # replaying stale activity on a backlog).
            self._q = [q for q in self._q if q[0] is _CMD_STOP]
            self._q.append(item)
            self._cv.notify_all()

    def _run(self):
        while True:
            with self._cv:
                while not self._q:
                    self._cv.wait()
                cmd, arg = self._q.pop(0)
            if cmd is _CMD_STOP:
                self._send_close_quietly()
                self._drop()
                return
            try:
                if cmd == _CMD_UPDATE:
                    self._do_set_activity(arg)
                elif cmd == _CMD_CLEAR:
                    self._do_set_activity(None)
            except Exception:
                # Any socket failure -> drop and let the NEXT command reconnect
                # lazily. Never raise out of the worker; never spin.
                logger.debug("discord-rpc: dropping dead socket", exc_info=True)
                self._drop()

    # ------------------------------------------------------------------ #
    # Connection management (worker thread only).                         #
    # ------------------------------------------------------------------ #

    def _default_connect(self):
        """Try each candidate socket path; return a connected socket or None."""
        for path in _candidate_socket_paths():
            if not os.path.exists(path):
                continue
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.settimeout(2.0)
                s.connect(path)
                return s
            except OSError:
                try:
                    s.close()
                except OSError:
                    pass
                continue
        return None

    def _ensure_connected(self) -> bool:
        """Lazily connect + handshake. Returns True if a live, ready socket."""
        if self._sock is not None and self._handshaked:
            return True
        sock = self._connect()
        if sock is None:
            return False
        self._sock = sock
        # HANDSHAKE then read the READY/DISPATCH reply (also catches a rejected
        # client_id: Discord closes the socket -> recv fails -> we drop).
        self._write_frame(OP_HANDSHAKE, handshake_payload(self._cid))
        op, _payload = self._read_frame()
        if op == OP_CLOSE:
            # Discord rejected us (e.g. bad client_id). Disable silently.
            raise ConnectionError("discord rejected handshake")
        self._handshaked = True
        if not self._logged_connect:
            logger.info("discord-rpc: connected (client_id=%s)", self._cid)
            self._logged_connect = True
        else:
            logger.debug("discord-rpc: reconnected")
        return True

    def _do_set_activity(self, activity):
        """Send SET_ACTIVITY, connecting lazily; coalesce identical states."""
        if not self._ensure_connected():
            return
        # Coalesce: skip the wire write if the logical activity is unchanged.
        if activity == self._last_activity:
            return
        payload = set_activity_payload(os.getpid(), activity)
        self._write_frame(OP_FRAME, payload)
        # Read (and discard) the ack; a dead socket surfaces here as an error.
        self._read_frame()
        self._last_activity = activity

    def _send_close_quietly(self):
        if self._sock is None:
            return
        try:
            self._write_frame(OP_CLOSE, {})
        except Exception:
            pass

    def _drop(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None
        self._handshaked = False
        # Force a fresh SET_ACTIVITY after any reconnect.
        self._last_activity = _CMD_STOP

    # ------------------------------------------------------------------ #
    # Framed socket IO (worker thread only).                              #
    # ------------------------------------------------------------------ #

    def _write_frame(self, op: int, payload: dict):
        self._sock.sendall(encode_frame(op, payload))

    def _read_frame(self):
        """Read exactly one frame; returns ``(op, payload)``. Raises on EOF."""
        header = self._recv_exact(_HEADER.size)
        op, length = _HEADER.unpack(header)
        if length > _MAX_FRAME_BYTES:
            raise ValueError("frame too large")
        body = self._recv_exact(length) if length else b""
        payload = json.loads(body.decode("utf-8")) if body else {}
        return op, payload

    def _recv_exact(self, n: int) -> bytes:
        chunks = []
        got = 0
        while got < n:
            chunk = self._sock.recv(n - got)
            if not chunk:
                raise ConnectionError("discord socket closed")
            chunks.append(chunk)
            got += len(chunk)
        return b"".join(chunks)
