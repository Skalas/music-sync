# Sprint plan — Tidal + SQLite library (mode: HOLD)

## Goal

Turn the 2-way Spotify ⇄ Apple Music liked-songs reconciler into an **N-way union**
across **Spotify, Apple Music, and Tidal**, backed by a **SQLite database that becomes
the source of truth** (replacing `state.json`) and is **exportable to CSV**.

**Union rule (unchanged, generalized):** if a track is liked on *any* connected platform,
it should end up liked on *all* connected platforms. Writes to every platform stay
**opt-in** behind per-platform `--apply-*` flags.

## Decisions (locked)

- **Tidal access:** official `developer.tidal.com` OAuth2 API (Authorization Code + PKCE).
  Read favorites via the user/collection endpoints; write via add-to-favorites. Official
  coverage of personal favorites is **uncertain** → the Tidal adapter must degrade
  gracefully and never break the Spotify⇄Apple path.
- **Database:** single SQLite file (stdlib `sqlite3`), new source of truth. `state.json`
  is migrated in on first run, then retired.
- **Mode:** HOLD — generalize the union carefully, full test matrix, dry-run safety on
  every platform.

## Architecture

Light clean-architecture split (the platform boundary genuinely has 3 implementations,
which justifies an interface; everything else stays KISS). Proposed package `musicsync/`:

- **domain** — `Track`, `normalize_key`, the pure N-way union function, and the
  `LibraryProvider` port (read_liked / apply_likes / capability flags) + `TrackRepository`
  port.
- **application** — sync orchestration service (reconcile → per-direction apply), CSV
  export service.
- **infrastructure** — `SpotifyProvider` (existing spotipy logic), `AppleProvider`
  (existing Shortcut + AppleScript), `TidalProvider` (new, official API),
  `SqliteTrackRepository`, and `seed` module for offline smoke.
- **presentation** — `sync_music.py` CLI (kept as entrypoint; thin), arg parsing.

### SQLite schema (source of truth)

```sql
tracks(key TEXT PRIMARY KEY, name TEXT, artist TEXT, first_seen TEXT, last_seen TEXT);
presence(
  key TEXT, platform TEXT, liked INTEGER, platform_id TEXT,
  added_at TEXT, synced_at TEXT,
  PRIMARY KEY (key, platform), FOREIGN KEY (key) REFERENCES tracks(key)
);
```

- `presence.synced_at` replaces `state.json`'s done-sets (per platform, per key).
- CSV export = a pivot view: one row per track, one column per platform's liked flag.

### CLI surface (additions)

| Flag | Effect |
|---|---|
| `--apply-tidal` | actually add new likes to Tidal (write scope) |
| `--no-tidal` | skip the Tidal direction |
| `--db PATH` | SQLite file (default `library.db`) |
| `--export PATH.csv` | dump the full library to CSV and exit |
| `--offline` | no network; read/operate only on the DB (for smoke/tests) |

Existing `--apply-spotify`, `--no-apple`/`--no-spotify`, `--dry-run`, `--full` preserved.

## DoD test matrix

- **T1 — N-way union purity:** given presence across {spotify, apple, tidal}, the union
  function returns, per platform, exactly the keys liked elsewhere but missing there and
  not already synced. Covers 0/1/2/3-platform-present cases. (pure unit test, no I/O)
- **T2 — Backward compatibility:** with Tidal disabled, the Spotify⇄Apple diff is
  identical to the pre-sprint behavior (golden test against a fixture).
- **T3 — SQLite repository:** idempotent upsert (re-running yields no duplicate keys),
  `synced_at` is set only after a successful apply, and a locked/missing DB fails loudly.
- **T4 — state.json migration:** a legacy `state.json` is imported into `presence` once,
  producing the same done-set semantics; absent state.json is a clean first run.
- **T5 — CSV export:** `--export` produces one row per track with correct per-platform
  liked columns, UTF-8, on a seeded DB; non-empty and re-runnable.
- **T6 — Tidal adapter read:** parses favorites from the official API response shape into
  `Track`s (mocked HTTP); pagination handled.
- **T7 — Tidal write guard:** without `--apply-tidal`, no write call is made; with it,
  adds are batched and `synced_at` recorded. (mocked HTTP)
- **T8 — Tidal graceful degradation:** auth/scope/endpoint failure logs a clear message,
  skips the Tidal direction, and the Spotify⇄Apple reconciliation still completes.
- **T9 — Offline dry-run + seed:** `--offline --dry-run` against a seeded DB makes zero
  network calls and reports the diff (this is the smoke command).

## Out of scope (this sprint)

- Playlist sync (only Liked/Favorites).
- Any web UI / browser to view the DB (CSV export + raw SQLite is enough for now).
- Pushing a git remote / opening a real GitHub PR (no remote configured yet).
