# Changelog

All notable changes to this project are documented here.
Format loosely follows [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased] — Sprint 1: Tidal + SQLite library

### Added
- **Tidal** as a third platform via the official `developer.tidal.com` OAuth2 (PKCE) API.
  Reads favorites and (with `--apply-tidal`) adds favorites. Degrades gracefully: on
  auth/scope/endpoint failure it logs once, skips Tidal, and the Spotify⇄Apple
  reconciliation still completes.
- **SQLite source of truth** (`library.db`): `tracks` + `presence` tables replace
  `state.json`. Legacy `state.json` is migrated in once, automatically.
- **N-way union**: a track liked on any connected platform is queued for every other
  platform where it is absent and not already synced.
- **CSV export** via `--export PATH.csv` — one row per track, one column per platform.
- New flags: `--apply-apple`, `--apply-tidal`, `--db PATH`, `--export PATH.csv`,
  `--offline`, `--no-tidal`.
- `musicsync/` package with clean layers (domain / application / infrastructure /
  presentation) and a `musicsync.seed` module for offline smoke.
- Test suite (22 tests) covering the DoD matrix T1–T9.

### Changed
- **Apple Music writes are now opt-in** behind `--apply-apple` (previously unconditional),
  aligning Apple with Spotify and Tidal under the "all remote writes are opt-in" rule. A
  default run now only reads, updates `library.db`, and emits review files.
- `sync_music.py` is now a thin CLI entrypoint over the `musicsync` package.
- Install switched to `uv sync` (deps + dev group pinned in `uv.lock`).

### Security
- Tidal token cache (`.tidal-cache`) written with mode `0600` (holds the refresh token).
- Token-error exceptions no longer embed the OAuth response body (could echo `code`/token).

### Deferred (see `docs/handoff/post-sprint-1.md`)
- Clean-arch: `application` imports `infrastructure.SpotifyProvider` for `write_review`.
- `--no-apple`/`--no-spotify` vs `--no-tidal` semantic asymmetry.
- `TIDAL_CLIENT_SECRET` required but unused under pure PKCE.
