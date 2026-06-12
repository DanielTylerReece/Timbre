# utils.py
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

# Phase 0: tidalapi imports stripped.  Jellyfin data layer added in Phase 1.

import concurrent.futures
import os
import re
import subprocess
import threading
import uuid
import logging

from gettext import gettext as _
from pathlib import Path
from typing import Any, Callable, List, Optional

from gi.repository import Gdk, Gio, GLib

from .cache import HTCache

logger = logging.getLogger(__name__)


def _owner_alive(owner: Any) -> bool:
    """Return True if a widget owner is still usable for UI mutation.

    Robust for both ``Gtk.Window`` and ``Adw.Dialog``: an explicit ``_alive``
    flag (set False on close/dispose) wins; otherwise fall back to
    ``get_realized()``. A None owner means "no guard" -> always alive.
    """
    if owner is None:
        return True
    flag = getattr(owner, "_alive", None)
    if flag is not None:
        return bool(flag)
    get_realized = getattr(owner, "get_realized", None)
    if callable(get_realized):
        return bool(get_realized())
    return True


# Shared, bounded worker pool for the high-fan-out, short-lived jobs (card /
# row / queue image loads). Without it, a Home page or a long list spawns one
# raw Thread per card — dozens of threads contend for the GIL and the image
# cache locks at once. Capping at 8 keeps concurrency useful but bounded. The
# pool is created lazily so importing utils stays cheap / fork-safe.
_IMAGE_POOL_MAX_WORKERS = 8
_image_pool: "concurrent.futures.ThreadPoolExecutor | None" = None
_image_pool_lock = threading.Lock()


def _get_image_pool() -> "concurrent.futures.ThreadPoolExecutor":
    global _image_pool
    if _image_pool is None:
        with _image_pool_lock:
            if _image_pool is None:
                # Never explicitly shut down: we intentionally rely on
                # ThreadPoolExecutor's atexit hook to drain in-flight jobs at
                # interpreter exit (no leak — see tests/manual/leak_check.py).
                _image_pool = concurrent.futures.ThreadPoolExecutor(
                    max_workers=_IMAGE_POOL_MAX_WORKERS,
                    thread_name_prefix="jt-image",
                )
    return _image_pool


def run_async(
    work: Callable[[], Any],
    on_done: Optional[Callable[[Any], Any]] = None,
    on_error: Optional[Callable[[BaseException], Any]] = None,
    owner: Any = None,
    scheduler: Callable = GLib.idle_add,
    pool: bool = False,
    submit: Optional[Callable] = None,
):
    """Run ``work()`` off the main thread, marshalling results to the main loop.

    The result of ``work()`` is delivered to ``on_done`` via ``scheduler``
    (``GLib.idle_add`` by default). Any exception raised by ``work`` is logged
    and, if ``on_error`` is given, marshalled to it. When ``owner`` (a widget)
    is supplied, the marshalled callback first checks that the owner is still
    alive (see :func:`_owner_alive`) and silently skips if it has gone away —
    preventing use-after-free style callbacks into a destroyed Window/Dialog.

    Scheduling:
      * ``pool=False`` (default): a fresh daemon ``threading.Thread`` is started
        and returned (callers may ``join`` it in tests).
      * ``pool=True``: the job is submitted to a shared, bounded
        ``ThreadPoolExecutor`` (max 8 workers). This is for the many tiny image
        loads (cards / track rows / queue rows) where spawning a thread per call
        would otherwise flood the scheduler. The returned ``Future`` lets tests
        wait on completion.

    ``scheduler`` (and, for the pool path, ``submit``) are injectable so the
    logic is headless-testable without a GLib main loop or a real pool.
    """

    def _runner():
        try:
            result = work()
        except BaseException as exc:  # noqa: BLE001 — log + route to on_error
            logger.exception("run_async work failed")
            if on_error is not None:

                def _deliver_err():
                    if _owner_alive(owner) and on_error is not None:
                        on_error(exc)
                    return False

                scheduler(_deliver_err)
            return
        if on_done is not None:

            def _deliver():
                if _owner_alive(owner) and on_done is not None:
                    on_done(result)
                return False

            scheduler(_deliver)

    if pool:
        _submit = submit if submit is not None else _get_image_pool().submit
        return _submit(_runner)

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    return thread

# Placeholder lists — Jellyfin data filled in Phase 1
favourite_mixes: List[Any] = []
favourite_tracks: List[Any] = []
favourite_artists: List[Any] = []
favourite_albums: List[Any] = []
favourite_playlists: List[Any] = []
playlist_and_favorite_playlists: List[Any] = []
user_playlists: List[Any] = []


def init() -> None:
    """Initialize the utils module by setting up cache directories and global objects.

    Sets up the cache directory structure, creates necessary directories,
    and initializes the global cache object for Jellyfin API responses.
    """
    global CACHE_DIR
    base_cache = os.environ.get("XDG_CACHE_HOME")
    if not base_cache:
        base_cache = f"{os.environ.get('HOME')}/.cache"
    CACHE_DIR = f"{base_cache}/timbre"

    global IMG_DIR
    IMG_DIR = Path(CACHE_DIR, "images")
    IMG_DIR.mkdir(parents=True, exist_ok=True)

    global MUSIC_DIR
    MUSIC_DIR = Path(CACHE_DIR, "music")
    MUSIC_DIR.mkdir(parents=True, exist_ok=True)

    global session
    global navigation_view
    global player_object
    global toast_overlay
    global cache
    global client
    global db
    global settings
    global discovery
    session = None
    cache = HTCache()
    client = None
    db = None
    settings = None
    discovery = None


def make_provider_from_settings(settings_, secret_store):
    """Build an :class:`AIProvider` from live settings + the secret store.

    Reads ``ai-provider``/``ai-endpoint``/``ai-model`` from GSettings and the
    ``ai-api-key`` from the secret store every call, so a provider/key change in
    Preferences takes effect on the next AI request with no restart. Returns
    ``None`` (AI disabled) when the provider is "none" or the key is missing.
    """
    from .ai import make_provider

    if settings_ is None:
        return None
    provider = settings_.get_string("ai-provider")
    endpoint = settings_.get_string("ai-endpoint")
    model = settings_.get_string("ai-model")
    api_key = ""
    if secret_store is not None:
        try:
            api_key = secret_store.load().get("ai-api-key", "")
        except Exception:  # noqa: BLE001 — no key == no AI
            api_key = ""
    return make_provider(provider, endpoint, model, api_key)


def init_runtime(client_, db_, player_, settings_, secret_store=None) -> None:
    """Install the shared app-wide runtime objects (upstream utils pattern).

    Called once after a successful login/restore so every widget can reach the
    Jellyfin client, the local DB, the player, GSettings, and the AI Discovery
    layer via the ``utils`` module without threading them through constructors.

    ``discovery`` is a :class:`~lib.ai.discovery.Discovery` whose provider
    factory reads settings + the secret store live, so it tracks Preferences
    changes. It is always constructed (even with no AI provider configured) —
    its methods are no-op-with-fallback in that case, so callers never branch.
    """
    global client, db, player_object, settings, discovery
    client = client_
    db = db_
    player_object = player_
    settings = settings_

    from .ai.discovery import Discovery

    discovery = Discovery(
        db_, client_,
        provider_factory=lambda: make_provider_from_settings(
            settings_, secret_store
        ),
        # Live "push AI bios to Jellyfin" preference — read per call so a
        # Preferences toggle takes effect with no restart.
        push_bios=lambda: (
            settings_ is not None
            and settings_.get_boolean("push-bios")
        ),
    )


def get_alsa_devices() -> List[dict]:
    """Get ALSA devices"""
    try:
        alsa_devices = get_alsa_devices_from_aplay()
    except (
        subprocess.CalledProcessError,
        FileNotFoundError,
        subprocess.TimeoutExpired,
    ):
        alsa_devices = get_alsa_devices_from_proc()
    return alsa_devices


def get_alsa_devices_from_aplay() -> List[dict]:
    """Get ALSA devices from aplay -l"""
    result = subprocess.run(["aplay", "-l"], capture_output=True, text=True)

    devices = [
        {
            "hw_device": "default",
            "name": _("Default"),
        }
    ]
    for line in result.stdout.split("\n"):
        # Example String: card 3: KA13 [FiiO KA13], device 0: USB Audio [USB Audio]
        match = re.match(
            r"^card\s+\d+:\s+([^[]+)\s+\[([^\]]+)\],\s+device\s+(\d+):\s+([^[]+)\s+\[([^\]]+)\]",
            line,
        )
        if match:
            card_short_name = match.group(1).strip()  # "KA13"
            card_full_name = match.group(2).strip()  # "FiiO KA13"
            device = int(match.group(3))  # 0
            device_short_name = match.group(4).strip()  # "USB Audio"
            device_full_name = match.group(5).strip()  # "USB Audio"

            # Persistent device string
            hw_string = f"hw:CARD={card_short_name},DEV={device}"
            devices.append(
                {
                    "hw_device": hw_string,
                    "name": f"{card_full_name} - {device_full_name} ({hw_string})",
                }
            )

    return devices


def get_alsa_devices_from_proc() -> List[dict]:
    """Get ALSA devices from files in /proc/asound"""
    cards = {}
    card_names = {}
    with open("/proc/asound/cards", "r") as f:
        for line in f:
            # Example String:  3 [KA13           ]: USB-Audio - FiiO KA13
            match = re.match(r"^\s*(\d+)\s+\[([^\]]+)\]\s*:\s*.+?\s-\s(.+)$", line)
            if match:
                index = int(match.group(1))
                shortname = match.group(2).strip()
                fullname = match.group(3).strip()
                cards[index] = fullname
                card_names[index] = shortname

    devices = [
        {
            "hw_device": "default",
            "name": _("Default"),
        }
    ]
    with open("/proc/asound/devices", "r") as f:
        for line in f:
            # Example String:  19: [ 3- 0]: digital audio playback
            match = re.match(
                r"^\s*\d+:\s+\[\s*(\d+)-\s*(\d+)\]:\s*digital audio playback", line
            )
            if match:
                card, device = int(match.group(1)), int(match.group(2))
                card_name = cards.get(card, f"Card {card}")
                short_name = card_names.get(card, f"{card}")

                # Persistent device string
                hw_string = f"hw:CARD={short_name},DEV={device}"

                devices.append(
                    {
                        "hw_device": hw_string,
                        "name": f"{card_name} ({hw_string})",
                    }
                )

    return devices


def get_artist(artist_id: str) -> Any:
    global cache
    return cache.get_artist(artist_id)


def get_album(album_id: str) -> Any:
    global cache
    return cache.get_album(album_id)


def get_track(track_id: str) -> Any:
    global cache
    return cache.get_track(track_id)


def get_playlist(playlist_id: str) -> Any:
    global cache
    return cache.get_playlist(playlist_id)


def get_mix(mix_id: str) -> Any:
    global cache
    return cache.get_mix(mix_id)


def get_favourites() -> None:
    """Phase 0 stub — Jellyfin favorites fetching implemented in Phase 1."""
    pass


def is_favourited(item: Any) -> bool:
    """Phase 0 stub — always returns False until Jellyfin layer is connected."""
    return False


def send_toast(toast_title: str, timeout: int) -> None:
    """Display a toast notification. Phase 0: requires toast_overlay to be set."""
    if toast_overlay:
        from gi.repository import Adw
        toast_overlay.add_toast(Adw.Toast(title=toast_title, timeout=timeout))


def th_add_to_my_collection(btn: Any, item: Any) -> None:
    """Phase 0 stub — Jellyfin collection add implemented in Phase 1."""
    pass


def th_remove_from_my_collection(btn: Any, item: Any) -> None:
    """Phase 0 stub — Jellyfin collection remove implemented in Phase 1."""
    pass


def on_in_to_my_collection_button_clicked(btn: Any, item: Any) -> None:
    """Phase 0 stub — no-op until Jellyfin layer is connected."""
    pass


def share_this(item: Any) -> None:
    """Phase 0 stub — share URL support implemented in Phase 1."""
    pass


def get_type(item: Any) -> str:
    """Return type string for an item (uses hasattr duck-typing)."""
    if hasattr(item, '_type'):
        return item._type
    return "unknown"


def open_uri(label: str, uri: str) -> bool:
    """Phase 0 stub — URI navigation implemented in Phase 1."""
    logger.debug(f"open_uri stub: {uri}")
    return True


def open_jellyfin_uri(uri: str) -> None:
    """Phase 0 stub — Jellyfin URI handling implemented in Phase 1."""
    logger.debug(f"open_jellyfin_uri stub: {uri}")


def th_play_track(track_id: str) -> None:
    """Phase 0 stub — playback implemented in Phase 1."""
    pass


def pretty_duration(secs: int | None) -> str:
    """Format a duration in seconds to a human-readable string.

    Args:
        secs (int): Duration in seconds

    Returns:
        str: Formatted duration string (MM:SS or HH:MM:SS for durations over an hour)
    """
    if not secs:
        return "00:00"

    hours = secs // 3600
    minutes = (secs % 3600) // 60
    seconds = secs % 60

    if hours > 0:
        return f"{int(hours)}:{int(minutes):02}:{int(seconds):02}"
    else:
        return f"{int(minutes):02}:{int(seconds):02}"


def get_best_dimensions(widget: Any) -> int:
    """Determine the best image dimensions for a widget.

    Args:
        widget: A GTK widget to measure

    Returns:
        int: The best image dimension from available sizes (80, 160, 320, 640, 1280)
    """
    edge = widget.get_height()
    dimensions = [80, 160, 320, 640, 1280]
    # The function for fractional scaling is not available in GTKWidget
    scale = 1.0
    native = widget.get_native()
    if native:
        surface = native.get_surface()
        if surface:
            scale = surface.get_scale()
    return next((x for x in dimensions if x > (edge * scale)), dimensions[-1])


def image_cache_path(item_id: str, dimensions: int) -> "Path":
    """Local on-disk cache path for an item's primary image at a size."""
    return IMG_DIR / f"{item_id}_{dimensions}.jpg"


# In-flight dedup: concurrent fetches of the same (item_id, dimensions) image
# (common when the picture + miniplayer image load the same album art at once,
# or a track repeats) should not hit the network N times. The first caller for
# a key holds the per-key lock and does the fetch+write; later callers block on
# that lock, then fall through to the disk-cache check below (now populated).
_inflight_lock = threading.Lock()
_inflight_locks: dict = {}


def _inflight_key_lock(key) -> threading.Lock:
    with _inflight_lock:
        lock = _inflight_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _inflight_locks[key] = lock
        return lock


def fetch_image_to_path(
    item_id: str, image_tag: str | None, dimensions: int = 320
) -> str | None:
    """Return the local file path for an item's primary image, fetching it
    through the authenticated Jellyfin client if not already cached.

    Routes through ``client.fetch_image_bytes`` (sends the MediaBrowser auth
    header) per the Phase 4 image-auth decision. Returns None when there is no
    client, no item id, or the fetch fails / yields no bytes. Concurrent calls
    for the same (item_id, dimensions) are de-duplicated so only one network
    fetch happens; the others wait and reuse the on-disk result.
    """
    if not item_id or client is None:
        return None
    file_path = image_cache_path(item_id, dimensions)
    if file_path.is_file():
        return str(file_path)

    key = (item_id, dimensions)
    key_lock = _inflight_key_lock(key)
    with key_lock:
        # Re-check after acquiring: a concurrent fetch for the same key may have
        # populated the cache while we were blocked.
        if file_path.is_file():
            return str(file_path)
        try:
            data = client.fetch_image_bytes(item_id, image_tag, dimensions)
        except Exception:
            logger.debug("Could not fetch image for %s", item_id, exc_info=True)
            return None
        if not data:
            return None
        try:
            with open(file_path, "wb") as fh:
                fh.write(data)
        except OSError:
            logger.exception("Could not write image cache for %s", item_id)
            return None
        return str(file_path)


def add_image_from_tag(
    widget: Any,
    item_id: str,
    image_tag: str | None,
    dimensions: int = 320,
    cancellable: Gio.Cancellable | None = None,
) -> None:
    """Fetch (worker-thread) an item's primary image and set it on a widget
    that supports ``set_from_file`` (e.g. Gtk.Image). Call from a worker
    thread; the widget mutation is marshalled back via ``GLib.idle_add``.
    """
    path = fetch_image_to_path(item_id, image_tag, dimensions)

    def _apply():
        if cancellable is not None and cancellable.is_cancelled():
            return False
        if path:
            widget.set_from_file(path)
        return False

    GLib.idle_add(_apply)


def add_picture_from_tag(
    widget: Any,
    item_id: str,
    image_tag: str | None,
    dimensions: int = 640,
    cancellable: Gio.Cancellable | None = None,
) -> None:
    """Fetch (worker-thread) an item's primary image and set it on a widget
    that supports ``set_filename`` (e.g. Gtk.Picture)."""
    path = fetch_image_to_path(item_id, image_tag, dimensions)

    def _apply():
        if cancellable is not None and cancellable.is_cancelled():
            return False
        if path:
            widget.set_filename(path)
        return False

    GLib.idle_add(_apply)


def add_image(widget: Any, item: Any, cancellable: Any = None) -> None:
    """Back-compat shim for Phase 5+ widgets that still call ``add_image``.

    The new image path is tag-based (``add_image_from_tag``). This shim adapts
    an item exposing ``id`` + ``image_tag`` to it; items without those are a
    no-op (the widget keeps its placeholder icon).
    """
    item_id = getattr(item, "id", None)
    image_tag = getattr(item, "image_tag", None)
    if item_id:
        add_image_from_tag(widget, item_id, image_tag, cancellable=cancellable)


def add_avatar_from_tag(
    widget: Any,
    item_id: str,
    image_tag: str | None,
    dimensions: int = 320,
    cancellable: Gio.Cancellable | None = None,
) -> None:
    """Fetch (worker-thread) a primary image and set it as an Adw.Avatar's
    custom image. Tag-based sibling of ``add_image_from_tag`` (the avatar API
    needs a ``Gdk.Texture`` rather than a file path)."""
    if not item_id:
        return
    path = fetch_image_to_path(item_id, image_tag, dimensions)

    def _apply():
        if cancellable is not None and cancellable.is_cancelled():
            return False
        if path:
            file = Gio.File.new_for_path(path)
            widget.set_custom_image(Gdk.Texture.new_from_file(file))
        return False

    GLib.idle_add(_apply)


def add_image_to_avatar(widget: Any, item: Any, cancellable: Any = None) -> None:
    """Back-compat shim: set an Adw.Avatar's custom image from an item tag."""
    item_id = getattr(item, "id", None)
    image_tag = getattr(item, "image_tag", None)
    if not item_id:
        return
    path = fetch_image_to_path(item_id, image_tag)

    def _apply():
        if path:
            file = Gio.File.new_for_path(path)
            widget.set_custom_image(Gdk.Texture.new_from_file(file))
        return False

    GLib.idle_add(_apply)


def toggle_favorite(kind, item_id, current_state, owner=None, on_applied=None):
    """Toggle the favorite state of an item, writing through to server + db.

    Single-sourced favorite helper used by track rows, album/artist/playlist
    pages, and the player pane. Flips ``current_state`` to ``new_state``, then on
    a worker thread issues ``client.set_favorite`` and mirrors the result into
    SQLite via ``db.set_favorite_local``. The UI is expected to update
    optimistically *before* calling this (using the returned ``new_state``); on
    error the caller's ``on_applied(reverted_state)`` is invoked so it can revert
    the optimistic icon and a toast is shown.

    Args:
        kind: ``"track"`` | ``"album"`` | ``"artist"``.
        item_id: the Jellyfin item id.
        current_state: the favorite state *before* the toggle.
        owner: run_async owner-guard widget (gates callbacks after teardown).
        on_applied: optional ``callable(state)`` invoked on the main loop with
            the state that is now in effect — ``new_state`` on success,
            ``current_state`` (reverted) on failure.

    Returns:
        bool: the optimistic ``new_state`` the caller should display immediately.
    """
    new_state = not bool(current_state)

    def work():
        client.set_favorite(item_id, new_state)
        try:
            db.set_favorite_local(kind, item_id, new_state)
        except Exception:
            logger.debug("favorite db mirror failed for %s", item_id,
                         exc_info=True)
        return new_state

    def on_done(state):
        if on_applied is not None:
            on_applied(state)

    def on_error(exc):
        logger.info("toggle_favorite failed for %s/%s: %s", kind, item_id, exc)
        send_toast(_("Could not update favorite"), 2)
        if on_applied is not None:
            on_applied(current_state)  # revert

    run_async(work, on_done=on_done, on_error=on_error, owner=owner)
    return new_state


def add_track_to_playlist(playlist_id, playlist_name, track_id, owner=None):
    """Add one track to an EXISTING playlist: server + local write-through.

    Cheap-read duplicate guard FIRST (``db.playlist_has_track``): if the track
    is already in the playlist, toast "Already in <name>" and make NO server
    call. Otherwise issue ``client.add_playlist_items`` off the main thread; on
    success mirror into SQLite (``db.append_playlist_track`` — appends + bumps
    track_count) and toast "Added to <name>". On failure toast a server-
    unreachable message. Never silent.

    ``owner`` is the run_async owner-guard widget (gates the toast/db callback
    after the track row is torn down).
    """
    if client is None or track_id is None or playlist_id is None:
        send_toast(_("Couldn't add — not connected"), 2)
        return
    if db is not None and db.playlist_has_track(playlist_id, track_id):
        send_toast(_("Already in {}").format(playlist_name), 2)
        return

    def work():
        client.add_playlist_items(playlist_id, [track_id])
        if db is not None:
            try:
                db.append_playlist_track(playlist_id, track_id)
            except Exception:
                logger.debug("playlist db mirror failed for %s", playlist_id,
                             exc_info=True)

    def on_done(_result):
        send_toast(_("Added to {}").format(playlist_name), 2)

    def on_error(exc):
        logger.info("add_track_to_playlist failed for %s: %s",
                    playlist_id, exc)
        send_toast(_("Couldn't add — server unreachable"), 3)

    run_async(work, on_done=on_done, on_error=on_error, owner=owner)


def create_playlist_with_track(name, track_id, owner=None):
    """Create a new playlist containing ``track_id``: server + write-through.

    Issues ``client.create_playlist(name, [track_id])`` off the main thread,
    then mirrors the new playlist into SQLite (upsert + append the track) so
    Collection's playlist list reflects it without a full sync. Toasts
    "Playlist <name> created" on success, a server-unreachable message on
    failure. Never silent.
    """
    if client is None or track_id is None:
        send_toast(_("Couldn't create — not connected"), 2)
        return

    def work():
        new_id = client.create_playlist(name, [track_id])
        if db is not None and new_id:
            try:
                db.upsert_playlists(
                    [{"id": new_id, "name": name, "track_count": 0}]
                )
                db.append_playlist_track(new_id, track_id)
            except Exception:
                logger.debug("new-playlist db mirror failed for %s", new_id,
                             exc_info=True)
        return new_id

    def on_done(_new_id):
        send_toast(_("Playlist {} created").format(name), 2)

    def on_error(exc):
        logger.info("create_playlist_with_track failed for %r: %s", name, exc)
        send_toast(_("Couldn't create — server unreachable"), 3)

    run_async(work, on_done=on_done, on_error=on_error, owner=owner)


def create_jellyfin_session():
    """Phase 0 stub — returns None.  Jellyfin session object created in Phase 1."""
    return None


def setup_logging():
    log_to_file = os.getenv("LOG_TO_FILE")
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()

    handlers = []
    if log_to_file:
        log_dir = globals().get("CACHE_DIR")
        if not log_dir:
            base_cache = os.environ.get("XDG_CACHE_HOME")
            if not base_cache:
                base_cache = f"{os.environ.get('HOME')}/.cache"
            log_dir = f"{base_cache}/timbre"
        try:
            os.makedirs(log_dir, exist_ok=True)
            handlers.append(logging.FileHandler(log_dir + "/timbre.log"))
        except OSError as exc:
            logger.warning("Could not set up file logging in %s: %s", log_dir, exc)
    handlers.append(logging.StreamHandler())

    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
    )

def evict_cache(cache_dir, max_gb):
    # NOTE(post-1.0): LRU disk-cache eviction. Implemented + correct, but not yet
    # wired to a caller — there's no cache-size GSetting and no eviction policy
    # decision (when to run, which dirs, default budget) for 1.0. Wiring it is a
    # post-1.0 task so a release doesn't ship an untuned background file-deleter.
    if not cache_dir or not cache_dir.exists():
        return

    max_bytes = max_gb * 1024 ** 3
    files = sorted(
        cache_dir.iterdir(),
        key=lambda f: f.stat().st_atime
    )
    total = sum(f.stat().st_size for f in files)
    for f in files:
        if total <= max_bytes:
            break
        total -= f.stat().st_size
        f.unlink()
        logger.info(f"Evicted from cache: {f.name}")
