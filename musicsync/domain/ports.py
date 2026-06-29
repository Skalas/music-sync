"""Domain ports (interfaces) for library providers and persistence."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

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

    def apply_likes(self, tracks: list[Track]) -> list[Track]:
        """Apply likes on the platform. Returns tracks successfully synced."""
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
