"""Pure Apple Music catalog candidate selection (no HTTP).

Given the songs Apple returns for an ISRC or a free-text search, pick the one
that is *exactly* the stored track — or nothing. Ambiguity always resolves to
None so the web app keeps its search-URL fallback instead of a wrong link.
"""

from __future__ import annotations

from dataclasses import dataclass

from musicsync.domain.track import (
    KEY_SEP,
    match_artist,
    normalize_recording_title,
    normalize_text,
)

DURATION_TOLERANCE_SEC = 3
"""Max |catalog - stored| duration for a candidate to count as the same recording."""


@dataclass(frozen=True)
class CatalogSong:
    """One song resource from the Apple Music catalog."""

    catalog_id: str
    name: str
    artist: str
    album: str | None = None
    duration_sec: int | None = None
    is_compilation: bool = False
    url: str | None = None


@dataclass(frozen=True)
class AppleLinkTarget:
    """A stored track awaiting an Apple catalog id (``key`` is the DB key, verbatim)."""

    key: str
    name: str
    artist: str
    album: str | None = None
    duration_sec: int | None = None
    isrc: str | None = None


def _duration_gap(song: CatalogSong, target: AppleLinkTarget) -> int | None:
    if song.duration_sec is None or target.duration_sec is None:
        return None
    return abs(song.duration_sec - target.duration_sec)


def _within_tolerance(song: CatalogSong, target: AppleLinkTarget) -> bool:
    gap = _duration_gap(song, target)
    return gap is None or gap <= DURATION_TOLERANCE_SEC


def _prefer_non_compilations(songs: list[CatalogSong]) -> list[CatalogSong]:
    originals = [s for s in songs if not s.is_compilation]
    return originals or songs


def _prefer_album_match(
    songs: list[CatalogSong], target: AppleLinkTarget
) -> list[CatalogSong]:
    if not target.album:
        return songs
    wanted = normalize_text(target.album)
    matching = [s for s in songs if s.album and normalize_text(s.album) == wanted]
    return matching or songs


def _closest_duration(songs: list[CatalogSong], target: AppleLinkTarget) -> CatalogSong:
    """Closest duration first; unknown durations last; ties keep Apple's order."""
    def rank(song: CatalogSong) -> int:
        gap = _duration_gap(song, target)
        return gap if gap is not None else DURATION_TOLERANCE_SEC + 1

    return min(songs, key=rank)


def _narrow(songs: list[CatalogSong], target: AppleLinkTarget) -> list[CatalogSong]:
    """Shared preference chain: in-tolerance → originals → same album."""
    acceptable = [s for s in songs if _within_tolerance(s, target)]
    return _prefer_album_match(_prefer_non_compilations(acceptable), target)


def _target_artist(target: AppleLinkTarget) -> str:
    return target.key.split(KEY_SEP, 1)[1] if KEY_SEP in target.key else ""


def _same_recording_title(song: CatalogSong, target: AppleLinkTarget) -> bool:
    return normalize_recording_title(song.name) == normalize_recording_title(target.name)


def _is_exact_search_hit(song: CatalogSong, target: AppleLinkTarget) -> bool:
    """Same primary artist as the stored key, same recording title, both durations known.

    The recording title is compared instead of the key's title half because the
    key keeps soundtrack tags ("- From \"Shrek 2\" Soundtrack") but strips live/
    remix markers; the recording title does the opposite, which is what we need.
    """
    artist = _target_artist(target)
    return (
        bool(artist)
        and match_artist(song.artist) == artist
        and _same_recording_title(song, target)
        and _duration_gap(song, target) is not None
    )


def select_isrc_match(
    candidates: list[CatalogSong], target: AppleLinkTarget
) -> CatalogSong | None:
    """Pick the catalog song for an ISRC lookup.

    The stored ISRC may come from a different variant than the stored title
    (the match key folds "Song - Remix" into "Song"), so a hit must carry the
    same recording title; otherwise None lets the caller fall back to search.
    Among those hits: the original album over a compilation, the stored album
    name, then the closest duration. A hit outside the tolerance is rejected.
    """
    same_title = [s for s in candidates if _same_recording_title(s, target)]
    remaining = _narrow(same_title, target)
    if not remaining:
        return None
    return _closest_duration(remaining, target)


def select_search_match(
    candidates: list[CatalogSong], target: AppleLinkTarget
) -> CatalogSong | None:
    """Pick a free-text search hit only when it is unambiguously the stored song.

    The match key strips "(Live)", "Remix", "Remastered"…, so a search hit must
    also match the full title with those markers kept, and both durations must
    be known and within tolerance. Anything less stays unresolved.
    """
    exact = [s for s in candidates if _is_exact_search_hit(s, target)]
    remaining = _narrow(exact, target)
    if not remaining:
        return None
    return _closest_duration(remaining, target)
