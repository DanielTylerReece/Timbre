# Changelog

All notable changes to Timbre are documented here. This project adheres to
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed

- **Rebrand: JellyTide → Timbre.** Full rename of the application across the
  codebase: app ID `io.github.tylerreece.timbre`, binary `timbre`, Python
  package, GResource prefix `/io/github/tylerreece/timbre`, GSettings schema id
  + path, gettext domain `timbre`, MPRIS bus `org.mpris.MediaPlayer2.timbre`,
  data/cache directories (`~/.local/share/timbre`, `~/.cache/timbre`), libsecret
  schema, and client/LRCLIB User-Agent strings. New stump-and-sprout application
  icon (replacing the ported High Tide icon) and a new wordmark.

## [0.1.0] — 2026-06-11

First release. A feature-complete GTK4/libadwaita Jellyfin music client, built
in phases on top of the High Tide skeleton (TIDAL layer fully replaced by a
Jellyfin data layer). Highlights:

### Added

- **Onboarding & auth** — Quick Connect (code-based authorization) and classic
  username/password login; secure credential storage.
- **Jellyfin data layer** — full replacement of the original TIDAL API layer:
  client, models, and a sync engine.
- **Sync-first SQLite library** — `full_sync` / incremental sync into a local
  database for fast, offline-capable browsing; all UI reads from SQLite, never
  blocks the main loop.
- **Playback** — GStreamer `playbin3` engine with **gapless** track transitions
  (prefetch + about-to-finish), favorites write-through, and play reporting.
- **Background audio + MPRIS** — playback survives window close; desktop media
  controls, art, and transport via MPRIS.
- **Browse pages** — Album, Artist, Playlist, Genre, Decade, Collection, and a
  generic paged from-function list, with a reusable card/track widget kit.
- **Home page** — recents and listening-history rows (wide-card / month-card
  widgets); F5 refresh.
- **Explore** — Genre / Decade / Search pages with album **year filtering**
  (dropdown + decade navigation).
- **AI discovery layer** (opt-in, bring-your-own-key) — custom mixes, personal
  radio, AI track radio, AI-ranked artist popularity, and artist bios over an
  OpenAI-compatible or Anthropic provider; pure-Python core with local
  heuristic fallback. Only library text metadata is ever sent; selections are
  validated against the local DB. Off by default. See the README privacy note.
- **Queue management** — drag-to-reorder, per-row remove, play-next /
  add-to-queue, clear, and a live queue view; player mutation API.
- **Lyrics** — synced vs. static lyrics with an indicator badge; lyrics cache.
- **Keyboard shortcuts** — transport + search accelerators and a shortcuts
  dialog.
- **Packaging** — native meson build/install; Arch/CachyOS `PKGBUILD`; desktop
  entry, AppStream metainfo, and GSettings schema (all validated).

### Testing & hardening

- 359-test pytest suite (fake server + temp DB), green.
- Headless manual battery: per-page weakref leak gate, population
  instance-count leak gate, live full_sync main-thread responsiveness gate, and
  a big-list memory/scroll measurement.
- Phase 10 leak fixes: auto-load row teardown now unparents its rows (was
  pinning track rows + link labels for the process lifetime); track-list pages
  now register their auto-load widget for teardown (was leaking each track
  row's global-player `song-changed` handler).
- Auto-load list capped at 500 rows (with a "use search to narrow" footer) —
  the accreted-rows design measured +870 MB RSS for the full 3,197-track
  library; the cap holds it to ~138 MB. A virtualized `Gtk.ListView` rewrite is
  tracked for post-1.0.

### Known limitations

- Flatpak packaging deferred to post-1.0.
- i18n / po translation deferred to post-1.0.
- Mix / artist-radio navigation targets are stubbed.
- valgrind memcheck is non-functional on Arch/CachyOS (stripped `ld.so`); the
  weakref + instance-count gates are authoritative. See `docs/MEMORY-GATES.md`.
