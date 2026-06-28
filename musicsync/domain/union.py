"""Pure N-way union logic for liked-track reconciliation."""

from __future__ import annotations

from musicsync.domain.track import Track


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


def reconcile_legacy(
    spotify: list[Track],
    apple: list[Track],
    state: dict[str, list[str]],
) -> tuple[list[Track], list[Track]]:
    """Pre-sprint 2-way reconcile — used only for backward-compat golden tests."""
    spotify_keys = {t.key for t in spotify}
    apple_keys = {t.key for t in apple}
    done_apple = set(state.get("apple", []))
    done_spotify = set(state.get("spotify", []))

    to_apple = [
        t for t in spotify if t.key not in apple_keys and t.key not in done_apple
    ]
    to_spotify = [
        t for t in apple if t.key not in spotify_keys and t.key not in done_spotify
    ]
    return to_apple, to_spotify
