# Post-sprint 4 handoff — playlist mirroring (REDUCE)

**Branch:** `feat/playlist-mirroring` · **Mode:** REDUCE · **Base:** `main`
Plan: `docs/sprint-4-playlists.md` (decisions locked at prep narrowed it — see below).

## What shipped

- **Playlist mirroring Spotify ⇄ Apple Music**, opt-in per playlist, additive only:
  `sync_music.py --mirror-playlist "<name>"` (repeatable). Without `--apply-spotify` / `--apply-apple`
  it reads, updates SQLite and writes `playlists_review.txt` — zero remote writes. `--list-playlists`
  prints `platform<TAB>name<TAB>count` for your own playlists.
- **Domain:** `Playlist`, `PlaylistDiff`, pure `compute_playlist_union` (title-only tracks merged only
  when the title has exactly one artist; otherwise `[SIN ARTISTA]`, never written).
- **SQLite:** `playlists` + `playlist_tracks` (idempotent migration). Only playlists read in *this* run
  drive writes; a changed remote id drops stale mirror state.
- **Spotify:** playlist scopes (`playlist-read-private`, `playlist-read-collaborative`, `playlist-modify-*`
  only with `--apply-spotify`); requested scopes are the union with what the cached token already holds,
  so liked-songs and playlist runs never force each other to re-authorize. Adds resolve via Spotify
  search (never another platform's id), skip ids already in the playlist, never search without an artist.
  Own collaborative playlists are targets; followed/other-owner same-name playlists → ambiguous, skipped.
- **Apple:** AppleScript read/list/add. Adds only songs already in your library (rest → review file
  `SIN MATCH`); lookup by persistent id only, creates a new playlist only when there is no id; smart
  playlists, folders and system names are never targets (same-name → ambiguous, skipped).
- **Tidal:** playlists read-only (`can_playlist_write=False`).
- **Web:** `GET /api/playlists`, `POST /api/playlists/preview` (dry-run, XHR-guarded, under `sync_lock`),
  read-only Playlists view. Web read paths never start an interactive OAuth flow; explicit Apply/Connect
  clicks still may.
- **Fixed (P0):** Tidal liked-songs read was failing with HTTP 400 (Tidal stopped accepting a top-level
  `artists` include) → now `items.artists,items.albums`; reads 974 tracks with album data.

## Verification

- **Ship gate:** ruff · mypy · **393 pytest** · `npm run build` · all 5 AppleScripts compile.
- **DoD P0–P10:** all pass. P9 live: 13 Spotify + 92 Apple playlists listed after one Spotify consent.
- **Live preview** (no writes) of «Not Quite Dating»: 2 missing on Spotify, 3 on Apple, review file correct.
- **Review:** 3 rounds + 2 confirming passes; 10 blockers fixed (scope ping-pong, web OAuth popups,
  stale rows driving writes, same-name merge, empty-artist wrong-song risk, duplicate playlists on both
  Spotify and Apple, Apple writing into system lists/folders).

## Things to know

- Nothing has been written to any playlist yet. Suggested first real run: one playlist, review file
  first, then `--apply-spotify` / `--apply-apple`.
- `My Playlist #16` (1,239 Spotify / 1,203 Apple) is the biggest; Spotify adds go through search
  (~2 tracks/s).

## Deferred debt (with triggers)

| Item | Trigger that forces the fix |
|---|---|
| Match key keeps a year before "Remaster" (`"… - 2011 Remaster"` ≠ `"…"`), so previews over-report missing tracks (applies to liked songs too). Writes are protected (Spotify id dedupe, Apple exact name). | When a review file shows many remaster pairs → strip `\d{4} remaster` in `normalize_key` **with a re-key migration** of `library.db`. |
| Apple skip reason shows only the first stderr line (script name), not "no encontrada". | When an Apple playlist write is skipped and the reason is unclear → put stderr on the first line. |
| Web Spotify playlist gating reads the cached scope once at boot. | If users hit "run --list-playlists" and the web still hides Spotify → re-check per request or say "restart". |
| Skip/SystemExit policy duplicated (2 playlist + 2 liked sites); ambiguity applied post-hoc to the union; `_apply_all` nesting; flag-conflict checks in 4 places. | Next change to playlist apply policy or CLI modes → collapse while there. |
| Web mirror buttons, strict mirror (removals/reorder), Tidal playlist writes, Apple catalog adds via Shortcut. | Next playlist sprint. |
| Signal `sig-sync-stream-cross-site-trigger` (pre-existing CSRF on `/api/sync/stream`, DoS only). | Next `metate-scope`. |
