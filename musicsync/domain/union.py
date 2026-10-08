"""Pure N-way union logic for liked-track reconciliation."""

from __future__ import annotations

from collections import defaultdict

from musicsync.domain.platforms import PLATFORMS
from musicsync.domain.track import KEY_SEP, Track


def _title_part(key: str) -> str:
    return key.split(KEY_SEP, 1)[0]


def _artist_part(key: str) -> bool:
    parts = key.split(KEY_SEP, 1)
    return len(parts) > 1 and bool(parts[1])


def _pick_canonical_key(
    keys: set[str],
    entries: list[tuple[str, str, Track]],
) -> str:
    with_artist = [k for k in keys if _artist_part(k)]
    if with_artist:
        for platform in PLATFORMS:
            for plat, key, _track in entries:
                if plat == platform and key in with_artist:
                    return key
        return sorted(with_artist)[0]
    return sorted(keys)[0]


def _merge_track_rows(left: Track, right: Track) -> Track:
    """Prefer non-empty metadata from either row."""
    return Track(
        name=left.name or right.name,
        artist=left.artist.strip() or right.artist.strip(),
        platform_id=left.platform_id or right.platform_id,
        added_at=left.added_at or right.added_at,
        album=left.album or right.album,
        artwork_url=left.artwork_url or right.artwork_url,
        duration_sec=left.duration_sec or right.duration_sec,
        year=left.year or right.year,
        isrc=left.isrc or right.isrc,
    )


def _to_canonical_track(track: Track, best_artist: str) -> Track:
    artist = track.artist.strip() or best_artist
    return Track(
        name=track.name,
        artist=artist,
        platform_id=track.platform_id,
        added_at=track.added_at,
        album=track.album,
        artwork_url=track.artwork_url,
        duration_sec=track.duration_sec,
        year=track.year,
        isrc=track.isrc,
    )


def canonicalize_presence(
    presence_by_platform: dict[str, dict[str, Track]],
) -> tuple[dict[str, dict[str, Track]], dict[str, str]]:
    """Collapse title-only keys when the same title has a known artist elsewhere.

    Returns remapped presence and ``old_key -> canonical_key`` for synced-key cleanup.
    """
    by_title: dict[str, list[tuple[str, str, Track]]] = defaultdict(list)
    for platform, tracks in presence_by_platform.items():
        for key, track in tracks.items():
            by_title[_title_part(key)].append((platform, key, track))

    key_remap: dict[str, str] = {}
    result: dict[str, dict[str, Track]] = {p: {} for p in presence_by_platform}

    for _title, entries in by_title.items():
        keys = {key for _plat, key, _track in entries}
        canonical = _pick_canonical_key(keys, entries)
        for key in keys:
            if key != canonical:
                key_remap[key] = canonical

        best_artist = next(
            (track.artist for _plat, _key, track in entries if track.artist.strip()),
            "",
        )

        for platform, _key, track in entries:
            canonical_track = _to_canonical_track(track, best_artist)
            bucket = result[platform]
            existing = bucket.get(canonical)
            bucket[canonical] = (
                _merge_track_rows(existing, canonical_track)
                if existing is not None
                else canonical_track
            )

    return result, key_remap


def remap_synced_keys(
    already_synced: dict[str, set[str]],
    key_remap: dict[str, str],
) -> dict[str, set[str]]:
    if not key_remap:
        return already_synced
    return {
        platform: {key_remap.get(key, key) for key in keys}
        for platform, keys in already_synced.items()
    }


def compute_to_sync(
    presence_by_platform: dict[str, dict[str, Track]],
    already_synced: dict[str, set[str]],
) -> dict[str, list[Track]]:
    """Return per-platform tracks to push: liked elsewhere but absent and not synced.

    For each target platform, a track is queued when it is liked on *any* other
    connected platform, is not already liked on the target, and has not been
    recorded as synced to the target.
    """
    platforms = list(presence_by_platform.keys())
    liked_keys: dict[str, set[str]] = {
        p: set(tracks.keys()) for p, tracks in presence_by_platform.items()
    }

    all_liked: set[str] = set()
    for keys in liked_keys.values():
        all_liked |= keys

    result: dict[str, list[Track]] = {p: [] for p in platforms}

    for target in platforms:
        target_liked = liked_keys.get(target, set())
        synced = already_synced.get(target, set())
        to_sync_keys = all_liked - target_liked - synced

        tracks_out: list[Track] = []
        for key in sorted(to_sync_keys):
            for source in platforms:
                if source == target:
                    continue
                source_tracks = presence_by_platform.get(source, {})
                if key in source_tracks:
                    tracks_out.append(source_tracks[key])
                    break
        result[target] = tracks_out

    return result


def sort_tracks_chronologically(tracks: list[Track]) -> list[Track]:
    """Oldest liked first — when applied to Tidal (prepend-newest UI), newest ends on top."""
    return sorted(tracks, key=lambda t: t.added_at or "")


def compute_tidal_catalog(
    presence_by_platform: dict[str, dict[str, Track]],
) -> list[Track]:
    """All tracks that belong on Tidal (liked on any connected platform).

    Sorted oldest-first by canonical *added_at* (Spotify → Apple → Tidal) so
    Tidal's prepend-newest favorites UI matches Spotify Liked Songs order.
    """
    all_liked: set[str] = set()
    for tracks in presence_by_platform.values():
        all_liked |= set(tracks.keys())

    catalog: list[Track] = []
    for key in all_liked:
        track = _track_for_tidal_catalog(key, presence_by_platform)
        if track is not None:
            catalog.append(track)

    return sort_tracks_chronologically(catalog)


def _track_for_tidal_catalog(
    key: str,
    presence_by_platform: dict[str, dict[str, Track]],
) -> Track | None:
    """Merge per-platform rows into one Track for Tidal apply/reorder."""
    name = ""
    artist = ""
    platform_id: str | None = None
    added_at: str | None = None
    album: str | None = None
    artwork_url: str | None = None
    duration_sec: int | None = None
    year: str | None = None
    isrc: str | None = None

    for platform in PLATFORMS:
        row = presence_by_platform.get(platform, {}).get(key)
        if row is None:
            continue
        if not name:
            name = row.name
            artist = row.artist
            album = row.album
            artwork_url = row.artwork_url
            duration_sec = row.duration_sec
            year = row.year
        if added_at is None and row.added_at:
            added_at = row.added_at
        if isrc is None and row.isrc:
            isrc = row.isrc
        if platform == "tidal" and row.platform_id:
            platform_id = row.platform_id

    if not name:
        return None

    return Track(
        name=name,
        artist=artist,
        platform_id=platform_id,
        added_at=added_at,
        album=album,
        artwork_url=artwork_url,
        duration_sec=duration_sec,
        year=year,
        isrc=isrc,
    )
