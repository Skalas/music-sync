# Sprint 3 plan — connector abstraction & cleanup

**Status:** planned, not started.
**Sequencing:** runs **before** Sprint 2 (the local web app / dashboard, `docs/sprint-2-app.md`,
which is captured-not-started). Rationale: the dashboard will add a *fourth* consumer of the
provider layer (`PlaylistService` + FastAPI). Doing it on today's duplicated, leaky connector
code bakes the duplication into a second call site. Clean the connectors first, then build the
dashboard on a stable port.

> Source: design findings from the `metate-review` pass on `feat/tidal-sqlite-library`
> (3 warnings already fixed in that branch; the items below are the DESIGN bucket, which is
> never auto-applied). No behavior change is intended by this sprint — it is a structural
> refactor guarded by the existing 22-test suite.

## Goal

One coherent connector contract. Every platform (Spotify, Apple, Tidal, and the next one)
plugs in through the same port, shares the same env-loading / time / path / platform-list
primitives, and the application layer depends on **nothing** in `infrastructure`.

## Definition of Done

- `musicsync/application/` has **zero** imports from `musicsync/infrastructure/` (enforced by a test).
- Adding a new platform touches exactly: one provider class + one row in a single `PLATFORMS`
  tuple. No new `if platform == ...` branches in the application layer.
- All 22 existing tests still pass unchanged (pure refactor; golden fixtures untouched).
- `ruff` + `mypy` clean (the ship gate).

## Failure modes to guard

- **Silent behavior drift.** The N-way union and the Spotify⇄Apple golden path must be
  byte-identical before/after. The backward-compat golden test (`test_backward_compat.py`) is
  the tripwire — run it after every step.
- **Over-abstraction.** A `LibraryProvider` protocol is a real architecture boundary, so it
  earns its keep. Resist inventing base classes for things with one caller (KISS). The shared
  helpers below are *functions*, not a class hierarchy.
- **Leaking the secret-handling policy.** Spotify exits the process on missing creds; Tidal
  raises `TidalError` for graceful skip. That policy difference is deliberate — the shared
  env loader must let the *caller* choose the failure action, not centralize it.

---

## Work items (ordered; each is independently shippable)

### 1. Kill the application→infrastructure layer violation  *(highest value)*
`sync_service.py:13` imports `SpotifyProvider` concretely, solely for an `isinstance` check at
line 171 to call `provider.write_review(...)`.

**Fix:** add a `write_review(tracks, path)` method to the `LibraryProvider` protocol in
`musicsync/domain/ports.py` with a default no-op. Application calls the interface; Spotify
overrides it. Delete the concrete import and the `isinstance`.

**Watch:** this is the one item that changes a public contract (the port). Land it first so the
rest builds on the clean interface.

### 2. Single source for the platform list
`PLATFORMS`/`PLATFORM_COLUMNS = ("spotify", "apple", "tidal")` is duplicated in
`sqlite_repository.py:36` and `csv_export.py:10`, and the union/active-platform logic hardcodes
the same tuple inline (`sync_service.py:85`).

**Fix:** define `PLATFORMS` once in the domain (`musicsync/domain/track.py` or a small
`musicsync/domain/platforms.py`). Import everywhere. This is the lever that makes "adding a
platform = one row" true.

### 3. Shared env-key loader
`_load_config` is structurally identical in `spotify_provider.py:59` and `tidal_provider.py:55`
(load_dotenv → read keys → detect missing). The only real difference is the failure action.

**Fix:** `musicsync/infrastructure/_env.py` →
`load_env_keys(base_dir, keys) -> tuple[dict[str,str], list[str]]` returning (config, missing).
Each provider decides what to do with `missing` (Spotify `sys.exit`, Tidal `raise TidalError`).
Policy stays at the call site.

### 4. Shared time helper
`_utc_now_iso` (`sync_service.py:213`) and `_utc_now` (`sqlite_repository.py:245`) are identical.

**Fix:** one helper in the domain (`track.py` or `domain/_time.py`); import from both.

### 5. Consolidate output-path constants
`TO_APPLE_PATH` is declared twice (`apple_provider.py` + `sync_service.py`); `unmatched.log` is
written inline in `spotify_provider.py:182` as a third copy; relative paths resolve against CWD.

**Fix:** one home for output-path constants (application layer), anchored to `BASE_DIR` like
`STATE_PATH` already is, so running from any directory writes to the same place.

### 6. Collapse the three `_apply_X` methods
`_apply_spotify` / `_apply_apple` / `_apply_tidal` (`sync_service.py:121-211`) share one
skeleton (guard empty → lookup provider → branch on apply flag → apply+mark_synced **or** write
review file). The duplication will triple-compound when the dashboard and a 4th platform arrive.

**Fix:** `_apply_platform(platform, tracks, apply_flag, result, now)` holding the skeleton;
per-platform specials (Spotify's `write_review`, Tidal's exception→skip) become small overrides
or injected callables. Keep it boring — this is deduplication, not a framework.

### 7. Minor / opportunistic
- `tidal_provider.py:_now_epoch` does a lazy `import time` for no reason — hoist to top-level.
- `reconcile_legacy` (`domain/union.py:45`) is a test-only migration shim living in the domain;
  move it into `tests/test_backward_compat.py` so the domain only holds the living
  `compute_to_sync` contract. (Golden test must import from its new home.)
- The `--no-spotify`/`--no-apple` (target-only) vs `--no-tidal` (full exclude) asymmetry is
  **intentional** (directional help text), but undocumented in code. Add a one-line docstring on
  `_should_skip_provider` so the next reader doesn't "fix" it into a bug.

---

## Out of scope
- No new platforms this sprint (the goal is to make adding one cheap, not to add one).
- No web/dashboard work — that is Sprint 2, which follows this.
- No change to the SQLite schema or the match-key identity rule.
