# Sprint 2 plan (captured) — "buttons": local web app

**Status:** captured, not started. Motivation: the CLI is too manual to actually use — the
goal is buttons.

## Decision: local web app, NOT native Swift

**FastAPI backend reusing the existing `musicsync/` core + a Vite/TypeScript SPA frontend,
launched locally and opened in the browser.**

Why (efficiency-first; user explicitly deprioritized stack preference):
- The sync/match/union/repository engine is already Python and tested (22 tests). Swift
  would require rewriting it or bundling Python into a `.app` and shelling out — both worse.
  The web backend imports `musicsync` and calls the same services directly: zero core rewrite.
- Apple Music has no API — it is `osascript`/Shortcuts on the local Mac. The backend keeps
  driving it exactly as the CLI does today; Swift's ScriptingBridge is a rewrite for marginal gain.
- OAuth already runs through the browser (Spotify + Tidal); SQLite is already the source of
  truth, so the dashboard is read queries and per-platform links come from `presence.platform_id`.

Trade-off accepted: it's a localhost app you launch, not a Dock icon. Mitigate with a
one-command launcher; optionally a thin `.app`/menubar shim later that boots the server and
opens the browser.

## Architecture

- **Backend** (`musicsync/web/`, presentation layer — depends only on application services):
  FastAPI wrapping the existing `SyncService` + `csv_export` + a new `PlaylistService`.
  Long-running sync runs as a background task; progress streamed via SSE/WebSocket (replaces
  the CLI's `tqdm`). No new business logic in the web layer.
- **Frontend** (`web/`): Vite + TypeScript SPA. Three areas:
  1. **Connections** — one card per platform: connected / not, "Connect" button kicks off the
     OAuth flow (Spotify/Tidal) or shows the Apple Shortcut/Automation status.
  2. **Library** — table from `library.db`: searchable/filterable, one column per platform
     with a link icon when present, an "on all three" badge, like/sync status.
  3. **Actions** — "Sync now", per-platform "Apply" buttons (guarded, with a confirm dialog,
     mapping to `--apply-*`), "Export CSV".

### Endpoints (sketch)
- `GET /api/library?q=&filter=` — paginated songlist + per-platform presence + deep links
- `POST /api/sync` (dry-run by default) → returns the diff; `GET /api/sync/stream` for progress
- `POST /api/apply/{platform}` — guarded write (the `--apply-*` equivalent)
- `GET /api/auth/{platform}` / `POST /api/auth/{platform}/connect` — status + start OAuth
- `GET /api/export.csv`
- `POST /api/playlist` — see stretch goal

## Known design problems to solve in this sprint

1. **OAuth port collision.** Spotify and Tidal redirect to `127.0.0.1:8080`, and the auth
   flows spin up a transient localhost server on that port. If the app server also runs on
   8080 the callback collides. Decision needed: run the app on a different port (e.g. 5173/8000)
   and keep 8080 free for the auth callback servers, OR handle the OAuth callback inside the
   app server itself (register `/callback` and update the redirect URIs in both dev consoles).
2. **Apple has no stable track id.** Today Apple tracks carry only name/artist (matched via the
   normalized key) — there is no Apple catalog id stored. So an "open on Apple Music" link can
   only be a search URL (`https://music.apple.com/search?term=…`), and presence is match-based.
   To get real per-track Apple links, capture the catalog id via the Shortcut/AppleScript and
   store it in `presence.platform_id` (a schema-populated, not schema-changed, task).
3. **Deep links per platform.** Spotify `https://open.spotify.com/track/{id}`, Tidal
   `https://tidal.com/browse/track/{id}`, Apple search URL (until #2 lands). Build a small
   link-resolver keyed by platform.

## Stretch — "turn something into a playlist"
- `PlaylistService`: from a filtered/selected set, create a playlist on a chosen platform.
  Spotify API supports create + add tracks. Tidal API support is uncertain (same caveat as
  favorites). Apple via a new Shortcut or AppleScript. Scope to Spotify first; treat
  Tidal/Apple as best-effort.

## DoD matrix (draft)
- A1 — `GET /api/library` returns correct presence + working links for a seeded DB (offline).
- A2 — "Sync now" runs a dry-run and renders the diff; progress streams to the UI.
- A3 — per-platform "Apply" calls the guarded path and never writes without the explicit action.
- A4 — Connections panel reflects real auth state; "Connect" completes an OAuth round-trip.
- A5 — OAuth callback works without colliding with the app server (problem #1 resolved).
- A6 — CSV export downloadable from the UI.
- A7 (stretch) — create a Spotify playlist from a filtered selection.

## Out of scope (sprint 2)
- App Store / notarized distribution; mobile; multi-user; cloud hosting (stays local-first).
