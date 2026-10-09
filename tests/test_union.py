"""T1 — N-way union purity tests (no I/O)."""

from __future__ import annotations

from pathlib import Path

from musicsync.domain.track import Track
from musicsync.domain.union import (
    canonicalize_presence,
    compute_tidal_catalog,
    compute_to_sync,
    remap_synced_keys,
    sort_tracks_chronologically,
)


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


def test_tidal_catalog_includes_all_liked_anywhere() -> None:
    old = Track(name="Old", artist="Band", added_at="2020-01-01")
    new = Track(name="New", artist="Band", added_at="2024-06-01")
    only_tidal = Track(name="Tidal Only", artist="X", added_at="2023-01-01")
    presence = {
        "spotify": {old.key: old, new.key: new},
        "apple": {},
        "tidal": {only_tidal.key: only_tidal},
    }
    catalog = compute_tidal_catalog(presence)
    assert {t.key for t in catalog} == {old.key, new.key, only_tidal.key}
    assert [t.name for t in catalog] == ["Old", "Tidal Only", "New"]


def test_sort_tracks_chronologically() -> None:
    a = Track(name="A", artist="X", added_at="2024-01-01")
    b = Track(name="B", artist="Y", added_at="2024-06-01")
    assert [t.key for t in sort_tracks_chronologically([b, a])] == [a.key, b.key]


def test_canonicalize_merges_title_only_and_full_artist_keys() -> None:
    tidal = Track(name="212", artist="")
    spotify = Track(name="212", artist="Azealia Banks")
    presence = {
        "tidal": {tidal.key: tidal},
        "spotify": {spotify.key: spotify},
        "apple": {},
    }
    canonical, key_remap = canonicalize_presence(presence)
    assert tidal.key in key_remap
    assert key_remap[tidal.key] == spotify.key
    assert set(canonical["tidal"]) == {spotify.key}
    assert set(canonical["spotify"]) == {spotify.key}
    assert canonical["tidal"][spotify.key].artist == "Azealia Banks"


def test_union_merges_title_only_duplicate_across_platforms() -> None:
    tidal = Track(name="212", artist="")
    spotify = Track(name="212", artist="Azealia Banks")
    presence = {
        "tidal": {tidal.key: tidal},
        "spotify": {spotify.key: spotify},
        "apple": {},
    }
    canonical, key_remap = canonicalize_presence(presence)
    result = compute_to_sync(
        canonical,
        remap_synced_keys({"apple": set(), "spotify": set(), "tidal": set()}, key_remap),
    )
    assert len(result["apple"]) == 1
    assert result["apple"][0].artist == "Azealia Banks"
    assert result["tidal"] == []
    assert result["spotify"] == []


def test_remap_synced_keys_follows_canonical_merge() -> None:
    tidal = Track(name="212", artist="")
    spotify = Track(name="212", artist="Azealia Banks")
    presence = {
        "tidal": {tidal.key: tidal},
        "spotify": {spotify.key: spotify},
        "apple": {},
    }
    _canonical, key_remap = canonicalize_presence(presence)
    synced = remap_synced_keys({"apple": {tidal.key}}, key_remap)
    assert synced["apple"] == {spotify.key}


def test_dedupe_title_only_keys_merges_db_rows(tmp_path: Path) -> None:
    from musicsync.infrastructure.sqlite_repository import SqliteTrackRepository

    repo = SqliteTrackRepository(tmp_path / "lib.db")
    title_only = Track(name="212", artist="", platform_id="t1", added_at="2026-07-02")
    full = Track(
        name="212",
        artist="Azealia Banks",
        platform_id="s1",
        added_at="2015-12-02",
        album="Album",
    )
    repo.upsert_presence("tidal", [title_only], liked=True)
    repo.upsert_presence("spotify", [full], liked=True)

    merged = repo.dedupe_title_only_keys()
    assert merged == 1

    presence = repo.get_liked_by_platform()
    assert title_only.key not in presence["tidal"]
    assert full.key in presence["tidal"]
    assert full.key in presence["spotify"]

    rows = repo.iter_enriched_rows()
    assert len(rows) == 1
    assert rows[0]["artist"] == "Azealia Banks"
    assert rows[0]["spotify"] == 1
    assert rows[0]["tidal"] == 1
    repo.close()


def test_union_queues_oldest_liked_first_not_alphabetical() -> None:
    newest = Track(name="Aaa", artist="Band", added_at="2024-06-01")
    oldest = Track(name="Zzz", artist="Band", added_at="2019-01-01")
    undated = Track(name="Mmm", artist="Band")
    presence = {
        "spotify": {t.key: t for t in (newest, oldest, undated)},
        "apple": {},
    }
    result = compute_to_sync(presence, {"spotify": set(), "apple": set()})
    assert [t.name for t in result["apple"]] == ["Mmm", "Zzz", "Aaa"]
