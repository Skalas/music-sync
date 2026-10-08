# Post-sprint 5 handoff — real Apple Music track links

**Branch:** `feat/apple-catalog-links` · **Mode:** HOLD · **Base:** `feat/sync-reliability` (stacked)
**Closes debt:** post-sprint-2 "Real per-track Apple links" — done with the Apple Music API, not the Shortcut.

## What shipped

Apple links in the web app now open the exact song (`https://music.apple.com/{storefront}/song/{id}`)
instead of a search page. Catalog ids are resolved in a batch step and stored in SQLite; the web
request path never touches the network.

- **ISRC capture:** `Track.isrc`, read from Spotify `external_ids.isrc` and Tidal `attributes.isrc`,
  stored in `tracks.isrc` (idempotent migration, COALESCE upsert, carried through key merges).
- **Apple Music API client** (`infrastructure/apple_music_api.py`): ES256 developer token from
  `.env` (`APPLE_TEAM_ID`, `APPLE_KEY_ID`, `APPLE_PRIVATE_KEY_PATH`, `APPLE_STOREFRONT`), cached and
  re-minted before expiry, never logged. Bounded retries on 429/5xx honoring `Retry-After`. All
  malformed responses fail closed as `AppleMusicError`.
- **Matching** (`domain/apple_catalog.py`, pure): ISRC first, then name + primary-artist search.
  A hit must have the same *recording title* (`normalize_recording_title`: ignores feat./with
  credits, "From … Soundtrack" tags and remaster/deluxe tags; keeps live/remix/acoustic/edit/
  version/instrumental markers), duration within ±3 s, preferring originals over compilations,
  then the stored album. Ambiguity → unresolved → search-URL fallback. **Never a wrong link.**
- **`apple_catalog` table:** `(key, catalog_id, storefront, resolved_at)`. NULL = not found, retried
  after 30 days; storefront change re-resolves; rows follow `merge_keys` / title-only dedupe.
- **CLI:** `sync_music.py --resolve-apple-links [--limit N]` (rejects sync flags). Also runs after a
  normal sync when credentials exist, capped at 200 tracks, never under `--offline`/`--dry-run`;
  a resolver failure prints one line and never fails the sync.
- **Web:** `track_url("apple", platform_id=catalog_id, storefront=…)` → song URL; search URL otherwise.

## Verification

- **Ship gate:** ruff clean · mypy clean · **292/292 pytest** (141 at sprint start; the base
  branch's 9 pre-existing mypy errors in test fakes were fixed along the way).
- **DoD:** T1–T9 all pass (`dod.json` commands). Offline smoke unchanged (5 rows exported).
- **Live (T8, real API, `sv`):** 40-track batch on a copy of `library.db` → **35 resolved**, stable
  across runs; spot-checked ids map to the right songs on original albums. The 5 unresolved: 4
  title-only (empty-artist) duplicate rows (by design) and 1 where Apple credits a different lead artist.
- **Review:** 3 rounds + a confirming pass. Blockers fixed: B1 resolver errors crashing a successful
  sync · B2 resolver running under `--dry-run` · B3 search accepting live/remix variants · B4 ISRC
  path ignoring title on merged rows · B5 malformed nested JSON crash · B6 marker inside a feat.
  bracket being dropped. Warnings fixed: W2 over-strict title (recall 30→35/40), W3/W4 flag conflicts.

## Things to know

- **Your library has no ISRCs yet** (`0 / 1041`): the DB predates capture. The next normal sync
  backfills them from Spotify/Tidal; ISRC matching then kicks in for those tracks.
- First syncs resolve 200 tracks each; run `sync_music.py --resolve-apple-links` once to do the
  whole library in one go.
- The MusicKit key lives at `~/.config/musicsync/` (outside the repo). Developer tokens are minted
  with a 12 h lifetime.

## Deferred debt (with triggers)

| Item | Trigger that forces the fix |
|---|---|
| Transient empty Apple search results are cached as "not found" for 30 days (seen once live: 31/40 vs stable 35/40). | If many obvious tracks stay on the search URL → retry NULL rows sooner (e.g. 1 day on first miss) or don't cache an empty result set. |
| NULL `apple_catalog` row isn't invalidated when a track later gains an ISRC. | After the first ISRC backfill sync, if previously-missed tracks with ISRCs stay unresolved → clear NULL rows whose track gained an ISRC. |
| `.gitignore` has no `*.p8` pattern. | Before anyone keeps a key inside the repo dir → add `*.p8`. |
| Per-field `Track` copies repeated at ~6 sites (isrc added by hand at each). | Next metadata field → `dataclasses.replace` + one `METADATA_FIELDS` tuple. |
| `run_apple_link_resolution` threads a `required` flag through two functions; `normalize_text` is a pass-through wrapper; `LIMIT -1` sentinel; unused `CatalogSong.url`. | Next change to Apple-link CLI reporting or normalization → collapse while there. |
| Search path accepts a neutral `" - with X"` suffix difference against a same-artist title. | If a same-artist wrong link is ever reported → require full key equality unless the only difference is a soundtrack tag. |

## Next sprint pointers

- **Sprint 4 (playlist mirroring)** is still planned and unstarted — `docs/sprint-4-playlists.md`.
- Merge order: `feat/sync-reliability` → `main` first, then this branch.
