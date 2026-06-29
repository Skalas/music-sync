# Sprint 4 plan — playlist mirroring

**Status:** planned, not started. Follows Sprint 2 (web app + metadata). Net-new capability:
today the tool reconciles only **liked/favorite songs**; there is **no playlist code at all**
(playlists were a Sprint-2 stretch goal, deferred). This sprint adds reading playlists and
mirroring them across platforms.

## Goal

Keep a **named playlist** in sync across Spotify ⇄ Apple Music ⇄ Tidal: a track in playlist
*P* on any connected platform is added to *P* on every other platform where it's missing —
the same N-way union the liked-songs engine already does, but scoped per playlist instead of
the global "liked" set. Reuse the existing match key, SQLite-as-source-of-truth, and opt-in
write model unchanged.

## Decisions to lock at prep (proposed defaults)

1. **Identity:** playlists are matched across platforms by **name** (case-insensitive, trimmed);
   tracks within a playlist by the existing `normalize_key(name, primary_artist)`. A "mirror set"
   is the union, per playlist name, of its tracks across the selected platforms.
2. **Direction:** **N-way additive union** by default (like likes) — never deletes. A track only
   ever gets *added* to a playlist where it's absent. **Strict mirror** (also remove tracks not
   in the source, reorder to match) is **out of scope** for this sprint — it's destructive and
   needs its own safeguards.
3. **Selection:** the user explicitly chooses *which* playlists to mirror (opt-in per playlist),
   not "mirror everything." Avoids surprise writes to dozens of playlists.
4. **Writes opt-in per platform**, consistent with the existing model (`--apply-*` / guarded web
   buttons). A default run reads playlists, updates the DB, and writes a review file only.
5. **Ordering:** not preserved this sprint (additive append). Order-faithful mirroring is deferred.

## Per-platform capability (verify at prep — this is the main risk)

| Platform | Read playlists | Create playlist | Add tracks | Notes |
|---|---|---|---|---|
| **Spotify** | ✅ `current_user_playlists` + items | ✅ `user_playlist_create` | ✅ `playlist_add_items` | needs scopes `playlist-read-private`, `playlist-modify-private`/`public` |
| **Apple** | ✅ AppleScript (user playlists in Music) | ✅ AppleScript `make new playlist` | ✅ add by match (no catalog id) | local automation; matching is name+artist like loved-songs |
| **Tidal** | ⚠️ uncertain (same caveat as favorites) | ⚠️ uncertain | ⚠️ uncertain | **degrade gracefully** — skip Tidal playlist direction on failure, never break Spotify⇄Apple |

The Tidal playlist endpoints' real coverage is the biggest unknown. The adapter must follow the
existing `graceful_on_error` pattern: on any auth/scope/endpoint failure, skip the Tidal playlist
direction with a clear message and leave Spotify⇄Apple mirroring intact.

## Architecture (builds on the Sprint-3 provider contract)

- **Domain:** a `Playlist` value object (name + ordered track keys) and a pure
  `compute_playlist_union(playlists_by_platform, already_synced)` mirroring `compute_to_sync` —
  no I/O, fully unit-testable, the analogue of the liked-songs union.
- **Ports:** extend `LibraryProvider` (or a sibling `PlaylistProvider` protocol) with
  `read_playlists()`, `create_playlist(name)`, `add_to_playlist(name, tracks)`. Keep liked-songs
  methods unchanged. A provider that can't do playlists advertises it (a `can_playlist` flag,
  like `can_write`) so the app layer skips it without `if platform == …`.
- **SQLite (source of truth):** new `playlists` (id, platform, name, remote_id?) and
  `playlist_tracks` (playlist_id, track_key, synced_at) tables. Idempotent migration (ALTER/CREATE
  IF NOT EXISTS) — existing `library.db` upgrades in place. Mirror decisions derive from these rows.
- **Application:** a `PlaylistSyncService` parallel to `SyncService` — read selected playlists →
  upsert → compute per-playlist union → (opt-in) create/add on each target. Reuse `SyncOptions`-style
  flags + `skip_unavailable_providers`.
- **Web (presentation):** a new **Playlists** view — list playlists per platform, pick which to
  mirror + target platforms, **preview** the per-playlist diff (dry-run), then guarded **Mirror**
  buttons. New endpoints: `GET /api/playlists`, `POST /api/playlists/sync` (dry-run diff),
  `POST /api/playlists/mirror/{platform}` (guarded write). Same CSRF + connected-gating as today.
- **CLI:** `--mirror-playlist "<name>"` (repeatable) + the existing `--apply-*`/`--offline` flags.

## DoD matrix (draft)
- **P1** — `compute_playlist_union` is pure and correct for 0/1/2/3-platform cases (unit-tested, offline).
- **P2** — `GET /api/playlists` lists playlists per connected platform with track counts.
- **P3** — playlist read → DB upsert is idempotent; migration adds tables to an existing DB without data loss.
- **P4** — dry-run mirror produces the correct per-playlist add-set; writes nothing.
- **P5** — guarded mirror creates the playlist if absent and adds only missing tracks, per target platform.
- **P6** — Tidal playlist failure degrades gracefully; Spotify⇄Apple mirroring still completes.
- **P7** — match-key identity reused; Apple adds by search/match (no catalog id); never duplicates a track already present.
- **P8** — ship gate green (`ruff` + `mypy` + `pytest`); architecture boundary intact.

## Out of scope (Sprint 4)
- **Strict/destructive mirror** (removing tracks, reordering) — additive only.
- Collaborative/followed playlists, smart playlists, playlist *artwork*/description sync.
- Real-time/continuous sync (it stays a manual run, like today).
- Apple catalog-id capture (still match-based; see issue #12 follow-up).

## Dependencies / sequencing
- Builds cleanly on the Sprint-3 connector contract (one provider port, `PLATFORMS`, graceful
  degradation) and the Sprint-2 web shell (Connections/Library/Actions → add Playlists).
- Confirm Tidal playlist API coverage from a live probe at prep; if writes are unsupported,
  formalize Tidal playlists as read-only in the union (mirror INTO Spotify/Apple only).
