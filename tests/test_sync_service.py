"""SyncService read-loop graceful-skip behavior (incl. SystemExit handling)."""

from __future__ import annotations

import pytest

from musicsync.application.sync_service import SyncOptions, SyncService
from musicsync.domain.track import Track


class _FakeRepo:
    """In-memory stand-in for TrackRepository (only what SyncService touches)."""

    def __init__(self) -> None:
        self.upserts: list[str] = []

    def migrate_state_json(self, path: str) -> bool:
        return False

    def clear_synced(self) -> None:
        pass

    def upsert_presence(self, platform: str, tracks: list[Track], *, liked: bool = True) -> None:
        self.upserts.append(platform)

    def get_liked_by_platform(self) -> dict[str, dict[str, Track]]:
        return {}

    def get_synced_keys(self, platform: str) -> set[str]:
        return set()

    def mark_synced(self, platform: str, keys: list[str], *, when: str) -> None:
        pass

    def iter_export_rows(self) -> list[dict[str, str | int | None]]:
        return []

    def iter_enriched_rows(
        self, *, sort_by: str | None = None
    ) -> list[dict[str, str | int | None]]:
        return []


class _SystemExitProvider:
    """Provider that calls sys.exit() on read (like SpotifyProvider w/o creds)."""

    can_write = True
    graceful_on_error = False

    def __init__(self, name: str = "spotify") -> None:
        self.name = name
        self.read_liked_calls = 0

    def read_liked(self) -> list[Track]:
        self.read_liked_calls += 1
        raise SystemExit("missing .env creds")

    def apply_likes(self, tracks: list[Track]) -> list[Track]:
        return tracks

    def write_review(self, tracks: list[Track], path: object) -> None:
        pass


def test_read_loop_skips_systemexit_when_flag_set() -> None:
    provider = _SystemExitProvider()
    service = SyncService(_FakeRepo(), [provider])

    result = service.run(
        SyncOptions(offline=False, dry_run=True, skip_unavailable_providers=True)
    )

    assert provider.read_liked_calls == 1
    assert "spotify" in result.skipped_providers


def test_read_loop_propagates_systemexit_when_flag_unset() -> None:
    """CLI default: a provider's sys.exit() must still terminate the run."""
    provider = _SystemExitProvider()
    service = SyncService(_FakeRepo(), [provider])

    with pytest.raises(SystemExit):
        service.run(
            SyncOptions(offline=False, dry_run=True, skip_unavailable_providers=False)
        )
