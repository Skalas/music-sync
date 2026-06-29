# Post-sprint 2 handoff — local web app ("buttons")

**Branch:** `feat/web-app` · **Mode:** EXPAND · **Base:** `main`

## What shipped

A local web app over the existing tested `musicsync` core — the CLI was too manual to use, so
this is buttons. FastAPI backend (`musicsync/web/`, presentation layer) + a vanilla TypeScript
+ Vite SPA (`web/`). No business logic in the web layer — it delegates to `SyncService` /
`csv_export`.

- **Three views:** Connections (per-platform status + Connect/OAuth), Library (searchable table
  from `library.db` with per-platform deep links + "on all three" badge), Actions (Sync now,
  guarded per-platform Apply, Export CSV).
- **Endpoints:** `/api/library`, `/api/auth/{platform}` (+ `/connect`), `/api/sync` (live
  dry-run) + `/api/sync/stream` (SSE), `/api/apply/{platform}` (guarded), `/api/export.csv`.
- **Link resolver** (`musicsync/web/links.py`): Spotify/Tidal deep links from
  `presence.platform_id`; Apple uses a search URL.
- **"Sync now" reads only connected platforms** — the container builds a provider only when its
  platform is connected (Spotify/Tidal token cached; Apple = `osascript` present), so a live
  sync never triggers an interactive OAuth or crashes on missing credentials.
- **Richer track metadata (closes #12), folded into this sprint:** `album`/`artwork_url`/
  `duration_sec`/`year` on `tracks`, per-platform `added_at` on `presence`, populated on read
  (Spotify/Tidal incl. artwork; Apple album/year/duration/date-added, no artwork). Surfaced in
  `/api/library` (+ SQL-side `sort_by=added_at`), the Library table (thumbnail/album/year/
  duration/sortable Added), and CSV. Idempotent migration upgrades an existing DB in place
  (verified on a copy of the real ~1000-track `library.db`, no data loss). Identity unchanged.
- **`Makefile`** — `make dev` runs backend + SPA together; plus `install`/`sync`/`apply`/
  `export`/`gate`/`smoke`.

## Verification

- **Ship gate green:** `ruff` clean · `mypy` clean · **98 tests**.
- **Review (metate-review):** three converged cycles, 0 surviving blockers. Round 1 fixed 4
  blockers — LAN bind (`0.0.0.0`→`127.0.0.1`), shared-SQLite-connection thread safety
  (re-entrant lock), CSRF on state-changing POSTs (`X-Requested-With`), and graceful startup when
  a provider is unconfigured. Round 2 fixed all DESIGN/DRY + the connect-flow robustness
  (timeout/409/logging) and added live "Sync now"; the Sprint-3 read-side `provider.name ==
  "tidal"` debt was resolved here. Round 3 (metadata) fixed migration-allowlist, duration/cover
  hardening, SQL-side sort, and DRY cleanups. All driven through the `claude -p` CLI implementer.
- **Smoke:** seed idempotent; A1–A7 exercised over the real HTTP server; CLI behavior preserved
  (independently verified: Spotify-fail raises, Tidal-fail degrades; web degrades all). A live
  no-`.env` run confirmed "Sync now" returns 200 and skips unconnected platforms (Apple read
  locally via osascript). Metadata: migration on a real-DB copy preserved all rows;
  `sort_by=added_at` verified live. UX signed off by the user.

## POC limits / things to know

- **Config is OAuth-only by design.** Credentials live in `.env` (hand-edited); the app never
  reads/writes secret material beyond what the providers already load.
- **Local-only.** Server binds `127.0.0.1:8000`; `:8080` is kept free for the OAuth callback.
  CSRF guard requires `X-Requested-With` on state-changing POSTs.
- **Apple links are search URLs** (no stored catalog id); Spotify/Tidal deep links need a
  `platform_id`, which appears after a live sync — the seed has none, so seeded demos show only
  Apple links.
- **A4/A5 (real OAuth round-trip) are manual** — only the status shapes are smoke-tested.

## Deferred debt (with triggers)

| Item | Trigger that forces the fix |
|---|---|
| Real per-track Apple links (catalog id capture via the Shortcut → `presence.platform_id`). | When Apple search URLs prove too imprecise. |
| Stretch: playlist creation (`PlaylistService`, Spotify first). | When "make a playlist from this filter" becomes the next obvious button. |
| Finer sync progress (per-track) over SSE — currently coarse staged events. | When a live sync is slow enough that users want a real progress bar → add a `SyncService` progress callback. |
| `output_paths.BASE_DIR` assumes an in-tree/editable layout. | If the package is ever installed non-editable. |

## Next sprint pointers

- The provider layer now has four consumers (CLI + three web concerns) behind one clean
  contract; the architecture test keeps `application` free of `infrastructure` imports.
- Decide Tidal's real coverage from a live run; formalize read-only if writes are unsupported.
- A one-command launcher (boot API + open browser) and an optional menubar/`.app` shim remain
  out of scope but are the obvious "make it feel native" follow-up.
