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

- Review (round A, build): 3 blockers found and fixed in 1 round (Apple write gate;
  token-cache perms 0600; token-error no longer leaks response body). 0 blockers remaining.
- Review (round B, `metate-review` on this branch): 0 blockers (two "CRITICAL" candidates
  rejected on adversarial verification — a misread `mark_synced` claim and the intentional
  directional `--no-X` semantics). 3 warnings fixed in place (see "Fixed (review round)" in
  the CHANGELOG); the remaining DESIGN findings were filed as Sprint 3, not auto-applied.
- Smoke: seed idempotent (5 tracks / 7 presence stable across two runs); offline dry-run
  union math reconciled to the row (spotify 2 / apple 3 / tidal 3 against the seed); CSV
  export produced 5 correct per-platform rows; zero network calls in `--offline`.

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

Resolved in review round B (no longer deferred): `TIDAL_CLIENT_SECRET` dead-config removed;
`_RedirectHandler` `code`/`state` reset before each listen; Tidal error logging no longer
emits exception strings. The clean-arch / asymmetry / hardcoded-platform items below are now
**scheduled as Sprint 3** (`docs/sprint-3-connectors.md`), kept here for the trigger record.

| Item | Trigger that forces the fix |
|---|---|
| `application/sync_service.py` imports `infrastructure.SpotifyProvider` and `isinstance`-checks it for `write_review` (clean-arch boundary violation). | **Scheduled — Sprint 3, item 1.** Also forced when a 2nd provider needs a pre-apply review file → promote `write_review(tracks, path)` to the `LibraryProvider` port. |
| `--no-apple`/`--no-spotify` only suppress writes; `--no-tidal` excludes from the union. Asymmetric (intentional, directional) + undocumented in code. | **Scheduled — Sprint 3, item 7.** Add a docstring on `_should_skip_provider` so it isn't "fixed" into a bug; revisit if a 4th platform needs different gating. |
| Platform tuple `("spotify","apple","tidal")` hardcoded in 3 files; duplicated time/env helpers. | **Scheduled — Sprint 3, items 2–4.** Forced when adding a 4th platform → single `domain` constant + shared helpers. |
| Apple `apply_likes` marks tracks synced even if `mark_loved.applescript` fails. | If "loved" flags go missing despite a clean run → propagate the AppleScript failure. |
| `test_backward_compat` asserts positional order. | When the golden fixture gains overlapping keys → compare sorted key lists. |

## Next sprint pointers

- **Sprint 3 comes first: connector abstraction & cleanup — see `docs/sprint-3-connectors.md`.**
  Pure structural refactor (no behavior change, guarded by the 22 tests): promote `write_review`
  to the port to kill the application→infrastructure import, single `PLATFORMS` constant in the
  domain, shared env/time/path helpers, and collapse the three near-identical `_apply_X` methods.
  **Sequenced ahead of the dashboard** because the web app adds a 4th consumer of the provider
  layer — cleaning the contract first avoids baking today's duplication into a second call site.
- **Then Sprint 2: a local web app ("buttons") — see `docs/sprint-2-app.md`.** Wraps the
  `musicsync` core in a FastAPI backend + TypeScript SPA: connections panel, searchable library
  dashboard (per-platform links + "on all three" badge), guarded apply buttons, CSV export, and
  a stretch "make a playlist". Decision: web app, not Swift, to reuse the tested Python core and
  because Apple Music is local automation anyway. Two design problems to solve there: OAuth port
  collision on 8080, and Apple having no stored track id (links are search-only until the
  Shortcut/AppleScript captures a catalog id into `presence.platform_id`).
- Decide Tidal's real coverage from a live run; if writes are unsupported, formalize Tidal as
  read-only in the union.
- Add a GitHub remote so the issue ledger and PR auto-close wiring become live.
