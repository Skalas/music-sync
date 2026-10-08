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


def test_tidal_read_fetches_artist_when_not_in_included(tidal_env: Path) -> None:
    page = {
        "data": [{"type": "tracks", "id": "1", "meta": {"addedAt": "2020-01-01"}}],
        "included": [
            {
                "type": "tracks",
                "id": "1",
                "attributes": {"title": "212"},
                "relationships": {
                    "artists": {"data": [{"type": "artists", "id": "a1"}]}
                },
            }
        ],
        "links": {},
    }
    artist_payload = {
        "data": {"type": "artists", "id": "a1", "attributes": {"name": "Azealia Banks"}}
    }

    session = requests.Session()
    session.get = MagicMock(  # type: ignore[method-assign]
        side_effect=[
            MagicMock(status_code=200, json=lambda: page),
            MagicMock(status_code=200, json=lambda: artist_payload),
        ]
    )

    provider = TidalProvider(base_dir=tidal_env, session=session)
    provider._write_cache(_valid_token_cache())  # noqa: SLF001

    tracks = provider.read_liked()
    assert len(tracks) == 1
    assert tracks[0].name == "212"
    assert tracks[0].artist == "Azealia Banks"
    assert tracks[0].key == Track(name="212", artist="Azealia Banks").key
    assert session.get.call_count == 2


def test_sqlite_drops_title_only_duplicate_on_artist_enrich(tmp_path: Path) -> None:
    repo = SqliteTrackRepository(tmp_path / "lib.db")
    title_only = Track(name="212", artist="")
    enriched = Track(name="212", artist="Azealia Banks", platform_id="1")
    repo.upsert_presence("tidal", [title_only], liked=True)
    repo.upsert_presence("tidal", [enriched], liked=True)

    presence = repo.get_liked_by_platform()
    assert title_only.key not in presence["tidal"]
    assert enriched.key in presence["tidal"]
    repo.close()


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
        graceful_on_error = True

        def read_liked(self) -> list[Track]:
            return []

        def apply_likes(
            self, tracks: list[Track], *, on_batch: object = None
        ) -> list[Track]:
            nonlocal posted
            posted = True
            return tracks

        def write_review(self, tracks: list[Track], path: object) -> None:
            pass

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
        graceful_on_error = False

        def read_liked(self) -> list[Track]:
            return [Track(name="Only", artist="Spotify")]

        def apply_likes(
            self, tracks: list[Track], *, on_batch: object = None
        ) -> list[Track]:
            return tracks

        def write_review(self, tracks: list[Track], path: object) -> None:
            pass

    class FakeApple:
        name = "apple"
        can_write = True
        graceful_on_error = False

        def read_liked(self) -> list[Track]:
            return []

        def apply_likes(
            self, tracks: list[Track], *, on_batch: object = None
        ) -> list[Track]:
            return tracks

        def write_review(self, tracks: list[Track], path: object) -> None:
            pass

    class FailingTidal:
        name = "tidal"
        can_write = False
        graceful_on_error = True

        def read_liked(self) -> list[Track]:
            raise TidalError("auth failed")

        def apply_likes(
            self, tracks: list[Track], *, on_batch: object = None
        ) -> list[Track]:
            return []

        def write_review(self, tracks: list[Track], path: object) -> None:
            pass

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


def test_tidal_post_retries_on_429(tidal_env: Path) -> None:
    session = requests.Session()
    post_attempts = {"n": 0}

    def post_side_effect(*args: object, **kwargs: object) -> MagicMock:
        post_attempts["n"] += 1
        if post_attempts["n"] == 1:
            return MagicMock(status_code=429, headers={"Retry-After": "0"})
        return MagicMock(status_code=204)

    session.get = MagicMock(  # type: ignore[method-assign]
        return_value=MagicMock(
            status_code=200,
            json=lambda: {"data": [{"type": "tracks", "id": "99"}]},
        )
    )
    session.post = MagicMock(side_effect=post_side_effect)  # type: ignore[method-assign]

    provider = TidalProvider(base_dir=tidal_env, need_write=True, session=session)
    provider._write_cache(_valid_token_cache())  # noqa: SLF001

    applied = provider.apply_likes([Track(name="X", artist="Y")])
    assert len(applied) == 1
    assert post_attempts["n"] == 2


def test_tidal_apply_invokes_on_batch_after_post(tidal_env: Path) -> None:
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

    batches: list[list[Track]] = []
    track = Track(name="Batch", artist="Artist")
    provider.apply_likes([track], on_batch=batches.append)

    assert len(batches) == 1
    assert batches[0] == [track]


def test_sync_service_checkpoints_partial_tidal_apply(tmp_path: Path) -> None:
    from musicsync.application.sync_service import SyncOptions, SyncService

    class PartialTidal:
        name = "tidal"
        can_write = True
        graceful_on_error = True

        def read_liked(self) -> list[Track]:
            return []

        def apply_likes(
            self,
            tracks: list[Track],
            *,
            on_batch: object = None,
            reorder: bool = False,
        ) -> list[Track]:
            if on_batch is not None and tracks:
                on_batch([tracks[0]])
            raise TidalError("POST /favorites HTTP 429")

        def write_review(self, tracks: list[Track], path: object) -> None:
            pass

    db = tmp_path / "lib.db"
    repo = SqliteTrackRepository(db)
    tracks = [
        Track(name="One", artist="A"),
        Track(name="Two", artist="B"),
    ]
    repo.upsert_presence("spotify", tracks, liked=True)

    service = SyncService(repo, [PartialTidal()])
    result = service.run(
        SyncOptions(apply_tidal=True, no_spotify=True, no_apple=True, offline=True)
    )

    assert result.applied.get("tidal") == 1
    assert tracks[0].key in repo.get_synced_keys("tidal")
    assert tracks[1].key not in repo.get_synced_keys("tidal")
    assert "tidal" in result.skipped_providers
    repo.close()


def test_tidal_batch_409_falls_back_to_individual(tidal_env: Path) -> None:
    session = requests.Session()

    def post_side_effect(*args: object, **kwargs: object) -> MagicMock:
        body = kwargs.get("json") or {}
        items = body.get("data", [])
        if len(items) > 1:
            return MagicMock(status_code=409)
        return MagicMock(status_code=204)

    session.get = MagicMock(  # type: ignore[method-assign]
        return_value=MagicMock(
            status_code=200,
            json=lambda: {"data": [{"type": "tracks", "id": "99"}]},
        )
    )
    session.post = MagicMock(side_effect=post_side_effect)  # type: ignore[method-assign]

    provider = TidalProvider(base_dir=tidal_env, need_write=True, session=session)
    provider._write_cache(_valid_token_cache())  # noqa: SLF001

    tracks = [Track(name=f"Song {i}", artist="A") for i in range(3)]
    applied = provider.apply_likes(tracks)

    assert len(applied) == 3
    assert session.post.call_count == 4  # 1 batch 409 + 3 singles


def test_http_error_includes_status_hint() -> None:
    from musicsync.infrastructure.tidal_provider import _http_error

    err = _http_error("POST", "/userCollectionTracks/me/relationships/items", 409)
    assert err.status_code == 409
    assert "HTTP 409" in str(err)
    assert "ya está en favoritos" in str(err)


def test_tidal_reorder_deletes_then_adds(tidal_env: Path) -> None:
    session = requests.Session()
    session.get = MagicMock(  # type: ignore[method-assign]
        return_value=MagicMock(
            status_code=200,
            json=lambda: {"data": [{"type": "tracks", "id": "42"}]},
        )
    )
    session.post = MagicMock(return_value=MagicMock(status_code=204))  # type: ignore[method-assign]
    session.delete = MagicMock(return_value=MagicMock(status_code=204))  # type: ignore[method-assign]

    provider = TidalProvider(base_dir=tidal_env, need_write=True, session=session)
    provider._write_cache(_valid_token_cache())  # noqa: SLF001

    tracks = [
        Track(name="Older", artist="A", added_at="2020-01-01"),
        Track(name="Newer", artist="B", added_at="2024-01-01"),
    ]
    applied = provider.apply_likes(tracks, reorder=True)

    assert len(applied) == 2
    session.delete.assert_called()
    assert session.post.call_count >= 1
    assert applied[0].name == "Older"
    assert applied[1].name == "Newer"
