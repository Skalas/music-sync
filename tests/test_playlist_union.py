"""P1 — pure per-playlist union (offline, no I/O)."""

from __future__ import annotations

from musicsync.domain.playlist import (
    Playlist,
    PlaylistSummary,
    compute_playlist_union,
    normalize_playlist_name,
)
from musicsync.domain.track import Track

A = Track(name="Song A", artist="Artist A")
B = Track(name="Song B", artist="Artist B")
C = Track(name="Song C", artist="Artist C")


def _pl(platform: str, name: str, *tracks: Track) -> Playlist:
    return Playlist(platform=platform, name=name, tracks=tuple(tracks))


def _names(tracks: list[Track]) -> list[str]:
    return [t.name for t in tracks]


def test_normalize_playlist_name_is_case_and_space_insensitive() -> None:
    assert normalize_playlist_name("  Road   Trip ") == normalize_playlist_name("road trip")
    assert PlaylistSummary(platform="apple", name="ROAD TRIP").key == "road trip"


def test_zero_platforms_yields_no_diffs() -> None:
    assert compute_playlist_union({}, {}, targets=["spotify", "apple"]) == {}


def test_one_platform_fills_absent_target_and_skips_itself() -> None:
    diffs = compute_playlist_union(
        {"spotify": [_pl("spotify", "Mix", A, B)]}, {}, targets=["spotify", "apple"]
    )

    diff = diffs["mix"]
    assert diff.name == "Mix"
    assert diff.to_add["spotify"] == []
    assert _names(diff.to_add["apple"]) == ["Song A", "Song B"]
    assert diff.total == 2


def test_two_platforms_union_is_additive_both_ways() -> None:
    diffs = compute_playlist_union(
        {
            "spotify": [_pl("spotify", "Mix", A, B)],
            "apple": [_pl("apple", "  mix ", B, C)],
        },
        {},
        targets=["spotify", "apple"],
    )

    assert list(diffs) == ["mix"]
    assert _names(diffs["mix"].to_add["spotify"]) == ["Song C"]
    assert _names(diffs["mix"].to_add["apple"]) == ["Song A"]


def test_three_platforms_read_only_source_feeds_writable_targets() -> None:
    diffs = compute_playlist_union(
        {
            "spotify": [_pl("spotify", "Mix", A)],
            "apple": [_pl("apple", "Mix", B)],
            "tidal": [_pl("tidal", "Mix", C)],
        },
        {},
        targets=["spotify", "apple"],
    )

    to_add = diffs["mix"].to_add
    assert set(to_add) == {"spotify", "apple"}
    assert _names(to_add["spotify"]) == ["Song B", "Song C"]
    assert _names(to_add["apple"]) == ["Song A", "Song C"]


def test_match_key_identity_treats_decorated_title_as_same_track() -> None:
    remastered = Track(name="Song A (Remastered 2011)", artist="Artist A, Guest")

    diffs = compute_playlist_union(
        {
            "spotify": [_pl("spotify", "Mix", A)],
            "apple": [_pl("apple", "Mix", remastered)],
        },
        {},
        targets=["spotify", "apple"],
    )

    assert diffs["mix"].to_add == {"spotify": [], "apple": []}


def test_already_synced_keys_are_not_queued_again() -> None:
    diffs = compute_playlist_union(
        {"spotify": [_pl("spotify", "Mix", A, B)]},
        {"mix": {"apple": {A.key}}},
        targets=["apple"],
    )

    assert _names(diffs["mix"].to_add["apple"]) == ["Song B"]


def test_duplicates_inside_a_playlist_are_queued_once() -> None:
    diffs = compute_playlist_union(
        {"spotify": [_pl("spotify", "Mix", A, A, B)]}, {}, targets=["apple"]
    )

    assert _names(diffs["mix"].to_add["apple"]) == ["Song A", "Song B"]


def test_playlists_with_different_names_stay_separate() -> None:
    diffs = compute_playlist_union(
        {
            "spotify": [_pl("spotify", "Mix", A)],
            "apple": [_pl("apple", "Other", B)],
        },
        {},
        targets=["spotify", "apple"],
    )

    assert _names(diffs["mix"].to_add["apple"]) == ["Song A"]
    assert diffs["mix"].to_add["spotify"] == []
    assert _names(diffs["other"].to_add["spotify"]) == ["Song B"]


def test_title_only_track_merges_with_same_title_that_has_artist() -> None:
    title_only = Track(name="Song A", artist="")

    diffs = compute_playlist_union(
        {
            "spotify": [_pl("spotify", "Mix", A)],
            "tidal": [_pl("tidal", "Mix", title_only)],
        },
        {},
        targets=["spotify", "apple"],
    )

    assert diffs["mix"].to_add["spotify"] == []
    assert _names(diffs["mix"].to_add["apple"]) == ["Song A"]
    assert diffs["mix"].to_add["apple"][0].artist == "Artist A"
    assert diffs["mix"].unresolved == []


def test_lone_title_only_track_is_unresolved_never_queued() -> None:
    lone = Track(name="Intro", artist="")

    diffs = compute_playlist_union(
        {"tidal": [_pl("tidal", "Mix", lone, B)]}, {}, targets=["spotify", "apple"]
    )

    assert _names(diffs["mix"].to_add["spotify"]) == ["Song B"]
    assert _names(diffs["mix"].to_add["apple"]) == ["Song B"]
    assert diffs["mix"].unresolved == [lone]


def test_title_only_track_with_ambiguous_artists_stays_unresolved() -> None:
    adele = Track(name="Hello", artist="Adele")
    richie = Track(name="Hello", artist="Lionel Richie")
    title_only = Track(name="Hello", artist="")

    diffs = compute_playlist_union(
        {
            "spotify": [_pl("spotify", "Mix", adele)],
            "apple": [_pl("apple", "Mix", richie)],
            "tidal": [_pl("tidal", "Mix", title_only)],
        },
        {},
        targets=["spotify", "apple"],
    )

    assert diffs["mix"].to_add["spotify"] == [richie]
    assert diffs["mix"].to_add["apple"] == [adele]
    assert diffs["mix"].unresolved == [title_only]
