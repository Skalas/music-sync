"""Domain ports (interfaces) for library providers and persistence."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Protocol, runtime_checkable

from musicsync.domain.playlist import Playlist, PlaylistAddResult, PlaylistSummary
from musicsync.domain.track import Track


@runtime_checkable
class LibraryProvider(Protocol):
    """Read liked tracks from a platform and optionally apply new likes."""

    name: str
    can_write: bool
    graceful_on_error: bool
    """True → a read or apply failure is logged and the platform is skipped;
    False → the exception propagates to the caller."""

    def read_liked(self) -> list[Track]:
        """Return all liked/favorite tracks from the remote library."""
        ...

    def apply_likes(
        self,
        tracks: list[Track],
        *,
        on_batch: Callable[[list[Track]], None] | None = None,
        reorder: bool = False,
    ) -> list[Track]:
        """Apply likes on the platform. Returns tracks successfully synced.

        *on_batch* is invoked after each durable batch (when supported) so the
        caller can checkpoint ``synced_at`` before the full run finishes.

        *reorder* (Tidal only): remove then re-add in *tracks* order so the
        remote favorites list matches chronological intent.
        """
        ...

    def write_review(self, tracks: list[Track], path: Path) -> None:
        """Write a review file of candidate tracks.

        Providers without a review flow inherit this no-op rather than raising
        AttributeError, so omitting the method degrades gracefully.
        """
        pass


@runtime_checkable
class TrackRepository(Protocol):
    """SQLite-backed source of truth for track presence and sync state."""

    def upsert_presence(
        self,
        platform: str,
        tracks: list[Track],
        *,
        liked: bool = True,
    ) -> None:
        """Idempotently upsert tracks and their presence for a platform."""
        ...

    def get_liked_by_platform(self) -> dict[str, dict[str, Track]]:
        """Return {platform: {key: Track}} for all liked presence rows."""
        ...

    def get_synced_keys(self, platform: str) -> set[str]:
        """Return keys already recorded as synced to *platform*."""
        ...

    def mark_synced(self, platform: str, keys: list[str], *, when: str) -> None:
        """Record successful apply for the given keys on *platform*."""
        ...

    def clear_synced(self) -> None:
        """Reset all synced_at timestamps (--full mode)."""
        ...

    def migrate_state_json(self, path: str) -> bool:
        """One-time import from legacy state.json. Returns True if migrated."""
        ...

    def iter_export_rows(self) -> list[dict[str, str | int | None]]:
        """Pivot rows for CSV export: one row per track, columns per platform."""
        ...

    def iter_enriched_rows(
        self, *, sort_by: str | None = None
    ) -> list[dict[str, str | int | None]]:
        """Enriched rows with per-platform presence, deep-link ids, metadata, and added_at.

        When sort_by='added_at', rows are ordered most-recently-added first.
        """
        ...

    def dedupe_title_only_keys(self) -> int:
        """Merge title-only keys into canonical keys with the same normalized title."""
        ...


@runtime_checkable
class PlaylistProvider(Protocol):
    """Read and (optionally) additively write user playlists on a platform.

    Capability flags let the application skip a platform without checking its
    name: ``can_playlist_read`` gates listing/reading, ``can_playlist_write``
    gates being a mirror target.
    """

    name: str
    can_playlist_read: bool
    can_playlist_write: bool
    reserved_playlist_names: frozenset[str]
    """Normalized names of the platform's system playlists: a wanted name in it
    is ambiguous there (never read, written, or created as a user playlist)."""

    def list_playlists(self) -> list[PlaylistSummary]:
        """Return the user's plain playlists (no smart/system ones).

        Followed/others' playlists may be included with ``owned=False`` so the
        application can detect a name clash; they are never read or written.
        """
        ...

    def read_playlist(self, summary: PlaylistSummary) -> Playlist:
        """Return *summary*'s playlist with its tracks."""
        ...

    def add_to_playlist(
        self, name: str, remote_id: str | None, tracks: list[Track]
    ) -> PlaylistAddResult:
        """Add *tracks* to playlist *name* (created when *remote_id* is None).

        Additive only: never removes, never duplicates a track already there.
        """
        ...


@runtime_checkable
class PlaylistRepository(Protocol):
    """SQLite-backed playlist membership and mirror state."""

    def upsert_playlist(self, playlist: Playlist) -> None:
        """Idempotently record *playlist* and the tracks currently in it."""
        ...

    def get_playlists(self, name_keys: set[str]) -> dict[str, list[Playlist]]:
        """Return ``{platform: [Playlist]}`` for the given normalized names."""
        ...

    def get_playlist_synced_keys(
        self, name_keys: set[str]
    ) -> dict[str, dict[str, set[str]]]:
        """Return ``{name_key: {platform: track_keys already mirrored there}}``."""
        ...

    def mark_playlist_synced(
        self,
        platform: str,
        name: str,
        remote_id: str | None,
        tracks: list[Track],
        *,
        when: str,
    ) -> None:
        """Record that *tracks* were mirrored into *platform*'s playlist *name*."""
        ...
