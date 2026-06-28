"""T1 — N-way union purity tests (no I/O)."""

from __future__ import annotations

from musicsync.domain.track import Track
from musicsync.domain.union import compute_to_sync


def _t(name: str, artist: str) -> Track:
    return Track(name=name, artist=artist)


def test_union_zero_platforms() -> None:
    result = compute_to_sync({}, {})
    assert result == {}


def test_union_single_platform_no_sync_needed() -> None:
    tracks = {"spotify": {_t("A", "B").key: _t("A", "B")}}
    result = compute_to_sync(tracks, {"spotify": set()})
    assert result["spotify"] == []


def test_union_two_platforms_bidirectional() -> None:
    spotify = {
        _t("Song A", "Artist X").key: _t("Song A", "Artist X"),
        _t("Song B", "Artist Y").key: _t("Song B", "Artist Y"),
    }
    apple = {
        _t("Song A", "Artist X").key: _t("Song A", "Artist X"),
        _t("Song C", "Artist Z").key: _t("Song C", "Artist Z"),
    }
    presence = {"spotify": spotify, "apple": apple}
    result = compute_to_sync(presence, {"spotify": set(), "apple": set()})

    assert len(result["apple"]) == 1
    assert result["apple"][0].name == "Song B"
    assert len(result["spotify"]) == 1
    assert result["spotify"][0].name == "Song C"


def test_union_three_platforms() -> None:
    s = _t("Only Spotify", "Band")
    a = _t("Only Apple", "Band")
    t_track = _t("Only Tidal", "Band")
    shared = _t("Shared", "Band")

    presence = {
        "spotify": {s.key: s, shared.key: shared},
        "apple": {a.key: a, shared.key: shared},
        "tidal": {t_track.key: t_track},
    }
    result = compute_to_sync(
        presence,
        {"spotify": set(), "apple": set(), "tidal": set()},
    )

    assert {x.key for x in result["spotify"]} == {a.key, t_track.key}
    assert {x.key for x in result["apple"]} == {s.key, t_track.key}
    assert {x.key for x in result["tidal"]} == {s.key, a.key, shared.key}


def test_union_respects_already_synced() -> None:
    only_spotify = _t("New", "Artist")
    presence = {
        "spotify": {only_spotify.key: only_spotify},
        "apple": {},
    }
    result = compute_to_sync(
        presence,
        {"apple": {only_spotify.key}, "spotify": set()},
    )
    assert result["apple"] == []
