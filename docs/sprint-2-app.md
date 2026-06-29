# Sprint 2 plan — "buttons": local web app

**Status:** shipped on `feat/web-app` (see `docs/handoff/post-sprint-2.md`). Motivation: the CLI is too
manual to actually use — the goal is buttons. Builds on the clean connector contract from
Sprint 3 (`docs/handoff/post-sprint-3.md`): the web layer becomes the *fourth* consumer of the
provider layer, and `tests/test_architecture.py` keeps its imports honest.

## Decision: local web app, NOT native Swift

**FastAPI backend reusing the existing `musicsync/` core + a Vite/TypeScript SPA frontend,
launched locally and opened in the browser.** (Rationale unchanged: the tested Python engine is
reused as-is; Apple is local automation either way; OAuth already runs through the browser and
SQLite is already the source of truth.)

## Locked decisions (Sprint 2)

1. **Configuration = OAuth-only.** The Connections view shows per-platform connected/not status
   and a **Connect** button that starts the existing OAuth flow. Client IDs / secrets / redirect
   URIs stay in `.env`, hand-edited. The app never reads or writes credential material beyond
   what the providers already load — smallest security surface.
2. **Ports: app API on `:8000`, Vite dev server on `:5173`; `:8080` stays free** for the
   transient OAuth callback servers the Spotify/Tidal flows already spin up. **No changes to the
   developer consoles** — today's `http://127.0.0.1:8080` redirect URIs are reused as-is.
3. **Links: real deep links for Spotify/Tidal from `presence.platform_id`; Apple = search URL**
   (`https://music.apple.com/search?term=…`). Apple catalog-id capture stays deferred (its
   trigger lives in the Sprint-3 handoff). A small link-resolver keyed by platform centralizes this.
4. **Frontend: vanilla TypeScript + Vite SPA.** Minimal dependencies, no framework runtime.

## Architecture

- **Backend** (`musicsync/web/`, presentation layer — depends only on application services):
  FastAPI wrapping the existing `SyncService` + `csv_export`. A long-running sync runs as a
  background task; progress streamed via **SSE** (replaces the CLI's `tqdm`). No new business
  logic in the web layer. A `musicsync/web/links.py` resolver builds per-platform URLs.
- **Frontend** (`web/`): Vite + TypeScript SPA. Three views:
  1. **Connections (config)** — one card per platform: connected / not; **Connect** kicks off
     OAuth (Spotify/Tidal) or shows Apple Shortcut/automation status. Read from `GET /api/auth`.
  2. **Library (the view)** — table from `library.db`: searchable/filterable, one column per
     platform with a deep-link icon when present, an "on all three" badge, like/sync status.
  3. **Actions** — "Sync now" (dry-run, streamed diff), per-platform guarded **Apply** buttons
     (confirm dialog → `--apply-*` equivalent), "Export CSV".

### Endpoints
- `GET /api/library?q=&filter=&page=` — paginated songlist + per-platform presence + deep links
- `GET /api/auth/{platform}` — connection status · `POST /api/auth/{platform}/connect` — start OAuth
- `POST /api/sync` (dry-run by default) → diff · `GET /api/sync/stream` — SSE progress
- `POST /api/apply/{platform}` — guarded write (the `--apply-*` equivalent)
- `GET /api/export.csv`

## DoD matrix
- **A1** — `GET /api/library` returns correct presence + working deep links for a seeded DB (offline).
- **A2** — "Sync now" runs a dry-run and renders the diff; progress streams to the UI via SSE.
- **A3** — per-platform "Apply" calls the guarded path and never writes without the explicit action.
- **A4** — Connections panel reflects real auth state; "Connect" completes an OAuth round-trip.
- **A5** — OAuth callback works on `:8080` without colliding with the app server on `:8000`.
- **A6** — CSV export downloadable from the UI.
- **A7** — link resolver: Spotify/Tidal deep links from `platform_id`; Apple search URL.

## Out of scope (Sprint 2)
- **Stretch playlist creation** (`PlaylistService`) — deferred to a later sprint.
- **Apple catalog-id capture** — Apple links stay search-based; presence remains match-based.
- App Store / notarized distribution; mobile; multi-user; cloud hosting (stays local-first).

## Deferred (with trigger)
- Real per-track Apple links. Trigger: when search URLs prove too imprecise → extend the
  Shortcut/AppleScript to store the Apple catalog id in `presence.platform_id`.
- Playlist creation. Trigger: when the library view is in daily use and "make a playlist from
  this filter" becomes the next obvious button → add `PlaylistService` (Spotify first).
