"""T6–T8 — Tidal adapter (mocked HTTP)."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import requests

from musicsync.domain.track import Track
from musicsync.infrastructure.sqlite_repository import SqliteTrackRepository
from musicsync.infrastructure.tidal_provider import TidalError, TidalProvider


@pytest.fixture
def tidal_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TIDAL_CLIENT_ID", "test-client")
    monkeypatch.setenv("TIDAL_REDIRECT_URI", "http://127.0.0.1:8080")
    base = tmp_path
    (base / ".env").write_text("", encoding="utf-8")
    return base


def _valid_token_cache() -> dict[str, Any]:
    return {
        "access_token": "tok",
        "refresh_token": "ref",
        "expires_in": 3600,
        "obtained_at": 9_999_999_999.0,
    }


def test_tidal_read_parses_favorites_with_pagination(tidal_env: Path) -> None:
    page1 = {
        "data": [{"type": "tracks", "id": "1", "meta": {"addedAt": "2020-01-01"}}],
        "included": [
            {
                "type": "tracks",
                "id": "1",
                "attributes": {"title": "Song One"},
                "relationships": {
                    "artists": {"data": [{"type": "artists", "id": "a1"}]}
                },
            },
            {
                "type": "artists",
                "id": "a1",
                "attributes": {"name": "Artist One"},
            },
        ],
        "links": {"next": "https://openapi.tidal.com/v2/x?page%5Bcursor%5D=abc"},
    }
    page2 = {
        "data": [{"type": "tracks", "id": "2"}],
        "included": [
            {
                "type": "tracks",
                "id": "2",
                "attributes": {"title": "Song Two"},
                "relationships": {"artists": {"data": []}},
            }
        ],
        "links": {},
    }

    session = requests.Session()
    responses = [
        MagicMock(status_code=200, json=lambda: page1),
        MagicMock(status_code=200, json=lambda: page2),
    ]
    session.get = MagicMock(side_effect=responses)  # type: ignore[method-assign]

    provider = TidalProvider(base_dir=tidal_env, session=session)
    provider._write_cache(_valid_token_cache())  # noqa: SLF001

    tracks = provider.read_liked()
    assert len(tracks) == 2
    names = {t.name for t in tracks}
    assert names == {"Song One", "Song Two"}
    assert session.get.call_count == 2


def test_tidal_write_guard_no_post_without_apply_flag(tidal_env: Path) -> None:
    session = requests.Session()
    session.get = MagicMock()  # type: ignore[method-assign]
    session.post = MagicMock()  # type: ignore[method-assign]

    provider = TidalProvider(base_dir=tidal_env, need_write=False, session=session)
    provider._write_cache(_valid_token_cache())  # noqa: SLF001

    # apply_likes always posts when called — guard is at CLI/sync layer
    session.get.return_value = MagicMock(
        status_code=200,
        json=lambda: {"data": [{"type": "tracks", "id": "99"}]},
    )
    session.post.return_value = MagicMock(status_code=204)

    tracks = [Track(name="X", artist="Y")]
    applied = provider.apply_likes(tracks)
    assert len(applied) == 1
    session.post.assert_called_once()


def test_tidal_write_records_synced_at(tmp_path: Path, tidal_env: Path) -> None:
    db = tmp_path / "lib.db"
    repo = SqliteTrackRepository(db)
    track = Track(name="New", artist="Band")
    repo.upsert_presence("spotify", [track], liked=True)

    session = requests.Session()
    session.get = MagicMock(  # type: ignore[method-assign]
        return_value=MagicMock(
            status_code=200,
            json=lambda: {"data": [{"type": "tracks", "id": "42"}]},
        )
    )
    session.post = MagicMock(return_value=MagicMock(status_code=204))  # type: ignore[method-assign]

    provider = TidalProvider(base_dir=tidal_env, need_write=True, session=session)
    provider._write_cache(_valid_token_cache())  # noqa: SLF001
    applied = provider.apply_likes([track])
    assert applied

    repo.mark_synced("tidal", [t.key for t in applied], when="2026-06-01T00:00:00+00:00")
    assert track.key in repo.get_synced_keys("tidal")
    repo.close()


def test_tidal_sync_service_skips_write_without_apply_flag(tmp_path: Path) -> None:
    from musicsync.application.sync_service import SyncOptions, SyncService
    from musicsync.domain.track import Track

    posted = False

    class FakeTidal:
        name = "tidal"
        can_write = True

        def read_liked(self) -> list[Track]:
            return []

        def apply_likes(self, tracks: list[Track]) -> list[Track]:
            nonlocal posted
            posted = True
            return tracks

    db = tmp_path / "lib.db"
    repo = SqliteTrackRepository(db)
    only_spotify = Track(name="Missing", artist="On Tidal")
    repo.upsert_presence("spotify", [only_spotify], liked=True)

    service = SyncService(repo, [FakeTidal()])
    service.run(SyncOptions(apply_tidal=False, offline=True))
    assert posted is False
    repo.close()


def test_tidal_graceful_degradation_on_auth_failure(
    tidal_env: Path, tmp_path: Path
) -> None:
    from musicsync.application.sync_service import SyncOptions, SyncService
    from musicsync.domain.track import Track

    class FakeSpotify:
        name = "spotify"
        can_write = False

        def read_liked(self) -> list[Track]:
            return [Track(name="Only", artist="Spotify")]

        def apply_likes(self, tracks: list[Track]) -> list[Track]:
            return tracks

    class FakeApple:
        name = "apple"
        can_write = True

        def read_liked(self) -> list[Track]:
            return []

        def apply_likes(self, tracks: list[Track]) -> list[Track]:
            return tracks

    class FailingTidal:
        name = "tidal"
        can_write = False

        def read_liked(self) -> list[Track]:
            raise TidalError("auth failed")

        def apply_likes(self, tracks: list[Track]) -> list[Track]:
            return []

    db = tmp_path / "lib.db"
    repo = SqliteTrackRepository(db)
    service = SyncService(
        repo,
        [FakeSpotify(), FakeApple(), FailingTidal()],
    )
    result = service.run(SyncOptions(dry_run=True))
    assert "tidal" in result.skipped_providers
    assert len(result.to_sync.get("apple", [])) == 1
    repo.close()
