# player_object.py
#
# Copyright 2023 Nokse
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

"""GStreamer player for Timbre.

Ported from High Tide's ``player_object.py`` (playbin3, gapless about-to-finish,
ReplayGain, audio-sink selection). The TIDAL-era stream machinery (manifest
parsing, ffmpeg/requests caching) has been removed: a Jellyfin stream URL is a
plain HTTP URI built locally by ``client.stream_url(track.id, max_bitrate)`` and
handed straight to playbin.

Architecture:

* **Queue logic lives in :class:`~play_queue.PlayQueue`** (pure python, no gi).
  PlayerObject delegates every queue/shuffle/repeat decision to it.
* **Gapless prefetch** is the headline feature. Because ``stream_url`` is a pure
  local URL builder (no network), "resolve the next track before about-to-finish
  fires" is trivially satisfied: when a track starts we precompute the next URI
  (via ``PlayQueue.peek_next``) and stash it in ``self._prefetched_uri``. The
  ``about-to-finish`` handler does nothing but
  ``playbin.set_property("uri", prefetched_uri)``. The prefetch is recomputed on
  queue/shuffle/repeat changes. PipeWire's gapless exclusion (from upstream) is
  preserved.
* **Reporting + history** go through :class:`~playback_reporter.PlaybackReporter`
  (its own worker thread). They only fire when ``client`` / ``db`` are present,
  so the Phase 0 shell runs fine with all-None deps.
* **Background audio**: the ``playing`` GObject property is bound by the app
  (Phase 4) to ``Gtk.Application.hold()/release()`` so playback survives the
  window closing.
"""

import logging
from enum import IntEnum
from gettext import gettext as _
from typing import Any, List, Optional

from gi.repository import GLib, GObject, Gst

from . import utils
from .play_queue import (  # noqa: F401  (RepeatType + codec re-export)
    PlayQueue,
    RepeatType,
    deserialize_player_state,
    serialize_player_state,
)
from .playback_reporter import PlaybackReporter
from .jellyfin.models import normalize_tracks

logger = logging.getLogger(__name__)

# Minimum seconds between Discord presence resyncs while a track plays (no new
# timer — piggybacks the 1s slider tick and rate-limits here). The presence end
# timestamp drifts only on seek/pause, so a coarse resync is plenty.
_DISCORD_RESYNC_INTERVAL = 15.0

# Key in the SQLite ``meta`` table holding the resume-state JSON blob. The
# queue can be thousands of ids, so gschema (dconf) is the wrong store; the
# db meta key/value store keeps it next to the library it references.
PLAYER_STATE_KEY = "player-state"

# Minimum seconds between coarse position persists during playback. The persist
# piggybacks the existing 1s slider tick (no new timer) and rate-limits here.
_PERSIST_INTERVAL = 12.0


class AudioSink(IntEnum):
    AUTO = 0
    PULSE = 1
    ALSA = 2
    JACK = 3
    OSS = 4
    PIPEWIRE = 5


# Ticks are 100ns units (Jellyfin convention). 1 second = 1e7 ticks.
# GStreamer nanoseconds → ticks: ns / 100.
_NS_PER_TICK = 100


def _track_duration_ticks(track) -> int:
    """Best-effort duration in 100ns ticks for a track-like object."""
    return getattr(track, "duration_ticks", None) or 0


class PlayerObject(GObject.GObject):
    """Handles player playback, delegating queue decisions to PlayQueue."""

    current_song_index = GObject.Property(type=int, default=-1)
    can_go_next = GObject.Property(type=bool, default=True)
    can_go_prev = GObject.Property(type=bool, default=True)

    __gsignals__ = {
        "songs-list-changed": (GObject.SignalFlags.RUN_FIRST, None, (int,)),
        "update-slider": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "song-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "song-added-to-queue": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "duration-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "volume-changed": (GObject.SignalFlags.RUN_FIRST, None, (float,)),
        "buffering": (GObject.SignalFlags.RUN_FIRST, None, (int,)),
    }

    def __init__(
        self,
        preferred_sink: AudioSink = AudioSink.AUTO,
        alsa_device: str = "default",
        normalize: bool = False,
        quadratic_volume: bool = False,
        client=None,
        db=None,
        settings=None,
    ) -> None:
        GObject.GObject.__init__(self)

        Gst.init(None)
        logger.info("GStreamer version: %s", Gst.version_string())

        # Injected dependencies — all optional so the Phase 0 shell works.
        self._client = client
        self._db = db
        self._settings = settings
        self._max_bitrate = None
        if settings is not None:
            try:
                self._max_bitrate = settings.get_int("max-bitrate") or None
            except Exception:
                self._max_bitrate = None

        self._reporter = PlaybackReporter(client, db)

        # Discord Rich Presence — lazily constructed on first use, gated by the
        # ``discord-rpc`` setting (read live). All socket IO is off-main inside
        # the client; fail-silent throughout. ``_discord_last_resync`` rate-
        # limits the periodic position resync piggybacked on the slider tick.
        self._discord = None
        self._discord_last_resync = None
        if settings is not None:
            try:
                settings.connect(
                    "changed::discord-rpc", self._on_discord_setting_changed
                )
            except Exception:
                logger.debug("could not connect discord-rpc settings signal",
                             exc_info=True)

        self.pipeline = Gst.Pipeline.new("timbre-player")

        self.playbin = Gst.ElementFactory.make("playbin3", "playbin")
        if self.playbin:
            self.playbin.connect("about-to-finish", self._on_about_to_finish)
            self.gapless_enabled = True
        else:
            logger.error("Could not create playbin3, falling back to playbin...")
            self.playbin = Gst.ElementFactory.make("playbin", "playbin")
            self.gapless_enabled = False

        if preferred_sink == AudioSink.PIPEWIRE:
            self.gapless_enabled = False

        self.use_about_to_finish = True
        self.pipeline.add(self.playbin)

        self.normalize = normalize
        self.quadratic_volume = quadratic_volume
        self.most_recent_rg_tags = ""

        self.alsa_device: str = alsa_device
        self._setup_audio_sink(preferred_sink)

        # Message bus
        self._bus = self.pipeline.get_bus()
        self._bus.add_signal_watch()
        self._bus.connect("message::eos", self._on_bus_eos)
        self._bus.connect("message::error", self._on_bus_error)
        self._bus.connect("message::buffering", self._on_buffering_message)
        self._bus.connect("message::stream-start", self._on_track_start)

        # State
        self._playing = False
        self.queue = PlayQueue()
        self.duration = 0
        self.update_timer: Any | None = None
        self.seek_after_sink_reload: Optional[float] = None
        self.seeked_to_end = False

        # Gapless prefetch: the next track and its pre-resolved stream URI.
        self._prefetched_track = None
        self._prefetched_uri = None

        # Track currently reported as started, so a track change emits stop
        # for the previous one exactly once.
        self._reported_track_id = None

        # Resume-state persistence: coarse cadence guard (monotonic seconds of
        # the last successful persist) so the 1s slider tick only writes every
        # ``_PERSIST_INTERVAL`` seconds during playback.
        self._last_state_persist_t = None
        # When restoring at launch we preroll PAUSED then seek to this absolute
        # position (seconds) on the resulting stream-start; mirrors the
        # ``seek_after_sink_reload`` (fraction) mechanism but for an absolute
        # restore position, and never auto-plays.
        self._restore_seek_secs: Optional[float] = None

    # ------------------------------------------------------------------ #
    # GObject properties                                                 #
    # ------------------------------------------------------------------ #

    @GObject.Property(type=bool, default=False)
    def playing(self) -> bool:
        return self._playing

    @playing.setter
    def playing(self, value: bool) -> None:
        self._playing = value
        self.notify("playing")

    @GObject.Property(type=bool, default=False)
    def shuffle(self) -> bool:
        return self.queue.shuffle

    @shuffle.setter
    def shuffle(self, value: bool) -> None:
        if self.queue.shuffle == value:
            return
        self.queue.shuffle = value
        self.notify("shuffle")
        self._update_prefetch()

    @GObject.Property(type=int, default=0)
    def repeat_type(self) -> int:
        return int(self.queue.repeat_type)

    @repeat_type.setter
    def repeat_type(self, value) -> None:
        self.queue.repeat_type = RepeatType(value)
        self.notify("repeat-type")
        self._update_prefetch()

    @property
    def playing_track(self):
        """The track currently loaded/playing (delegated to PlayQueue)."""
        return self.queue.current

    # ------------------------------------------------------------------ #
    # Audio sink                                                         #
    # ------------------------------------------------------------------ #

    def _setup_audio_sink(self, sink_type: AudioSink) -> None:
        """Configure the audio sink via a parsed bin description."""
        sink_map = {
            AudioSink.AUTO: "autoaudiosink",
            AudioSink.PULSE: "pulsesink",
            AudioSink.ALSA: f"alsasink device={self.alsa_device}",
            AudioSink.JACK: "jackaudiosink",
            AudioSink.OSS: "osssink",
            AudioSink.PIPEWIRE: "pipewiresink",
        }
        sink_name = sink_map.get(sink_type, "autoaudiosink")

        normalization = ""
        if self.normalize:
            normalization = (
                f"taginject name=rgtags {self.most_recent_rg_tags} ! "
                f"rgvolume name=rgvol pre-amp=4.0 fallback-gain=-10 headroom=6.0 ! "
                f"rglimiter ! audioconvert !"
            )

        pipeline_str = (
            f"queue ! audioconvert ! {normalization} audioresample ! {sink_name}"
        )

        # PipeWire's sink does not support playbin3 gapless cleanly upstream.
        self.gapless_enabled = sink_type != AudioSink.PIPEWIRE

        try:
            audio_bin = Gst.parse_bin_from_description(pipeline_str, True)
            if not audio_bin:
                raise RuntimeError("Failed to create audio bin")
            self.playbin.set_property("audio-sink", audio_bin)
        except GLib.Error:
            logger.exception("Error creating audio sink pipeline")
            self.playbin.set_property(
                "audio-sink", Gst.ElementFactory.make("autoaudiosink", None)
            )

    def change_audio_sink(self, sink_type: AudioSink) -> None:
        """Change the audio sink while preserving playback state."""
        self.use_about_to_finish = False
        was_playing: bool = self.playing
        position: int = self.query_position()
        duration: int = self.query_duration()
        replay = self.playing_track

        self.pipeline.set_state(Gst.State.NULL)
        self._setup_audio_sink(sink_type)

        if replay is not None:
            self._load_track_uri(replay, gapless=False, resume=was_playing)
            if was_playing and duration:
                self.seek_after_sink_reload = position / duration
        self.use_about_to_finish = True

    # ------------------------------------------------------------------ #
    # Bus handlers                                                       #
    # ------------------------------------------------------------------ #

    def _on_bus_eos(self, *args) -> None:
        """End of stream. With gapless on, playbin handles the handoff; without,
        advance manually. If nothing follows, stop.

        Gapless-on EOS contract: when gapless is enabled, ``about-to-finish``
        has already handed playbin the next URI, so EOS only ever fires here at
        the *true* end of the queue (``peek_next() is None``). The
        ``_report_stop`` below is the single stop report for that final track.
        The double-stop case is guarded: ``_report_stop`` is idempotent — it
        clears ``_reported_track_id`` and no-ops on a second call — so even if
        EOS and another stop path (e.g. an explicit stop()) overlap, the track
        is reported stopped exactly once.
        """
        if self.queue.peek_next() is None:
            self.pause()
            self._report_stop()
        if not self.gapless_enabled:
            GLib.idle_add(self.play_next, False)

    def _on_bus_error(self, bus: Any, message: Any) -> None:
        err, debug = message.parse_error()
        logger.error("GStreamer error: %s", err.message)
        logger.error("Debug info: %s", debug)

        if "Internal data stream error" in err.message and "not-linked" in debug:
            logger.error("Stream not linked; attempting pipeline restart")
            track = self.playing_track
            if track is not None:
                self._load_track_uri(track, gapless=False, resume=self.playing)
        elif (
            "Error outputting to audio device" in err.message
            and "disconnected" in err.message
        ):
            utils.send_toast(_("Audio device is not available"), 5)
            self.pause()
            self.pipeline.set_state(Gst.State.NULL)

    def _on_buffering_message(self, bus: Any, message: Any) -> None:
        buffer_per: int = message.parse_buffering()
        self.emit("buffering", buffer_per)

    def _on_track_start(self, bus: Any, message: Any):
        """A new track started playing on the pipeline."""
        track = self.playing_track
        if track is None:
            return

        # Restore preroll: this stream-start is the PAUSED preroll of a
        # restored track at launch, not a real play. Seek to the stored
        # position and emit the UI-facing signals (so the pane/MPRIS paint the
        # restored track), but do NOT report a play / start the play-tick timer
        # — restore must not count as a listen or autoplay.
        restoring = self._restore_seek_secs is not None

        self.apply_replaygain_tags()
        self._refresh_can_go()
        self.duration = self.query_duration() or (
            _track_duration_ticks(track) * 100  # ticks → ns
        )
        self.emit("song-changed")
        self.emit("duration-changed")

        if restoring:
            target = self._restore_seek_secs
            self._restore_seek_secs = None
            self.playbin.seek_simple(
                Gst.Format.TIME,
                Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
                int(target * Gst.SECOND),
            )
            # Precompute the gapless successor and paint the slider; stay paused.
            self._update_prefetch()
            self.emit("update-slider")
            self.seeked_to_end = False
            return

        # Reporting + local history (worker thread; no-op without deps).
        self._report_start(track)

        # Precompute the next stream URI for gapless.
        self._update_prefetch()

        if self.update_timer:
            GLib.source_remove(self.update_timer)
        self.update_timer = GLib.timeout_add(1000, self._update_slider_callback)

        self.seeked_to_end = False
        if self.seek_after_sink_reload is not None:
            self.seek(self.seek_after_sink_reload)
            self.seek_after_sink_reload = None

    # ------------------------------------------------------------------ #
    # Reporting helpers                                                  #
    # ------------------------------------------------------------------ #

    def _report_start(self, track):
        track_id = getattr(track, "id", None)
        if track_id is None:
            return
        # Emit a stop for any previously started track that wasn't stopped.
        if self._reported_track_id and self._reported_track_id != track_id:
            self._report_stop()
        self._reported_track_id = track_id
        self._reporter.on_start(track_id)
        # Mirror the reporter: publish the new track to Discord (song change).
        self._discord_update()

    def _report_progress(self, paused, force=False):
        if self._reported_track_id is None:
            return
        self._reporter.on_tick(self.position_ticks(), paused, force=force)
        # Mirror the reporter's tick: on a forced report (seek / play-pause
        # flip) push presence now; otherwise coarse-resync the end timestamp.
        if force:
            self._discord_update()
        else:
            self._discord_resync()

    def _report_stop(self):
        if self._reported_track_id is None:
            return
        self._reporter.on_stop(self.position_ticks())
        self._reported_track_id = None
        # Mirror the reporter: a stop clears the Discord presence too.
        self._discord_clear()

    # ------------------------------------------------------------------ #
    # Discord Rich Presence (mirrors the reporter drive points)          #
    # ------------------------------------------------------------------ #

    def _discord_enabled(self) -> bool:
        """True iff the discord-rpc setting is on (settings may be None)."""
        if self._settings is None:
            return False
        try:
            return self._settings.get_boolean("discord-rpc")
        except Exception:
            return False

    def _ensure_discord(self):
        """Lazily build the RPC client when first needed. Returns it or None."""
        if self._discord is None:
            try:
                from .discord_rpc import DiscordRPC

                self._discord = DiscordRPC()
            except Exception:
                logger.debug("could not create Discord RPC client",
                             exc_info=True)
                self._discord = None
        return self._discord

    def _discord_large_image(self) -> Optional[str]:
        """A reachable artwork URL for Discord, or None.

        Discord's ``large_image`` accepts an https URL, but Tyler's Jellyfin
        server is a LAN box that is usually NOT publicly reachable and the
        primary-image URL carries an api_key — so we only offer artwork when the
        server URL is https (a reasonable proxy for "reachable from Discord's
        CDN fetcher"); otherwise we omit it and Discord shows the activity with
        no asset. Conservative by design: a wrong URL just yields no art.
        """
        client = self._client
        track = self.playing_track
        if client is None or track is None:
            return None
        base = getattr(client, "base", "") or ""
        if not base.startswith("https://"):
            return None
        track_id = getattr(track, "id", None)
        if not track_id:
            return None
        return f"{base}/Items/{track_id}/Images/Primary"

    def _discord_update(self):
        """Push the current track to Discord if enabled (fail-silent)."""
        if not self._discord_enabled():
            return
        track = self.playing_track
        if track is None:
            return
        rpc = self._ensure_discord()
        if rpc is None:
            return
        duration_secs = (self.duration / Gst.SECOND) if self.duration else None
        if not duration_secs:
            ticks = _track_duration_ticks(track)
            duration_secs = (ticks / 1e7) if ticks else None
        try:
            rpc.update(
                getattr(track, "name", None),
                getattr(track, "artist_name", None),
                duration_secs=duration_secs,
                position_secs=self._current_position_secs(),
                large_image=self._discord_large_image(),
                large_text=getattr(track, "album_name", None),
            )
        except Exception:
            logger.debug("discord update failed", exc_info=True)
        self._discord_last_resync = GLib.get_monotonic_time() / 1_000_000.0

    def _discord_resync(self):
        """Coarse periodic resync of the presence (rate-limited)."""
        if not self._discord_enabled() or self._discord is None:
            return
        now = GLib.get_monotonic_time() / 1_000_000.0
        if (
            self._discord_last_resync is not None
            and (now - self._discord_last_resync) < _DISCORD_RESYNC_INTERVAL
        ):
            return
        self._discord_update()

    def _discord_clear(self):
        """Clear the presence if a client exists (no-op otherwise)."""
        if self._discord is None:
            return
        try:
            self._discord.clear()
        except Exception:
            logger.debug("discord clear failed", exc_info=True)
        self._discord_last_resync = None

    def _on_discord_setting_changed(self, *args):
        """React live to the discord-rpc toggle.

        OFF -> clear the presence immediately. ON -> if a track is playing,
        push it right away so the user sees presence appear without waiting for
        the next track change.
        """
        if self._discord_enabled():
            if self._reported_track_id is not None:
                self._discord_update()
        else:
            self._discord_clear()

    def position_ticks(self) -> int:
        """Current playback position in 100ns ticks (Jellyfin units).

        ``query_position`` can return -1 even when it reports success (no
        position available yet), so clamp at 0 to avoid negative ticks leaking
        into playback reports.
        """
        return max(0, self.query_position() // _NS_PER_TICK)

    # ------------------------------------------------------------------ #
    # Stream resolution + loading                                        #
    # ------------------------------------------------------------------ #

    def _resolve_uri(self, track) -> Optional[str]:
        """Build the stream URI for a track (pure, local, no network)."""
        track_id = getattr(track, "id", None)
        if track_id is None or self._client is None:
            return None
        try:
            return self._client.stream_url(track_id, self._max_bitrate)
        except Exception:
            logger.exception("Failed to build stream URL for %s", track_id)
            return None

    def _load_track_uri(self, track, gapless: bool, resume: bool) -> None:
        """Point playbin at a track's stream URI and (re)start the pipeline."""
        uri = self._resolve_uri(track)
        if uri is None:
            logger.warning("No stream URI for track; nothing to play")
            return
        if not gapless:
            self.use_about_to_finish = False
            self.pipeline.set_state(Gst.State.NULL)
        self.playbin.set_property("uri", uri)
        logger.info("Loaded stream URI: %s", uri)
        if not gapless and resume:
            self.play()
        if not gapless:
            self.use_about_to_finish = True

    def _update_prefetch(self):
        """Recompute the next track + its stream URI for gapless handoff."""
        nxt = self.queue.peek_next()
        self._prefetched_track = nxt
        self._prefetched_uri = self._resolve_uri(nxt) if nxt is not None else None

    def _on_about_to_finish(self, playbin: Any):
        """playbin is nearly done with the current track — hand it the next URI.

        Runs on the GStreamer **streaming thread**, not the GLib main loop.
        We therefore re-derive the handoff under :class:`PlayQueue`'s lock
        (its public methods are thread-safe) instead of trusting the stashed
        ``_prefetched_uri``, which can be stale after a queue edit on the main
        thread. ``queue.next(user=False)`` atomically advances to the gapless
        successor and returns it; :meth:`_resolve_uri` is a pure local URL
        build (no network), safe to call here. The ``set_property("uri", …)``
        must stay synchronous in this callback — that is the gapless contract.

        We do not emit GObject signals or call the reporter here: those are
        UI-/main-loop-facing and are driven instead by the ``stream-start``
        bus message (``_on_track_start``), which the signal-watch dispatches
        on the main loop.
        """
        if not (self.gapless_enabled and self.use_about_to_finish):
            logger.info("Gapless disabled; ignoring about-to-finish")
            return

        # Re-derive under the queue's lock rather than trusting the stash.
        track = self.queue.next(user=False)
        uri = self._resolve_uri(track) if track is not None else None
        if uri is None:
            # Fall back to the prefetched URI only if the fresh derivation
            # produced nothing (e.g. transient client issue); otherwise stop.
            uri = self._prefetched_uri
            if uri is None:
                logger.info("No next track; nothing to gapless into")
                return

        # Synchronous property set — required for gapless handoff.
        self.playbin.set_property("uri", uri)
        logger.info("Gapless handoff to %s", uri)

    # ------------------------------------------------------------------ #
    # Playback control                                                   #
    # ------------------------------------------------------------------ #

    def play_this(self, thing, index: int = 0) -> None:
        """Play a list of tracks (or a single track) starting at ``index``."""
        tracks = self.get_track_list(thing)
        if not tracks:
            logger.info("No tracks found to play")
            return
        self.queue.set_tracks(tracks, index)
        track = self.queue.current
        if track is None:
            return
        self.playing = True
        self._load_track_uri(track, gapless=False, resume=True)

    def shuffle_this(self, thing) -> None:
        """Play a collection with shuffle enabled."""
        tracks = self.get_track_list(thing)
        if not tracks:
            return
        self.queue.set_tracks(tracks, 0)
        self.shuffle = True
        track = self.queue.current
        if track is not None:
            self.playing = True
            self._load_track_uri(track, gapless=False, resume=True)

    def get_track_list(self, thing) -> List[Any]:
        """Normalise ``thing`` into a list of track objects.

        Accepts a list of tracks, a single track-like object, or any object
        exposing a ``tracks()``/``items()`` method (e.g. an album/playlist
        wrapper). Phase 1 ``Track`` dataclasses are passed through directly.
        """
        if isinstance(thing, list):
            return normalize_tracks(thing)
        for attr in ("items", "tracks", "top_tracks"):
            method = getattr(thing, attr, None)
            if callable(method):
                result = method()
                return normalize_tracks(list(result)) if result else []
        # A single track-like object (or db row dict).
        return normalize_tracks([thing])

    def play(self) -> None:
        """Start/resume playback."""
        self.playing = True
        self.pipeline.set_state(Gst.State.PLAYING)
        self._report_progress(paused=False)
        # Play/pause is a presence event: refresh so Discord's timestamp tracks
        # the resumed position (only does wire IO when actually enabled).
        self._discord_update()
        if self.update_timer:
            GLib.source_remove(self.update_timer)
        self.update_timer = GLib.timeout_add(1000, self._update_slider_callback)

    def pause(self) -> None:
        """Pause playback."""
        self.playing = False
        self.pipeline.set_state(Gst.State.PAUSED)
        self._report_progress(paused=True)
        # Mirror play(): refresh presence so the (now frozen) position is shown.
        self._discord_update()
        # Pause is a natural checkpoint: persist resume state immediately so a
        # crash/kill while paused doesn't lose the position.
        self._persist_state(force=True)

    def play_pause(self) -> None:
        if self.playing:
            self.pause()
        else:
            self.play()

    def play_next(self, user: bool = True) -> None:
        """Advance to the next track. ``user=True`` is an explicit skip."""
        # Report the finished track stopping before moving on.
        self._report_stop()
        track = self.queue.next(user=user)
        if track is None:
            self.pause()
            return
        self._load_track_uri(track, gapless=False, resume=self.playing)

    def load_at_index(self, index: int) -> None:
        """Jump to ``index`` in the current queue and start playing it.

        Used by the queue tab when the user activates a row.
        """
        self._report_stop()
        track = self.queue.jump_to(index)
        if track is None:
            return
        self.playing = True
        self._load_track_uri(track, gapless=False, resume=True)

    def play_previous(self) -> None:
        """Restart the current track if past 2s, else step back."""
        if self.query_position() > 2 * Gst.SECOND:
            self.seek(0)
            self._report_progress(paused=not self.playing, force=True)
            self._refresh_can_go()
            return
        self._report_stop()
        track = self.queue.previous()
        if track is None:
            self._refresh_can_go()
            return
        self._load_track_uri(track, gapless=False, resume=self.playing)

    def stop(self) -> None:
        """Stop playback entirely and report the stop."""
        self._report_stop()
        self.pause()
        self.pipeline.set_state(Gst.State.NULL)

    # ------------------------------------------------------------------ #
    # Queue editing (delegated)                                          #
    # ------------------------------------------------------------------ #

    def add_to_queue(self, track):
        tracks = normalize_tracks([track])
        if not tracks:
            return
        self.queue.append(tracks[0])
        self._refresh_can_go()
        self._update_prefetch()
        self.emit("song-added-to-queue")

    def add_next(self, track):
        tracks = normalize_tracks([track])
        if not tracks:
            return
        self.queue.add_next(tracks[0])
        self._refresh_can_go()
        self._update_prefetch()
        self.emit("song-added-to-queue")

    def remove_at(self, index: int) -> None:
        """Remove the track at ``index`` from the queue (queue-tab edit).

        Wraps :meth:`PlayQueue.remove` with the player-side consequences the
        pure queue cannot know about:

        * Removing a track *other* than the current one is a silent queue edit:
          recompute prefetch (the gapless successor may have changed) and emit
          ``songs-list-changed`` so the queue tab / MPRIS refresh.
        * Removing the **current** track follows the PlayQueue contract —
          the pointer stays at the same slot, which is now the *following*
          track (clamped at the end). The UI consequence is that playback must
          advance to that new current track. If the queue is now empty, stop.

        This keeps engine and UI consistent with the pinned PlayQueue
        ``remove(current)`` semantics (Phase 3): remove-current == skip to the
        track that slid into its place, not "stay paused on a dead index".
        """
        n = len(self.queue.tracks)
        if not (0 <= index < n):
            return
        was_current = index == self.queue.current_index
        self.queue.remove(index)
        if was_current:
            track = self.queue.current
            if track is None:
                # Removed the last remaining track — nothing to play.
                self.stop()
            else:
                # The following track slid into the current slot; play it.
                self._report_stop()
                self._load_track_uri(track, gapless=False, resume=self.playing)
        self._refresh_can_go()
        self._update_prefetch()
        self.emit("songs-list-changed", self.queue.current_index)

    def move(self, from_index: int, to_index: int) -> None:
        """Reorder the queue (drag-to-reorder), then recompute prefetch.

        The current track is preserved on its object by PlayQueue.move; a move
        can change which track is the gapless successor, so prefetch is always
        recomputed and ``songs-list-changed`` emitted.
        """
        n = len(self.queue.tracks)
        if not (0 <= from_index < n) or not (0 <= to_index < n):
            return
        if from_index == to_index:
            return
        self.queue.move(from_index, to_index)
        self._refresh_can_go()
        self._update_prefetch()
        self.emit("songs-list-changed", self.queue.current_index)

    def clear_queue(self) -> None:
        """Empty the queue and stop playback (queue-tab "clear" button)."""
        self.stop()
        self.queue.clear()
        self._refresh_can_go()
        self._update_prefetch()
        self.emit("songs-list-changed", -1)

    def _refresh_can_go(self):
        self.can_go_next = self.queue.peek_next() is not None
        self.can_go_prev = self.queue.current_index > 0
        self.notify("can-go-next")
        self.notify("can-go-prev")

    # ------------------------------------------------------------------ #
    # Volume                                                             #
    # ------------------------------------------------------------------ #

    def query_volume(self):
        volume = self.playbin.get_property("volume")
        if self.quadratic_volume:
            return round(volume ** (1 / 2), 1)
        return round(volume, 1)

    def change_volume(self, value):
        if self.quadratic_volume:
            self.playbin.set_property("volume", value ** 2)
        else:
            self.playbin.set_property("volume", value)
        self.emit("volume-changed", value)

    # ------------------------------------------------------------------ #
    # ReplayGain                                                         #
    # ------------------------------------------------------------------ #

    def apply_replaygain_tags(self):
        """Apply ReplayGain tags to the sink if normalization is enabled.

        Jellyfin tracks do not currently carry ReplayGain metadata, so this is
        a no-op that keeps the sink's taginject element consistent; kept so the
        normalize pipeline path matches upstream and is ready when RG metadata
        becomes available.
        """
        if not self.normalize:
            return
        audio_sink = self.playbin.get_property("audio-sink")
        rgtags = audio_sink.get_by_name("rgtags") if audio_sink else None
        tags = ""
        if rgtags:
            rgtags.set_property("tags", tags)
        self.most_recent_rg_tags = f"tags={tags}"

    # ------------------------------------------------------------------ #
    # Slider / position / duration                                       #
    # ------------------------------------------------------------------ #

    def _update_slider_callback(self):
        self.update_timer = None
        if not self.duration:
            self.duration = self.query_duration()
        self.emit("update-slider")
        # Periodic progress report (reporter rate-limits to >=10s itself).
        self._report_progress(paused=not self.playing)
        # Coarse resume-state persist piggybacked on this existing tick (no new
        # timer); rate-limited internally to >= _PERSIST_INTERVAL.
        self._persist_state()
        if self.playing:
            self.update_timer = GLib.timeout_add(1000, self._update_slider_callback)
        return False

    def query_duration(self):
        success, duration = self.playbin.query_duration(Gst.Format.TIME)
        return duration if success else 0

    def query_position(self, default=0) -> int:
        success, position = self.playbin.query_position(Gst.Format.TIME)
        return position if success else default

    def seek(self, seek_fraction):
        """Seek to ``seek_fraction`` (0.0–1.0) of the current track."""
        if not self.seeked_to_end and seek_fraction > 0.98:
            self.use_about_to_finish = False
            self.seeked_to_end = True
            self.play_next(user=True)
            return
        position = int(seek_fraction * self.query_duration())
        self.playbin.seek_simple(
            Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT, position
        )
        # A seek forces an immediate progress report.
        self._report_progress(paused=not self.playing, force=True)

    # ------------------------------------------------------------------ #
    # Resume-state persistence + restore                                 #
    # ------------------------------------------------------------------ #

    def _current_position_secs(self) -> float:
        """Best-effort current playback position in seconds (>=0)."""
        return max(0, self.query_position()) / Gst.SECOND

    def _persist_state(self, force: bool = False) -> None:
        """Persist the live queue / index / position / shuffle to the db.

        Called from the MAIN loop (slider tick, pause, shutdown) — never the
        GStreamer streaming thread. The actual write goes through
        ``db.meta_set`` which marshals onto the db writer thread, so no db work
        runs on the main thread beyond enqueueing. Rate-limited to
        ``_PERSIST_INTERVAL`` unless ``force`` (pause / quit).

        An empty queue clears the stored state so a fresh start next launch
        doesn't restore a stale queue.
        """
        if self._db is None:
            return
        now = GLib.get_monotonic_time() / 1_000_000.0
        if (
            not force
            and self._last_state_persist_t is not None
            and (now - self._last_state_persist_t) < _PERSIST_INTERVAL
        ):
            return
        self._last_state_persist_t = now

        tracks = self.queue.tracks  # snapshot (live order)
        track_ids = [getattr(t, "id", None) for t in tracks]
        track_ids = [tid for tid in track_ids if tid]
        if not track_ids:
            blob = serialize_player_state([], -1, 0.0, self.queue.shuffle)
        else:
            blob = serialize_player_state(
                track_ids,
                self.queue.current_index,
                self._current_position_secs(),
                self.queue.shuffle,
            )
        try:
            self._db.meta_set(PLAYER_STATE_KEY, blob)
        except Exception:
            logger.debug("failed to persist player state", exc_info=True)

    def load_persisted_state(self):
        """Read + parse the stored resume state from the db (or None).

        Pure read off the stored blob; returns the normalized dict from
        :func:`deserialize_player_state` (ids NOT yet resolved against the
        library) or ``None`` when there is nothing / it is corrupt. Safe to
        call before login.
        """
        if self._db is None:
            return None
        try:
            blob = self._db.meta_get(PLAYER_STATE_KEY)
        except Exception:
            logger.debug("failed to read player state", exc_info=True)
            return None
        return deserialize_player_state(blob)

    def restore_state(self, tracks, current_index, position_secs, shuffle,
                      repeat_type=None) -> bool:
        """Rebuild the queue from resolved tracks and load PAUSED at position.

        ``tracks`` is the already-resolved, library-validated list (ids no
        longer present were dropped by the caller); it is normalized through
        the player boundary here per the cardinal rule. The current track is
        loaded and SEEKED to ``position_secs`` but left PAUSED — restore never
        autoplays. Shuffle/repeat are applied to the live queue WITHOUT
        re-shuffling (the saved order is the live order). Re-arms gapless
        prefetch. Returns True if a track was loaded, False if nothing to
        restore.

        IMPORTANT: ``shuffle`` is set directly on the queue's backing flag (not
        via the player ``shuffle`` setter) so the saved order is preserved —
        the setter would re-run ``_apply_shuffle`` and reshuffle. Unshuffle
        history is intentionally NOT restored (see module/test notes), so a
        post-restore shuffle-OFF keeps the current live order.
        """
        tracks = normalize_tracks(tracks)
        if not tracks:
            return False
        n = len(tracks)
        idx = max(0, min(int(current_index), n - 1))

        # Build the queue with the EXACT saved order. set_tracks would re-apply
        # shuffle if the flag were already on, so set the order first with
        # shuffle off, then flip the backing flag directly to mark the state
        # without reshuffling.
        self.queue.set_tracks(tracks, idx)
        if shuffle:
            with self.queue._lock:
                self.queue._shuffle = True
                self.queue._original_order = []
        else:
            with self.queue._lock:
                self.queue._shuffle = False
        if repeat_type is not None:
            self.queue.repeat_type = RepeatType(repeat_type)
        self.notify("shuffle")
        self.notify("repeat-type")

        track = self.queue.current
        if track is None:
            return False

        # Load PAUSED + seek-on-preroll. Mirror _load_track_uri's non-gapless
        # path but transition to PAUSED (not PLAYING) so the stream prerolls
        # (enabling the seek in _on_track_start) without starting audio.
        self.playing = False
        self._restore_seek_secs = max(0.0, float(position_secs or 0.0))
        uri = self._resolve_uri(track)
        if uri is None:
            logger.warning("restore: no stream URI for current track")
            return False
        self.use_about_to_finish = False
        self.pipeline.set_state(Gst.State.NULL)
        self.playbin.set_property("uri", uri)
        self.pipeline.set_state(Gst.State.PAUSED)
        self.use_about_to_finish = True

        self._refresh_can_go()
        self._update_prefetch()
        logger.info(
            "restored queue: %d tracks, index %d, pos %.1fs, shuffle=%s",
            n, idx, self._restore_seek_secs, bool(shuffle),
        )
        return True

    # ------------------------------------------------------------------ #
    # Lifecycle                                                          #
    # ------------------------------------------------------------------ #

    def shutdown(self):
        """Tear down the player cleanly (call once on app shutdown).

        Sends a final ``report_stop`` (with the current position) for any
        track still reported as playing, flushes + closes the reporter worker,
        and drops the pipeline to NULL so GStreamer releases the audio device.
        Safe to call when nothing is playing — ``_report_stop`` is a no-op when
        no track is active.
        """
        # Persist resume state one last time before everything tears down, so a
        # real quit (app.quit / SIGTERM) restores exactly where we left off.
        # This is the quit/shutdown flush path (NOT the close-to-tray path,
        # which keeps the window hidden and playback running).
        self._persist_state(force=True)
        # Final stop for the in-flight track (carries current position ticks).
        self._report_stop()
        self._reporter.flush()
        self._reporter.close()
        # Clear + tear down the Discord presence (sends CLOSE, joins worker).
        if self._discord is not None:
            try:
                self._discord.clear()
                self._discord.close()
            except Exception:
                logger.debug("error during Discord RPC shutdown", exc_info=True)
            self._discord = None
        # Release the audio device / pipeline resources.
        try:
            self.pipeline.set_state(Gst.State.NULL)
        except Exception:
            logger.exception("Error setting pipeline to NULL during shutdown")
        logger.info("player shutdown complete")
