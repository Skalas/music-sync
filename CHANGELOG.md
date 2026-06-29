# Changelog

All notable changes to this project are documented here.
Format loosely follows [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased] — Sprint 2: local web app ("buttons")

A local web app over the existing `musicsync` core: FastAPI backend (`musicsync/web/`) + a
vanilla TypeScript + Vite SPA (`web/`). See `docs/sprint-2-app.md`.

### Added
- **FastAPI backend** (`musicsync/web/`, presentation layer reusing `SyncService` +
  `csv_export`): `GET /api/library` (presence + per-platform deep links, search/filter/
  pagination), `GET /api/auth/{platform}` (connection status), `POST /api/auth/{platform}/connect`,
  `POST /api/sync` (live dry-run diff), `GET /api/sync/stream` (SSE progress),
  `POST /api/apply/{platform}` (guarded write), `GET /api/export.csv`.
- **Vanilla TS + Vite SPA** (`web/`): Connections, Library, and Actions views.
- **Per-platform link resolver** (`musicsync/web/links.py`): Spotify/Tidal deep links from
  `presence.platform_id`; Apple uses a `music.apple.com/search` URL.
- New deps: `fastapi`, `uvicorn`, `sse-starlette`; `httpx` (dev). Node toolchain under `web/`.
- **Richer track metadata** (closes #12): `album`, `artwork_url`, `duration_sec`, `year` on the
  `tracks` table and per-platform `added_at` on `presence`, populated on read — Spotify/Tidal get
  everything incl. artwork; Apple gets album/year/duration/date-added (no artwork via AppleScript).
  Surfaced in `GET /api/library` (+ `sort_by=added_at`, sorted in SQL) and the Library table
  (artwork thumbnail, album, year, duration, sortable "Added" column); CSV export gains the
  columns. Idempotent schema migration (`PRAGMA table_info` + `ALTER TABLE ADD COLUMN`) upgrades
  an existing `library.db` in place; new fields backfill on the next sync. Identity is unchanged —
  metadata is display-only; the match key stays name + primary artist.
- **`Makefile`** with self-documenting targets: `make dev` (backend + SPA together), `install`,
  `sync`/`apply`/`export`, `gate`, `smoke`, etc.

### Changed
- **"Sync now" reads only connected platforms.** The web container builds a provider only when
  its platform is connected (Spotify/Tidal token cached; Apple = `osascript` available), so a
  live sync never triggers an interactive OAuth or crashes on missing credentials. Unconnected
  platforms are skipped gracefully.
- **Read-side graceful degradation generalized** (resolves the Sprint-3 deferred debt): the
  `graceful_on_apply_error` provider flag is renamed `graceful_on_error` and now governs read
  *and* apply; a new `SyncOptions.skip_unavailable_providers` lets the web degrade every
  provider while the CLI keeps its exact behavior (Tidal graceful, Spotify/Apple loud).
- `SqliteTrackRepository` serializes all connection access behind a re-entrant lock and opens
  with `check_same_thread=False` (FastAPI runs sync routes in a threadpool); the enriched
  library query moved into `iter_enriched_rows()` (no raw SQL in the web layer).
- Shared `build_providers` factory now used by both the CLI and the web container (no
  duplicated wiring).

### Security
- Server binds `127.0.0.1` only (never the LAN); `:8080` left free for the OAuth callback.
- **CSRF protection:** state-changing POSTs require an `X-Requested-With` header (forces a
  preflight that the localhost-only CORS policy blocks for other origins). CORS narrowed to
  GET/POST, `allow_credentials` dropped.
- SPA renders API-sourced text via `textContent`/escaping (no `innerHTML` injection).

## [Unreleased] — Sprint 3: connector abstraction & cleanup

Pure structural refactor — **no functional behavior change**, guarded by the existing test
suite. Goal: one coherent connector contract so the upcoming dashboard (Sprint 2) builds on a
clean provider layer. See `docs/sprint-3-connectors.md`.

### Changed
- **Clean-architecture boundary enforced**: `musicsync/application` no longer imports
  `musicsync/infrastructure`. `write_review` was promoted to the `LibraryProvider` port; the
  concrete `SpotifyProvider` import + `isinstance` check are gone. A new architecture test
  (`tests/test_architecture.py`, AST-based) fails the build if the boundary is ever violated.
- **Adding a platform is now cheap**: single `PLATFORMS` tuple in the domain
  (`musicsync/domain/platforms.py`); the apply dispatch is a data-driven loop; per-platform
  graceful-degradation is a `graceful_on_apply_error` flag on the port instead of a
  name check. No `if platform == …` branches remain in the apply path.
- Shared helpers extracted (plain functions, no new class hierarchy): `infrastructure/_env.py`
  (`load_env_keys`, each provider keeps its own missing-key policy), `domain/_time.py`
  (`utc_now_iso`). Duplicated platform tuples, time helpers, and output-path constants removed.
- Output paths consolidated in `application/output_paths.py` and **anchored to the project
  root** (previously CWD-relative) — files now land in the same place regardless of the working
  directory. Providers receive their paths by injection; the CWD-relative defaults are gone.

### Security
- **Tidal token cache written atomically and private from creation** (`tempfile.mkstemp` +
  `chmod 0600` + `os.replace`), closing the brief world-readable window between `write_text`
  and `chmod` in the previous implementation.
- `_RedirectHandler` OAuth `code`/`state` moved off shared class-level attributes into a
  per-call holder; `load_env_keys` asserts an absolute `base_dir` (fails loud on misuse).

### Deferred (with trigger)
- Read-side graceful skip still name-checks `provider.name == "tidal"` (pre-existing). Trigger:
  when a 4th platform needs graceful read degradation → generalize to a `graceful_on_error`
  flag and a platform-neutral log message.
- Apple `apply_likes` marks tracks synced even if `mark_loved.applescript` fails. Trigger: if
  "loved" flags go missing despite a clean run → propagate the AppleScript failure.

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
- Tidal read/apply failures now log only the exception class name, never the exception
  string (which could carry the request URL or response fragments if logs are redirected).

### Fixed (review round)
- `TIDAL_CLIENT_SECRET` removed: the PKCE public-client flow never sent it, so requiring it
  in `.env` was dead config. Dropped from `_load_config`, `.env.example`, and the README.
- `_RedirectHandler` OAuth `code`/`state` are reset before each listen, so a second
  interactive auth in the same process can no longer read a stale authorization code.

### Deferred (now planned as Sprint 3 — see `docs/sprint-3-connectors.md`)
- Clean-arch: `application` imports `infrastructure.SpotifyProvider` for `write_review`.
- `--no-apple`/`--no-spotify` vs `--no-tidal` semantic asymmetry (intentional, undocumented).
- Platform tuple `("spotify","apple","tidal")` hardcoded in 3 files; duplicated time/env helpers.
