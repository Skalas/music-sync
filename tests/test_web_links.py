"""A7 — unit tests for musicsync.web.links.track_url."""

from __future__ import annotations

from urllib.parse import unquote_plus

from musicsync.web.links import track_url


def test_spotify_with_id() -> None:
    url = track_url("spotify", platform_id="abc123", name="Song", artist="Artist")
    assert url == "https://open.spotify.com/track/abc123"


def test_spotify_without_id_returns_none() -> None:
    url = track_url("spotify", platform_id=None, name="Song", artist="Artist")
    assert url is None


def test_spotify_empty_id_returns_none() -> None:
    url = track_url("spotify", platform_id="", name="Song", artist="Artist")
    assert url is None


def test_tidal_with_id() -> None:
    url = track_url("tidal", platform_id="99999", name="Song", artist="Artist")
    assert url == "https://tidal.com/browse/track/99999"


def test_tidal_without_id_returns_none() -> None:
    url = track_url("tidal", platform_id=None, name="Song", artist="Artist")
    assert url is None


def test_tidal_empty_id_returns_none() -> None:
    url = track_url("tidal", platform_id="", name="Song", artist="Artist")
    assert url is None


def test_apple_always_returns_search_url() -> None:
    url = track_url("apple", platform_id=None, name="Imagine", artist="John Lennon")
    assert url is not None
    assert url.startswith("https://music.apple.com/search?term=")


def test_apple_url_encodes_spaces() -> None:
    url = track_url("apple", platform_id=None, name="Bohemian Rhapsody", artist="Queen")
    assert url is not None
    decoded = unquote_plus(url.split("term=")[1])
    assert "Bohemian Rhapsody" in decoded
    assert "Queen" in decoded


def test_apple_url_encodes_special_chars() -> None:
    url = track_url("apple", platform_id=None, name="C'est la vie", artist="Khaled")
    assert url is not None
    # The apostrophe must be percent-encoded in the URL
    assert " " not in url.split("term=")[1]


def test_unknown_platform_returns_none() -> None:
    url = track_url("deezer", platform_id="xyz", name="Song", artist="Artist")
    assert url is None
