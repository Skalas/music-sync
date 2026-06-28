"""Domain ports (interfaces) for library providers and persistence."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from musicsync.domain.track import Track


@runtime_checkable
class LibraryProvider(Protocol):
    """Read liked tracks from a platform and optionally apply new likes."""

    name: str
    can_write: bool

    def read_liked(self) -> list[Track]:
        """Return all liked/favorite tracks from the remote library."""
        ...

    def apply_likes(self, tracks: list[Track]) -> list[Track]:
        """Apply likes on the platform. Returns tracks successfully synced."""
        ...


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

    def iter_export_rows(self) -> list[dict[str, str | int]]:
        """Pivot rows for CSV export: one row per track, columns per platform."""
        ...
