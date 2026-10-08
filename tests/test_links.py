"""T7 — Apple Music deep links from the stored catalog id."""

from __future__ import annotations

from musicsync.web.links import track_url


def test_apple_catalog_id_builds_song_url() -> None:
    url = track_url(
        "apple", platform_id="1440818839", storefront="sv", name="Song", artist="Artist"
    )
    assert url == "https://music.apple.com/sv/song/1440818839"


def test_apple_without_catalog_id_falls_back_to_search() -> None:
    url = track_url("apple", platform_id=None, storefront="sv", name="Song", artist="Artist")
    assert url == "https://music.apple.com/search?term=Song+Artist"


def test_apple_without_storefront_falls_back_to_search() -> None:
    url = track_url("apple", platform_id="1440818839", name="Song", artist="Artist")
    assert url is not None
    assert url.startswith("https://music.apple.com/search?term=")


def test_apple_path_segments_are_escaped() -> None:
    url = track_url("apple", platform_id="1/../x", storefront="s v", name="S", artist="A")
    assert url == "https://music.apple.com/s%20v/song/1%2F..%2Fx"
