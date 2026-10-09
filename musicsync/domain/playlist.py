"""Playlist model and the pure per-playlist union used for mirroring.

Playlists match across platforms by name (case-insensitive, trimmed); tracks
inside a playlist match by the same ``normalize_key`` the liked-songs union uses.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace

from musicsync.domain.platforms import PLATFORMS
from musicsync.domain.track import KEY_SEP, Track, match_artist

_WS_RE = re.compile(r"\s+")


def normalize_playlist_name(name: str) -> str:
    """Cross-platform playlist identity: trimmed, whitespace-collapsed, casefolded."""
    return _WS_RE.sub(" ", name or "").strip().casefold()


@dataclass(frozen=True)
class PlaylistSummary:
    """A playlist as listed by a platform, before its tracks are read."""

    platform: str
    name: str
    remote_id: str | None = None
    track_count: int = 0
    owned: bool = True
    """False for a followed / other user's playlist: never read nor written."""

    @property
    def key(self) -> str:
        return normalize_playlist_name(self.name)


@dataclass(frozen=True)
class Playlist:
    """A platform playlist with its tracks (order kept, duplicates allowed)."""

    platform: str
    name: str
    remote_id: str | None = None
    tracks: tuple[Track, ...] = ()

    @property
    def key(self) -> str:
        return normalize_playlist_name(self.name)

    @property
    def track_keys(self) -> set[str]:
        return {track.key for track in self.tracks}


@dataclass(frozen=True)
class PlaylistAddResult:
    """Outcome of one additive write to a target playlist."""

    remote_id: str | None
    added: list[Track] = field(default_factory=list)
    """Tracks now in the target playlist (newly added or already there)."""

    unresolved: list[Track] = field(default_factory=list)
    """Tracks the platform could not resolve; they go to the review file."""


@dataclass
class PlaylistDiff:
    """Per-playlist mirror plan: what each target platform is missing."""

    name: str
    to_add: dict[str, list[Track]] = field(default_factory=dict)
    unresolved: list[Track] = field(default_factory=list)
    """Title-only tracks (no artist anywhere) missing on a target: never written,
    they go to review — a search without artist could add the wrong song."""

    @property
    def total(self) -> int:
        return sum(len(tracks) for tracks in self.to_add.values())


def _ordered_platforms(platforms: Iterable[str]) -> list[str]:
    known = [p for p in PLATFORMS if p in platforms]
    return known + sorted(p for p in platforms if p not in PLATFORMS)


def _group_by_name(
    playlists_by_platform: Mapping[str, Sequence[Playlist]],
) -> dict[str, dict[str, Playlist]]:
    """{name_key: {platform: Playlist}} — first playlist wins on a name clash."""
    grouped: dict[str, dict[str, Playlist]] = {}
    for platform in _ordered_platforms(playlists_by_platform):
        for playlist in playlists_by_platform[platform]:
            grouped.setdefault(playlist.key, {}).setdefault(platform, playlist)
    return grouped


def _title_part(key: str) -> str:
    return key.split(KEY_SEP, 1)[0]


def has_artist(track: Track) -> bool:
    """False for a title-only match key (``"title␟"``): unsafe to write anywhere."""
    parts = track.key.split(KEY_SEP, 1)
    return len(parts) > 1 and bool(parts[1])


def _merge_title_only(by_platform: Mapping[str, Playlist]) -> dict[str, Playlist]:
    """Give title-only tracks the artist of a same-title track in this playlist group.

    Mirrors the liked-songs title-only canonicalization (domain/union.py), but
    keeps each playlist's order, which ``canonicalize_presence`` does not. Only
    an unambiguous title merges: when the group holds the title by two different
    artists ("Hello" by Adele and by Lionel Richie), the title-only track stays
    title-only and ends up unresolved rather than guessed.
    """
    artists_by_title: dict[str, dict[str, str]] = {}
    for platform in _ordered_platforms(by_platform):
        for track in by_platform[platform].tracks:
            if has_artist(track):
                by_artist = artists_by_title.setdefault(_title_part(track.key), {})
                by_artist.setdefault(match_artist(track.artist), track.artist)
    artist_by_title = {
        title: next(iter(by_artist.values()))
        for title, by_artist in artists_by_title.items()
        if len(by_artist) == 1
    }

    def canonical(track: Track) -> Track:
        artist = artist_by_title.get(_title_part(track.key))
        return track if has_artist(track) or not artist else replace(track, artist=artist)

    return {
        platform: replace(playlist, tracks=tuple(canonical(t) for t in playlist.tracks))
        for platform, playlist in by_platform.items()
    }


def _union_tracks(by_platform: Mapping[str, Playlist]) -> list[Track]:
    """Every distinct track across the playlist's platforms, in source order."""
    seen: set[str] = set()
    union: list[Track] = []
    for platform in _ordered_platforms(by_platform):
        for track in by_platform[platform].tracks:
            if track.key not in seen:
                seen.add(track.key)
                union.append(track)
    return union


def _display_name(by_platform: Mapping[str, Playlist]) -> str:
    return by_platform[_ordered_platforms(by_platform)[0]].name


def compute_playlist_union(
    playlists_by_platform: Mapping[str, Sequence[Playlist]],
    already_synced: Mapping[str, Mapping[str, set[str]]],
    targets: Iterable[str],
) -> dict[str, PlaylistDiff]:
    """Return ``{playlist_name_key: PlaylistDiff}`` for every playlist name seen.

    For each target platform, a track is queued when it is in the same-named
    playlist on any other platform, is absent from the target's playlist (which
    may not exist yet), and was not already recorded as synced to it
    (*already_synced* is ``{name_key: {platform: track_keys}}``). Additive only.
    Title-only tracks are first merged into a same-title track with an artist;
    any left without an artist are reported in ``unresolved``, never queued.
    """
    target_list = list(targets)
    diffs: dict[str, PlaylistDiff] = {}
    for name_key, grouped in _group_by_name(playlists_by_platform).items():
        by_platform = _merge_title_only(grouped)
        union = _union_tracks(by_platform)
        synced = already_synced.get(name_key, {})
        diff = PlaylistDiff(name=_display_name(by_platform))
        unresolved: dict[str, Track] = {}
        for target in target_list:
            present = by_platform[target].track_keys if target in by_platform else set()
            skip = present | synced.get(target, set())
            missing = [t for t in union if t.key not in skip]
            diff.to_add[target] = [t for t in missing if has_artist(t)]
            unresolved.update((t.key, t) for t in missing if not has_artist(t))
        diff.unresolved = list(unresolved.values())
        diffs[name_key] = diff
    return diffs
