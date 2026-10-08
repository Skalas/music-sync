"""Spotify provider write-scope and error handling."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from requests.exceptions import RequestException
from spotipy.exceptions import SpotifyException

from musicsync.domain.errors import PlatformOperationError
from musicsync.domain.track import Track
from musicsync.infrastructure.spotify_provider import (
    SPOTIFY_ADD_BATCH,
    SpotifyProvider,
    _with_retries,
)


def test_apply_likes_requires_write_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPOTIPY_CLIENT_ID", "id")
    monkeypatch.setenv("SPOTIPY_CLIENT_SECRET", "secret")
    monkeypatch.setenv("SPOTIPY_REDIRECT_URI", "http://127.0.0.1:8080")
    (tmp_path / ".env").write_text("", encoding="utf-8")

    provider = SpotifyProvider(
        base_dir=tmp_path,
        unmatched_log_path=tmp_path / "unmatched.log",
        need_write=False,
    )

    with pytest.raises(PlatformOperationError, match="user-library-modify"):
        provider.apply_likes([Track(name="A", artist="B")])


def test_apply_likes_maps_403_scope_to_platform_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SPOTIPY_CLIENT_ID", "id")
    monkeypatch.setenv("SPOTIPY_CLIENT_SECRET", "secret")
    monkeypatch.setenv("SPOTIPY_REDIRECT_URI", "http://127.0.0.1:8080")
    (tmp_path / ".env").write_text("", encoding="utf-8")

    sp = MagicMock()
    sp.search.return_value = {
        "tracks": {
            "items": [
                {
                    "id": "tid",
                    "name": "A",
                    "artists": [{"name": "B"}],
                    "external_urls": {"spotify": "https://open.spotify.com/track/tid"},
                }
            ]
        }
    }
    sp.current_user_saved_tracks_add.side_effect = SpotifyException(
        403, -1, "https://api.spotify.com/v1/me/library", "Insufficient client scope"
    )

    provider = SpotifyProvider(
        base_dir=tmp_path,
        unmatched_log_path=tmp_path / "unmatched.log",
        need_write=True,
        client=sp,
    )

    with pytest.raises(PlatformOperationError, match="HTTP 403"):
        provider.apply_likes([Track(name="A", artist="B")])


def test_apply_likes_batches_at_40_tracks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SPOTIPY_CLIENT_ID", "id")
    monkeypatch.setenv("SPOTIPY_CLIENT_SECRET", "secret")
    monkeypatch.setenv("SPOTIPY_REDIRECT_URI", "http://127.0.0.1:8080")
    (tmp_path / ".env").write_text("", encoding="utf-8")

    sp = MagicMock()
    sp.search.return_value = {
        "tracks": {
            "items": [
                {
                    "id": "tid",
                    "name": "A",
                    "artists": [{"name": "B"}],
                    "external_urls": {"spotify": "https://open.spotify.com/track/tid"},
                }
            ]
        }
    }
    sp.current_user_saved_tracks_add.return_value = None

    provider = SpotifyProvider(
        base_dir=tmp_path,
        unmatched_log_path=tmp_path / "unmatched.log",
        need_write=True,
        client=sp,
    )
    tracks = [Track(name=f"Song {i}", artist="Band") for i in range(45)]
    applied = provider.apply_likes(tracks)

    assert len(applied) == 45
    assert sp.current_user_saved_tracks_add.call_count == 2
    first_batch = sp.current_user_saved_tracks_add.call_args_list[0].kwargs["tracks"]
    second_batch = sp.current_user_saved_tracks_add.call_args_list[1].kwargs["tracks"]
    assert len(first_batch) == 40
    assert len(second_batch) == 5


def test_with_retries_re_raises_spotify_429_after_exhausted() -> None:
    from musicsync.infrastructure.spotify_provider import MAX_RETRIES

    sp_exc = SpotifyException(
        429,
        -1,
        "https://api.spotify.com/v1/search",
        "rate limited",
        headers={"Retry-After": "0"},
    )
    fn = MagicMock(side_effect=sp_exc)

    with pytest.raises(SpotifyException):
        _with_retries(fn)

    assert fn.call_count == MAX_RETRIES


def test_search_track_id_waits_through_429(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SPOTIPY_CLIENT_ID", "id")
    monkeypatch.setenv("SPOTIPY_CLIENT_SECRET", "secret")
    monkeypatch.setenv("SPOTIPY_REDIRECT_URI", "http://127.0.0.1:8080")
    (tmp_path / ".env").write_text("", encoding="utf-8")

    sp = MagicMock()
    sp_exc = SpotifyException(
        429,
        -1,
        "https://api.spotify.com/v1/search",
        "rate limited",
        headers={"Retry-After": "0"},
    )
    sp.search.side_effect = [
        sp_exc,
        {
            "tracks": {
                "items": [
                    {
                        "id": "tid",
                        "name": "A",
                        "artists": [{"name": "B"}],
                    }
                ]
            }
        },
    ]

    provider = SpotifyProvider(
        base_dir=tmp_path,
        unmatched_log_path=tmp_path / "unmatched.log",
        need_write=True,
        client=sp,
    )
    monkeypatch.setattr(
        "musicsync.infrastructure.spotify_provider.SPOTIFY_SEARCH_DELAY_SEC",
        0,
    )

    track_id = provider._search_track_id(sp, Track(name="A", artist="B"))  # noqa: SLF001

    assert track_id == "tid"
    assert sp.search.call_count == 2


def test_apply_likes_checkpoints_each_add_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SPOTIPY_CLIENT_ID", "id")
    monkeypatch.setenv("SPOTIPY_CLIENT_SECRET", "secret")
    monkeypatch.setenv("SPOTIPY_REDIRECT_URI", "http://127.0.0.1:8080")
    (tmp_path / ".env").write_text("", encoding="utf-8")

    sp = MagicMock()
    sp.search.return_value = {
        "tracks": {
            "items": [
                {
                    "id": "tid",
                    "name": "A",
                    "artists": [{"name": "B"}],
                }
            ]
        }
    }
    add_calls = {"n": 0}

    def add_side_effect(*_args: object, **kwargs: object) -> None:
        add_calls["n"] += 1
        if add_calls["n"] == 1:
            return None
        raise SpotifyException(403, -1, "url", "scope")

    sp.current_user_saved_tracks_add.side_effect = add_side_effect

    provider = SpotifyProvider(
        base_dir=tmp_path,
        unmatched_log_path=tmp_path / "unmatched.log",
        need_write=True,
        client=sp,
    )
    monkeypatch.setattr(
        "musicsync.infrastructure.spotify_provider.SPOTIFY_SEARCH_DELAY_SEC",
        0,
    )

    tracks = [Track(name=f"Song {i}", artist="Band") for i in range(SPOTIFY_ADD_BATCH + 5)]
    batches: list[list[Track]] = []

    with pytest.raises(PlatformOperationError, match="HTTP 403"):
        provider.apply_likes(tracks, on_batch=batches.append)

    assert len(batches) == 1
    assert len(batches[0]) == SPOTIFY_ADD_BATCH


def test_match_on_spotify_survives_transient_search_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SPOTIPY_CLIENT_ID", "id")
    monkeypatch.setenv("SPOTIPY_CLIENT_SECRET", "secret")
    monkeypatch.setenv("SPOTIPY_REDIRECT_URI", "http://127.0.0.1:8080")
    (tmp_path / ".env").write_text("", encoding="utf-8")

    sp = MagicMock()
    sp.search.side_effect = [
        RequestException("network"),
        {
            "tracks": {
                "items": [
                    {
                        "id": "tid",
                        "name": "A",
                        "artists": [{"name": "B"}],
                        "external_urls": {"spotify": "https://open.spotify.com/track/tid"},
                    }
                ]
            }
        },
    ]

    provider = SpotifyProvider(
        base_dir=tmp_path,
        unmatched_log_path=tmp_path / "unmatched.log",
        need_write=True,
        client=sp,
    )
    monkeypatch.setattr(
        "musicsync.infrastructure.spotify_provider.SPOTIFY_SEARCH_DELAY_SEC",
        0,
    )

    candidates = provider._match_on_spotify(sp, [Track(name="A", artist="B")])  # noqa: SLF001

    assert candidates[0]["match"] is not None
    assert sp.search.call_count == 2
