# Post-sprint 3 handoff — connector abstraction & cleanup

**Branch:** `refactor/connector-abstraction` · **Mode:** REDUCE · **Base:** `main`

## What shipped

Pure structural refactor — **no functional behavior change** (the one intentional exception:
output files are now anchored to the project root instead of CWD-relative). Net effect: adding
a new platform is now cheap, and the application layer no longer reaches into infrastructure.

- **Layer boundary enforced.** `write_review` promoted to the `LibraryProvider` port; the
  `application → infrastructure` import + `isinstance` check removed. New AST-based
  `tests/test_architecture.py` fails the build if `musicsync/application` ever imports
  `musicsync/infrastructure` again.
- **One connector contract.** Single `PLATFORMS` tuple (`musicsync/domain/platforms.py`);
  apply dispatch is a data-driven loop; per-platform graceful degradation is a
  `graceful_on_apply_error` flag on the port. `_apply_platform` has no platform-name literals.
- **Shared primitives** (plain functions, no class hierarchy): `infrastructure/_env.py`
  (`load_env_keys` — each provider keeps its own missing-key policy: Spotify `sys.exit`, Tidal
  `raise TidalError`), `domain/_time.py` (`utc_now_iso`). The three near-identical `_apply_X`
  methods collapsed into one skeleton.
- **Output paths** consolidated in `application/output_paths.py`, anchored to the project root,
  injected into providers (CWD-relative defaults deleted).
- **Security hardening folded in from review:** token cache written atomically + `0600` from
  creation; `_RedirectHandler` state moved to a per-call holder; `load_env_keys` asserts an
  absolute `base_dir`.

## Verification

- **Ship gate green:** `ruff` clean · `mypy` clean (28 files) · **23/23 pytest** (the original
  22 + the new architecture guard).
- **Review (metate-review):** converged, **0 blockers**. Two escalations rejected on
  adversarial verification (a `sorted()` "test weakening" that is actually correct — it compares
  the real key-set contract, since `reconcile_legacy` and `compute_to_sync` legitimately differ
  in order; and a token-cache TOCTOU that is pre-existing, not introduced here). The remaining
  DESIGN + warning findings (D1–D4, W1–W3) were all routed to the implementer and fixed.
- **Smoke:** offline dry-run union math unchanged after the dispatch refactor (missing —
  spotify 2 / apple 3 / tidal 3 against the seed); CSV export identical (5 rows). Behavior held.

## POC limits / things to know

- **No new platforms or features** — the sprint made adding one cheap, it did not add one.
- **Output-path anchoring is the only behavior change.** Files (`canciones_to_apple.txt`,
  `to_spotify_review.txt`, `unmatched.log`, `library.db`, `.tidal-cache`) now resolve against
  the project root, not the directory you run from. Intentional (removes CWD fragility).
- `output_paths.BASE_DIR` assumes an in-tree / editable layout; a non-editable `pip`/`uv`
  install into `site-packages` would resolve the root incorrectly. Acceptable for this
  local dev tool; documented inline.

## Deferred debt (with triggers)

| Item | Trigger that forces the fix |
|---|---|
| Read-side graceful skip still name-checks `provider.name == "tidal"` (pre-existing; DoD's "no *new* `if platform ==`" is met). | When a 4th platform needs graceful **read** degradation → generalize to a `graceful_on_error` flag + platform-neutral log message. |
| Apple `apply_likes` marks tracks synced even if `mark_loved.applescript` fails. | If "loved" flags go missing despite a clean run → propagate the AppleScript failure. |
| `output_paths.BASE_DIR` uses a `__file__`-relative climb. | If the package is ever installed non-editable → pass an explicit base dir (git root) at startup. |

## Next sprint pointers

- **Sprint 2 (the local web app / dashboard) is now unblocked — see `docs/sprint-2-app.md`.**
  It builds on the clean provider contract this sprint produced: the FastAPI `PlaylistService`
  becomes the *fourth* consumer of the provider layer, and the architecture test will keep its
  imports honest. Two design problems still open there: OAuth port collision on 8080, and Apple
  having no stored track id (links are search-only until the Shortcut captures a catalog id into
  `presence.platform_id`).
- Decide Tidal's real coverage from a live run; if writes are unsupported, formalize Tidal as
  read-only in the union.
