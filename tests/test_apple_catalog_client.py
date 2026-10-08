"""T3/T4 — Apple catalog client (mocked HTTP) and candidate selection.

T3 tests carry "isrc" in their names, T4 tests carry "search" (dod.json -k selectors).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
import requests

from musicsync.domain.apple_catalog import (
    AppleLinkTarget,
    CatalogSong,
    select_isrc_match,
    select_search_match,
)
from musicsync.domain.track import normalize_key
from musicsync.infrastructure.apple_music_api import (
    AppleCatalogClient,
    AppleMusicError,
    DeveloperTokenProvider,
)

SECRET_TOKEN = "secret.jwt.value"


def _target(
    name: str = "Believer",
    artist: str = "Imagine Dragons",
    *,
    album: str | None = "Evolve",
    duration_sec: int | None = 204,
    isrc: str | None = "USUM71700626",
) -> AppleLinkTarget:
    return AppleLinkTarget(
        key=normalize_key(name, artist),
        name=name,
        artist=artist,
        album=album,
        duration_sec=duration_sec,
        isrc=isrc,
    )


def _song(
    catalog_id: str,
    *,
    name: str = "Believer",
    artist: str = "Imagine Dragons",
    album: str | None = "Evolve",
    duration_sec: int | None = 204,
    is_compilation: bool = False,
) -> CatalogSong:
    return CatalogSong(
        catalog_id=catalog_id,
        name=name,
        artist=artist,
        album=album,
        duration_sec=duration_sec,
        is_compilation=is_compilation,
    )


def _resource(
    catalog_id: str,
    *,
    album: str = "Evolve",
    millis: int = 204_000,
    album_is_compilation: bool | None = None,
) -> dict[str, Any]:
    resource: dict[str, Any] = {
        "id": catalog_id,
        "type": "songs",
        "attributes": {
            "name": "Believer",
            "artistName": "Imagine Dragons",
            "albumName": album,
            "durationInMillis": millis,
            "url": f"https://music.apple.com/sv/song/{catalog_id}",
            "isrc": "USUM71700626",
        },
    }
    if album_is_compilation is not None:
        resource["relationships"] = {
            "albums": {
                "data": [{"id": "al", "attributes": {"isCompilation": album_is_compilation}}]
            }
        }
    return resource


def _response(status: int, body: dict[str, Any] | None = None, headers: Any = None) -> Any:
    resp = MagicMock(status_code=status, headers=headers or {})
    resp.json.return_value = body or {}
    return resp


def _client(*responses: Any) -> tuple[AppleCatalogClient, MagicMock, list[float]]:
    tokens = MagicMock(spec=DeveloperTokenProvider)
    tokens.token.return_value = SECRET_TOKEN
    session = MagicMock(spec=requests.Session)
    session.get.side_effect = list(responses)
    sleeps: list[float] = []
    client = AppleCatalogClient(tokens, "sv", session=session, sleep=sleeps.append)
    return client, session, sleeps


# --- T3: ISRC lookup ------------------------------------------------------


def test_isrc_lookup_sends_filter_and_bearer_token() -> None:
    client, session, _sleeps = _client(_response(200, {"data": [_resource("111")]}))

    songs = client.songs_by_isrc("USUM71700626")

    url = session.get.call_args.args[0]
    kwargs = session.get.call_args.kwargs
    assert url == "https://api.music.apple.com/v1/catalog/sv/songs"
    assert kwargs["params"]["filter[isrc]"] == "USUM71700626"
    assert kwargs["headers"]["Authorization"] == f"Bearer {SECRET_TOKEN}"
    assert songs == [
        CatalogSong(
            catalog_id="111",
            name="Believer",
            artist="Imagine Dragons",
            album="Evolve",
            duration_sec=204,
            is_compilation=False,
            url="https://music.apple.com/sv/song/111",
        )
    ]


def test_isrc_compilation_flag_from_album_relationship_and_name() -> None:
    body = {
        "data": [
            _resource("comp", album="Evolve", album_is_compilation=True),
            _resource("hits", album="Greatest Hits 2017"),
            _resource("orig", album="Evolve", album_is_compilation=False),
        ]
    }
    client, _session, _sleeps = _client(_response(200, body))

    flags = {s.catalog_id: s.is_compilation for s in client.songs_by_isrc("X")}

    assert flags == {"comp": True, "hits": True, "orig": False}


def test_isrc_lookup_retries_429_respecting_retry_after() -> None:
    client, session, sleeps = _client(
        _response(429, headers={"Retry-After": "7"}),
        _response(503),
        _response(200, {"data": [_resource("111")]}),
    )

    songs = client.songs_by_isrc("X")

    assert [s.catalog_id for s in songs] == ["111"]
    assert session.get.call_count == 3
    assert sleeps[0] == 7.0
    assert len(sleeps) == 2


def test_isrc_non_json_200_raises_platform_error() -> None:
    resp = _response(200)
    resp.json.side_effect = requests.exceptions.JSONDecodeError("bad", "<html>", 0)
    client, _session, _sleeps = _client(resp)

    with pytest.raises(AppleMusicError, match="no JSON"):
        client.songs_by_isrc("X")


def test_isrc_json_array_body_raises_platform_error() -> None:
    resp = _response(200)
    resp.json.return_value = [{"id": "1"}]
    client, _session, _sleeps = _client(resp)

    with pytest.raises(AppleMusicError):
        client.songs_by_isrc("X")


@pytest.mark.parametrize("retry_after", ["nan", "-1", "inf", "soon"])
def test_isrc_bad_retry_after_falls_back_to_backoff(retry_after: str) -> None:
    client, _session, sleeps = _client(
        _response(429, headers={"Retry-After": retry_after}),
        _response(200, {"data": []}),
    )

    client.songs_by_isrc("X")

    assert len(sleeps) == 1
    assert 0 < sleeps[0] <= 60


def test_isrc_lookup_auth_error_never_leaks_token() -> None:
    client, _session, _sleeps = _client(_response(401))

    with pytest.raises(AppleMusicError) as exc:
        client.songs_by_isrc("X")

    assert "401" in str(exc.value)
    assert SECRET_TOKEN not in str(exc.value)


def test_isrc_network_error_never_leaks_token() -> None:
    error = requests.ConnectionError(f"boom Authorization: Bearer {SECRET_TOKEN}")
    client, _session, _sleeps = _client(error, error, error, error)

    with pytest.raises(AppleMusicError) as exc:
        client.songs_by_isrc("X")

    assert SECRET_TOKEN not in str(exc.value)
    assert exc.value.__cause__ is None


def test_isrc_selection_prefers_original_over_compilation() -> None:
    candidates = [
        _song("comp", album="Now 98", is_compilation=True),
        _song("orig", album="Evolve"),
    ]
    match = select_isrc_match(candidates, _target())
    assert match is not None
    assert match.catalog_id == "orig"


def test_isrc_selection_keeps_compilation_when_it_is_the_only_hit() -> None:
    match = select_isrc_match([_song("comp", is_compilation=True)], _target())
    assert match is not None
    assert match.catalog_id == "comp"


def test_isrc_selection_prefers_album_name_match() -> None:
    candidates = [
        _song("single", album="Believer - Single", duration_sec=204),
        _song("album", album="Evolve (Deluxe)", duration_sec=205),
    ]
    match = select_isrc_match(candidates, _target(album="Evolve"))
    assert match is not None
    assert match.catalog_id == "album"


def test_isrc_selection_picks_closest_duration() -> None:
    candidates = [
        _song("far", album="Other", duration_sec=207),
        _song("near", album="Other 2", duration_sec=205),
    ]
    match = select_isrc_match(candidates, _target(album=None))
    assert match is not None
    assert match.catalog_id == "near"


def test_isrc_selection_rejects_hit_outside_tolerance() -> None:
    assert select_isrc_match([_song("long", duration_sec=260)], _target()) is None


def test_isrc_selection_with_no_hits_is_none() -> None:
    assert select_isrc_match([], _target()) is None


# --- T4: name + artist search fallback -----------------------------------


def test_search_request_shape() -> None:
    body = {"results": {"songs": {"data": [_resource("222")]}}}
    client, session, _sleeps = _client(_response(200, body))

    songs = client.search_songs("Believer Imagine Dragons")

    url = session.get.call_args.args[0]
    params = session.get.call_args.kwargs["params"]
    assert url == "https://api.music.apple.com/v1/catalog/sv/search"
    assert params["types"] == "songs"
    assert params["term"] == "Believer Imagine Dragons"
    assert int(params["limit"]) == 10
    assert [s.catalog_id for s in songs] == ["222"]


def test_search_with_empty_results_returns_nothing() -> None:
    client, _session, _sleeps = _client(_response(200, {"results": {}}))
    assert client.search_songs("nothing") == []


def test_search_accepts_exact_title_within_tolerance() -> None:
    candidates = [
        _song("cover", artist="Some Cover Band"),
        _song("real", name="Believer", artist="Imagine Dragons, Lil Wayne", duration_sec=206),
    ]
    match = select_search_match(candidates, _target(album=None))
    assert match is not None
    assert match.catalog_id == "real"


def test_search_title_punctuation_and_accents_still_match() -> None:
    target = _target(name="Canción (En Vivo)", artist="Artista", album=None)
    match = select_search_match(
        [_song("x", name="Cancion [En Vivo]", artist="Artista")], target
    )
    assert match is not None


def test_search_rejects_variant_title_with_unknown_duration() -> None:
    candidates = [_song("live", name="Song (Live)", artist="Artist", duration_sec=None)]
    target = _target(name="Song", artist="Artist", album=None, duration_sec=None)
    assert select_search_match(candidates, target) is None


def test_search_rejects_variant_title_even_within_tolerance() -> None:
    for variant in ("Believer (Live)", "Believer - Remix", "Believer (Acoustic)"):
        assert select_search_match([_song("v", name=variant)], _target(album=None)) is None


def test_search_rejects_unknown_apple_duration() -> None:
    assert select_search_match([_song("x", duration_sec=None)], _target()) is None


def test_search_rejects_other_key() -> None:
    candidates = [_song("other", name="Thunder")]
    assert select_search_match(candidates, _target()) is None


def test_search_rejects_duration_outside_tolerance() -> None:
    candidates = [_song("live", name="Believer (Live)", duration_sec=240)]
    assert select_search_match(candidates, _target()) is None


def test_search_rejects_unknown_stored_duration() -> None:
    candidates = [_song("a", duration_sec=204), _song("b", duration_sec=205)]
    assert select_search_match(candidates, _target(album=None, duration_sec=None)) is None


def test_search_title_only_key_never_matches() -> None:
    target = AppleLinkTarget(key=normalize_key("Believer", ""), name="Believer", artist="")
    assert select_search_match([_song("x")], target) is None


# --- B4: ISRC hits must carry the exact stored title ----------------------


def test_isrc_variant_title_is_not_picked() -> None:
    target = _target(name="Believer", isrc="REMIXISRC")
    candidates = [_song("remix", name="Believer (Kaskade Remix)", duration_sec=205)]
    assert select_isrc_match(candidates, target) is None


def test_isrc_variant_hit_falls_back_to_search() -> None:
    from musicsync.application.apple_links import find_catalog_song

    lookup = MagicMock()
    lookup.songs_by_isrc.return_value = [_song("remix", name="Believer - Remix")]
    lookup.search_songs.return_value = [_song("orig", name="Believer")]

    match = find_catalog_song(lookup, _target())

    assert match is not None
    assert match.catalog_id == "orig"
    lookup.search_songs.assert_called_once()


def test_isrc_exact_title_hit_still_resolves() -> None:
    candidates = [
        _song("remix", name="Believer (Remix)"),
        _song("orig", name="Believer", album="Evolve"),
    ]
    match = select_isrc_match(candidates, _target())
    assert match is not None
    assert match.catalog_id == "orig"


# --- B5: malformed nested JSON never crashes ------------------------------


def _with(resource: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    out = dict(resource)
    out.update(overrides)
    return out


def _with_attrs(**overrides: Any) -> dict[str, Any]:
    resource = _resource("1")
    resource["attributes"] = {**resource["attributes"], **overrides}
    return resource


@pytest.mark.parametrize(
    "resource",
    [
        _with_attrs(name=123),
        _with_attrs(name=None),
        _with_attrs(artistName=["Imagine Dragons"]),
        _with_attrs(artistName=None),
        _with(_resource("1"), attributes="oops"),
        _with(_resource("1"), id={"nested": True}),
        _with(_resource("1"), id=True),
        "not-a-dict",
        None,
    ],
)
def test_isrc_malformed_song_is_skipped(resource: Any) -> None:
    client, _session, _sleeps = _client(_response(200, {"data": [resource]}))
    assert client.songs_by_isrc("X") == []


@pytest.mark.parametrize(
    "relationships",
    [
        "oops",
        {"albums": "oops"},
        {"albums": {"data": "oops"}},
        {"albums": {"data": {"id": "al"}}},
        {"albums": {"data": ["not-a-dict"]}},
        {"albums": {"data": [{"attributes": "oops"}]}},
        {"albums": {"data": [{"attributes": {"isCompilation": "yes"}}]}},
        {"albums": {"data": [{"attributes": {"artistName": 42}}]}},
        {"albums": {"data": []}},
    ],
)
def test_isrc_malformed_album_relationship_is_unknown(relationships: Any) -> None:
    resource = _with(_resource("1"), relationships=relationships)
    client, _session, _sleeps = _client(_response(200, {"data": [resource]}))

    [song] = client.songs_by_isrc("X")

    assert song.is_compilation is False


@pytest.mark.parametrize(
    ("overrides", "expected_album", "expected_duration"),
    [
        ({"albumName": 7, "durationInMillis": "204000"}, None, None),
        ({"albumName": ["x"], "durationInMillis": True}, None, None),
        ({"url": 5, "durationInMillis": -1}, "Evolve", None),
        ({"durationInMillis": 204_400.0}, "Evolve", 204),
    ],
)
def test_isrc_wrong_typed_optional_fields_become_none(
    overrides: dict[str, Any], expected_album: str | None, expected_duration: int | None
) -> None:
    client, _session, _sleeps = _client(_response(200, {"data": [_with_attrs(**overrides)]}))

    [song] = client.songs_by_isrc("X")

    assert (song.album, song.duration_sec) == (expected_album, expected_duration)
    assert song.url is None or isinstance(song.url, str)


def test_search_malformed_hits_do_not_crash_resolution() -> None:
    body = {"results": {"songs": {"data": [_with_attrs(artistName=None), _resource("ok")]}}}
    client, _session, _sleeps = _client(_response(200, body))

    assert [s.catalog_id for s in client.search_songs("Believer Imagine Dragons")] == ["ok"]


# --- W2: recording-title normalizer (live T8 titles) ----------------------

LIVE_DECORATED_TITLES = [
    ('Accidentally In Love - From "Shrek 2" Soundtrack', "Accidentally In Love", "Counting Crows"),
    (
        'All The Stars (with SZA) - From "Black Panther: The Album"',
        "All the Stars",
        "Kendrick Lamar",
    ),
    ("All That I Got Is You (feat. Mary J. Blige)", "All That I Got Is You", "Ghostface Killah"),
    ("Here Comes The Sun - Remastered 2009", "Here Comes the Sun", "The Beatles"),
    ("Here Comes The Sun - 2009 Remaster", "Here Comes the Sun", "The Beatles"),
]

RECORDING_VARIANTS = [
    "Song - Monkey Safari Remix",
    "Song (Live)",
    "Song - Acoustic Version",
    "Song - Radio Edit",
]


def _decorated_target(stored: str, artist: str) -> AppleLinkTarget:
    return _target(name=stored, artist=artist, album=None, duration_sec=200, isrc="ISRC")


@pytest.mark.parametrize(("stored", "apple", "artist"), LIVE_DECORATED_TITLES)
def test_isrc_resolves_neutral_title_decorations(stored: str, apple: str, artist: str) -> None:
    hit = _song("ok", name=apple, artist=artist, album=None, duration_sec=201)
    assert select_isrc_match([hit], _decorated_target(stored, artist)) == hit


@pytest.mark.parametrize(("stored", "apple", "artist"), LIVE_DECORATED_TITLES)
def test_search_resolves_neutral_title_decorations(stored: str, apple: str, artist: str) -> None:
    hit = _song("ok", name=apple, artist=artist, album=None, duration_sec=201)
    assert select_search_match([hit], _decorated_target(stored, artist)) == hit


@pytest.mark.parametrize("variant", RECORDING_VARIANTS)
def test_isrc_recording_variant_vs_plain_title_unresolved(variant: str) -> None:
    plain = _song("plain", name="Song", artist="Artist", album=None, duration_sec=200)
    assert select_isrc_match([plain], _decorated_target(variant, "Artist")) is None
    variant_hit = _song("v", name=variant, artist="Artist", album=None, duration_sec=200)
    assert select_isrc_match([variant_hit], _decorated_target("Song", "Artist")) is None


@pytest.mark.parametrize("variant", RECORDING_VARIANTS)
def test_search_recording_variant_vs_plain_title_unresolved(variant: str) -> None:
    plain = _song("plain", name="Song", artist="Artist", album=None, duration_sec=200)
    assert select_search_match([plain], _decorated_target(variant, "Artist")) is None
    variant_hit = _song("v", name=variant, artist="Artist", album=None, duration_sec=200)
    assert select_search_match([variant_hit], _decorated_target("Song", "Artist")) is None


def test_search_decorated_title_still_requires_same_primary_artist() -> None:
    hit = _song("x", name="Accidentally In Love", artist="Some Cover Band", duration_sec=200)
    target = _decorated_target('Accidentally In Love - From "Shrek 2" Soundtrack', "Counting Crows")
    assert select_search_match([hit], target) is None


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Song (feat. X) [Remix]", "song remix"),
        ("Song - Single Version", "song single version"),
        ("Song - Live From Wembley", "song live from wembley"),
        ('Song (From the Motion Picture "X")', "song"),
        ("Song [Deluxe Edition]", "song"),
        ("Song - Mono", "song"),
    ],
)
def test_search_recording_title_normalizer(title: str, expected: str) -> None:
    from musicsync.domain.track import normalize_recording_title

    assert normalize_recording_title(title) == expected


# --- B6: a marker inside a credit bracket keeps the bracket ---------------

MARKER_IN_CREDIT = [
    ("Titanium (feat. Sia - Alesso Remix)", "Titanium", "David Guetta"),
    ("Wonderwall (with Strings Version)", "Wonderwall", "Oasis"),
]


@pytest.mark.parametrize(("stored", "plain", "artist"), MARKER_IN_CREDIT)
def test_search_marker_inside_credit_does_not_match_plain(
    stored: str, plain: str, artist: str
) -> None:
    plain_hit = _song("plain", name=plain, artist=artist, album=None, duration_sec=200)
    assert select_search_match([plain_hit], _decorated_target(stored, artist)) is None
    variant_hit = _song("v", name=stored, artist=artist, album=None, duration_sec=200)
    assert select_search_match([variant_hit], _decorated_target(plain, artist)) is None


@pytest.mark.parametrize(("stored", "plain", "artist"), MARKER_IN_CREDIT)
def test_isrc_marker_inside_credit_does_not_match_plain(
    stored: str, plain: str, artist: str
) -> None:
    plain_hit = _song("plain", name=plain, artist=artist, album=None, duration_sec=200)
    assert select_isrc_match([plain_hit], _decorated_target(stored, artist)) is None


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Titanium (feat. Sia - Alesso Remix)", "titanium feat sia alesso remix"),
        ("Wonderwall (with Strings Version)", "wonderwall with strings version"),
        ("Song (feat. X)", "song"),
        ('All The Stars (with SZA) - From "Black Panther: The Album"', "all the stars"),
        ("Stay With Me", "stay with me"),
        ("From Eden", "from eden"),
        ("Song (feat. Mix Master Mike)", "song feat mix master mike"),
    ],
)
def test_search_normalizer_marker_wins_over_credit(title: str, expected: str) -> None:
    from musicsync.domain.track import normalize_recording_title

    assert normalize_recording_title(title) == expected
