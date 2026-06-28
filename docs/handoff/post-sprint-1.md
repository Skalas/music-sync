# Post-sprint 1 handoff — Tidal + SQLite library

**Branch:** `feat/tidal-sqlite-library` · **Mode:** HOLD · **Base:** `main`

## What shipped

- **Three-way union** across Spotify, Apple Music, and **Tidal** (official OAuth2/PKCE API),
  replacing the old 2-way Spotify⇄Apple reconciler. Union rule unchanged in spirit: liked
  on any connected platform → queued for every other platform where it's missing and not
  already synced.
- **SQLite is the source of truth** (`library.db`, tables `tracks` + `presence`). `state.json`
  is migrated in once and then retired. `synced_at` per (key, platform) replaces the old
  done-sets.
- **CSV export** (`--export`) — one row per track, one column per platform's liked flag.
- **All remote writes are opt-in** behind `--apply-spotify` / `--apply-apple` / `--apply-tidal`.
  A default run reads, updates the DB, and writes review files only.
- New `musicsync/` package in clean layers; 22 tests covering DoD T1–T9; ship gate
  (`ruff` + `mypy` + `pytest`) green.

## Verification

- Review: 3 blockers found and fixed in 1 round (Apple write gate; token-cache perms 0600;
  token-error no longer leaks response body). 0 blockers remaining.
- Smoke: seed idempotent (5 tracks / 7 presence stable); offline dry-run union math verified
  by hand against the seed; CSV export produced correct per-platform columns; zero network
  calls in `--offline`.

## POC limits / things to know

- **Tidal coverage is uncertain.** The official API's support for personal favorites
  read/write may be partial or change. The adapter is best-effort by design: it skips Tidal
  on failure and never breaks Spotify⇄Apple. If favorites endpoints don't behave, treat Tidal
  as read-only or run `--no-tidal`.
- **Apple writes changed default behavior**: Apple used to be pushed on every run; it is now
  opt-in (`--apply-apple`). Intentional, but a behavior change for existing users.
- **No git remote** is configured, so the sprint issue ledger (`.metate/issues.json`) is local
  and ship cannot auto-close issues until a remote + GitHub repo exist.

## Deferred debt (with triggers)

| Item | Trigger that forces the fix |
|---|---|
| `application/sync_service.py` imports `infrastructure.SpotifyProvider` and `isinstance`-checks it for `write_review` (clean-arch boundary violation). | When a 2nd provider needs a pre-apply review file → promote `write_pending(tracks, path)` to the `LibraryProvider` port. |
| `--no-apple`/`--no-spotify` only suppress writes; `--no-tidal` excludes from the union. Asymmetric + untested. | When a user reports surprising sync after `--no-apple`, or when adding a 4th platform → make `_should_skip_provider` table-driven and consistent. |
| Platform tuple `("spotify","apple","tidal")` hardcoded in 3 files. | When adding a 4th platform → hoist to a single `domain` constant. |
| `TIDAL_CLIENT_SECRET` required in `.env` but unused under PKCE. | When tidying Tidal config, or if a config dump is ever logged → drop it from `_load_config` + `.env.example`. |
| Apple `apply_likes` marks tracks synced even if `mark_loved.applescript` fails. | If "loved" flags go missing despite a clean run → propagate the AppleScript failure. |
| `_RedirectHandler` keeps `code`/`state` as class-level mutables. | When a single process performs a 2nd interactive auth → reset to instance attrs. |
| `test_backward_compat` asserts positional order. | When the golden fixture gains overlapping keys → compare sorted key lists. |

## Next sprint pointers

- Decide Tidal's real coverage from a live run; if writes are unsupported, formalize Tidal as
  read-only in the union.
- Add a GitHub remote so the issue ledger and PR auto-close wiring become live.
- Consider a tiny read-only viewer (or just document `sqlite3 library.db` recipes) if CSV
  isn't enough day-to-day.
