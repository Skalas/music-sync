"""P3 — provider playlist reads, capability flags, and additive writes (all mocked)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import requests

import musicsync.infrastructure.spotify_provider as spotify_mod
from musicsync.domain.errors import PlatformOperationError
from musicsync.domain.playlist import PlaylistSummary
from musicsync.domain.ports import PlaylistProvider
from musicsync.domain.track import Track
from musicsync.infrastructure.apple_provider import AppleProvider
from musicsync.infrastructure.spotify_provider import (
    SCOPE_PLAYLIST_READ,
    PlaylistScope,
    SpotifyProvider,
    cached_token_scopes,
)
from musicsync.infrastructure.tidal_provider import TidalProvider

USER_ID = "me-123"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def spotify_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SPOTIPY_CLIENT_ID", "id")
    monkeypatch.setenv("SPOTIPY_CLIENT_SECRET", "secret")
    monkeypatch.setenv("SPOTIPY_REDIRECT_URI", "http://127.0.0.1:8080")
    monkeypatch.setattr(spotify_mod, "SPOTIFY_SEARCH_DELAY_SEC", 0)
    (tmp_path / ".env").write_text("", encoding="utf-8")
    return tmp_path


@pytest.fixture
def tidal_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TIDAL_CLIENT_ID", "test-client")
    monkeypatch.setenv("TIDAL_REDIRECT_URI", "http://127.0.0.1:8080")
    (tmp_path / ".env").write_text("", encoding="utf-8")
    return tmp_path


def _spotify(
    base: Path, sp: MagicMock | None = None, scope: PlaylistScope = PlaylistScope.READ
) -> SpotifyProvider:
    return SpotifyProvider(
        base_dir=base,
        unmatched_log_path=base / "unmatched.log",
        playlist_scope=scope,
        client=sp,
    )


def _page(items: list[dict[str, Any]], nxt: str | None = None) -> dict[str, Any]:
    return {"items": items, "next": nxt}


def _sp_track(track_id: str | None, name: str, artist: str = "Artist") -> dict[str, Any]:
    return {"type": "track", "id": track_id, "name": name, "artists": [{"name": artist}]}


def _search_hit(track_id: str) -> dict[str, Any]:
    return {"tracks": {"items": [{"id": track_id}]}}


def _completed(stdout: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


# ---------------------------------------------------------------------------
# Capability flags
# ---------------------------------------------------------------------------


def test_capability_flags_tidal_is_read_only(
    tmp_path: Path, spotify_env: Path, tidal_env: Path
) -> None:
    spotify = _spotify(spotify_env)
    apple = AppleProvider(applescript_dir=tmp_path, output_path=tmp_path / "o.txt")
    tidal = TidalProvider(base_dir=tidal_env)

    for provider in (spotify, apple, tidal):
        assert isinstance(provider, PlaylistProvider)
        assert provider.can_playlist_read is True
    assert spotify.can_playlist_write is True
    assert apple.can_playlist_write is True
    assert tidal.can_playlist_write is False
    with pytest.raises(PlatformOperationError, match="solo lectura"):
        tidal.add_to_playlist("Mix", None, [Track(name="A", artist="B")])


# ---------------------------------------------------------------------------
# Spotify
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("scope", "need_write", "expected"),
    [
        (PlaylistScope.NONE, False, {"user-library-read"}),
        (PlaylistScope.NONE, True, {"user-library-read", "user-library-modify"}),
        (
            PlaylistScope.READ,
            False,
            {"user-library-read", "playlist-read-private", "playlist-read-collaborative"},
        ),
        (
            PlaylistScope.WRITE,
            False,
            {
                "user-library-read",
                "playlist-read-private",
                "playlist-read-collaborative",
                "playlist-modify-private",
                "playlist-modify-public",
            },
        ),
    ],
)
def test_spotify_playlist_scopes_only_when_needed(
    spotify_env: Path, scope: PlaylistScope, need_write: bool, expected: set[str]
) -> None:
    provider = SpotifyProvider(
        base_dir=spotify_env,
        unmatched_log_path=spotify_env / "u.log",
        need_write=need_write,
        playlist_scope=scope,
    )

    config = provider._load_config(need_write)  # noqa: SLF001

    assert set(config["scope"].split()) == expected


def test_spotify_list_playlists_flags_followed_keeps_own_collaborative(
    spotify_env: Path,
) -> None:
    sp = MagicMock()
    sp.current_user.return_value = {"id": USER_ID}
    sp.current_user_playlists.side_effect = [
        _page(
            [
                {"id": "p1", "name": "Mine", "owner": {"id": USER_ID}, "items": {"total": 3}},
                {"id": "p2", "name": "Followed", "owner": {"id": "other"}},
            ],
            nxt="more",
        ),
        _page(
            [
                {"id": "p3", "name": "Legacy", "owner": {"id": USER_ID}, "tracks": {"total": 7}},
                {"id": "p4", "name": "Gym", "owner": {"id": USER_ID}, "collaborative": True},
            ]
        ),
    ]

    summaries = _spotify(spotify_env, sp).list_playlists()

    assert [(s.name, s.remote_id, s.track_count, s.owned) for s in summaries] == [
        ("Mine", "p1", 3, True),
        ("Followed", "p2", 0, False),
        ("Legacy", "p3", 7, True),
        ("Gym", "p4", 0, True),
    ]
    assert all(s.platform == "spotify" for s in summaries)


def test_spotify_read_playlist_parses_items_and_skips_episodes(spotify_env: Path) -> None:
    sp = MagicMock()
    sp.playlist_items.return_value = _page(
        [
            {"item": _sp_track("t1", "One")},
            {"track": _sp_track("t2", "Two")},
            {"item": {"type": "episode", "id": "e1", "name": "Pod"}},
            {"item": None},
            {"item": _sp_track(None, "Local file")},
        ]
    )

    playlist = _spotify(spotify_env, sp).read_playlist(
        PlaylistSummary(platform="spotify", name="Mix", remote_id="p1")
    )

    assert [t.name for t in playlist.tracks] == ["One", "Two", "Local file"]
    assert playlist.tracks[0].platform_id == "t1"
    assert playlist.remote_id == "p1"


def test_spotify_add_requires_playlist_write_scope(spotify_env: Path) -> None:
    sp = MagicMock()

    with pytest.raises(PlatformOperationError, match="--apply-spotify"):
        _spotify(spotify_env, sp, PlaylistScope.READ).add_to_playlist(
            "Mix", None, [Track(name="A", artist="B")]
        )
    sp.search.assert_not_called()


def test_spotify_add_resolves_by_search_and_never_duplicates(spotify_env: Path) -> None:
    sp = MagicMock()
    sp.playlist_items.return_value = _page([{"item": _sp_track("already", "Old")}])
    hits = {"New": _search_hit("new1"), "Old Alias": _search_hit("already")}
    sp.search.side_effect = lambda q, **_: hits.get(
        q.split("track:")[1].split(" artist:")[0], {"tracks": {"items": []}}
    )
    foreign = Track(name="New", artist="Artist", platform_id="apple-or-tidal-id")
    alias = Track(name="Old Alias", artist="Artist")
    missing = Track(name="Nowhere", artist="Artist")

    result = _spotify(spotify_env, sp, PlaylistScope.WRITE).add_to_playlist(
        "Mix", "p1", [foreign, alias, missing]
    )

    sp.playlist_add_items.assert_called_once_with("p1", ["new1"])
    sp.current_user_playlist_create.assert_not_called()
    assert result.remote_id == "p1"
    assert result.added == [foreign, alias]
    assert result.unresolved == [missing]


def test_spotify_add_creates_private_playlist_when_absent(spotify_env: Path) -> None:
    sp = MagicMock()
    sp.search.return_value = _search_hit("t1")
    sp.current_user_playlist_create.return_value = {"id": "created"}

    result = _spotify(spotify_env, sp, PlaylistScope.WRITE).add_to_playlist(
        "Mix", None, [Track(name="A", artist="B")]
    )

    sp.current_user_playlist_create.assert_called_once_with("Mix", public=False)
    sp.playlist_add_items.assert_called_once_with("created", ["t1"])
    assert result.remote_id == "created"


def test_spotify_add_creates_nothing_when_no_track_resolves(spotify_env: Path) -> None:
    sp = MagicMock()
    sp.search.return_value = {"tracks": {"items": []}}

    result = _spotify(spotify_env, sp, PlaylistScope.WRITE).add_to_playlist(
        "Mix", None, [Track(name="A", artist="B")]
    )

    sp.current_user_playlist_create.assert_not_called()
    sp.playlist_add_items.assert_not_called()
    assert result.remote_id is None
    assert len(result.unresolved) == 1


def test_cached_token_scopes_reads_spotify_cache(tmp_path: Path) -> None:
    assert cached_token_scopes(tmp_path) == set()
    (tmp_path / ".cache").write_text(
        json.dumps({"access_token": "x", "scope": f"user-library-read {SCOPE_PLAYLIST_READ}"}),
        encoding="utf-8",
    )
    assert cached_token_scopes(tmp_path) == {"user-library-read", SCOPE_PLAYLIST_READ}


# ---------------------------------------------------------------------------
# Apple (AppleScript mocked at subprocess.run)
# ---------------------------------------------------------------------------


def _apple(tmp_path: Path) -> AppleProvider:
    return AppleProvider(applescript_dir=tmp_path, output_path=tmp_path / "o.txt")


def test_apple_list_playlists_skips_system_and_garbled_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stdout = (
        "PID1\tRoad Trip\t12\n"
        "PID2\tFavourite Songs\t300\n"
        "PID3\tMusic Videos\t4\n"
        "garbled line\n"
        "PID4\tGym\t\n"
    )
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return _completed(stdout)

    monkeypatch.setattr(subprocess, "run", fake_run)

    summaries = _apple(tmp_path).list_playlists()

    assert [(s.name, s.remote_id, s.track_count) for s in summaries] == [
        ("Road Trip", "PID1", 12),
        ("Gym", "PID4", 0),
    ]
    assert calls[0][1].endswith("read_playlists.applescript")


def test_apple_read_playlist_passes_persistent_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return _completed("One\tArtist A\nTwo\tArtist B\n")

    monkeypatch.setattr(subprocess, "run", fake_run)

    playlist = _apple(tmp_path).read_playlist(
        PlaylistSummary(platform="apple", name="Mix", remote_id="PID1")
    )

    assert calls[0][1].endswith("read_playlist_tracks.applescript")
    assert calls[0][2] == "PID1"
    assert [(t.name, t.artist) for t in playlist.tracks] == [
        ("One", "Artist A"),
        ("Two", "Artist B"),
    ]


def test_apple_add_maps_statuses_and_captures_new_playlist_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tracks = [
        Track(name="Added", artist="Artist A, Guest"),
        Track(name="Present", artist="Artist B"),
        Track(name="Catalog only", artist="Artist C"),
        Track(name="Broken", artist="Artist D"),
    ]
    seen: dict[str, Any] = {}

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen["cmd"] = cmd
        seen["payload"] = Path(cmd[2]).read_text(encoding="utf-8")
        return _completed(
            "ADDED\tAdded\tArtist A\n"
            "PRESENT\tPresent\tArtist B\n"
            "MISSING\tCatalog only\tArtist C\n"
            "ERROR\tBroken\tArtist D\n"
            "PLAYLIST\tNEWPID\n"
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = _apple(tmp_path).add_to_playlist("Mix", None, tracks)

    assert seen["cmd"][1].endswith("add_to_playlist.applescript")
    assert seen["cmd"][3:] == ["Mix", ""]  # no persistent id → script creates new
    assert seen["payload"].splitlines()[0] == "Added\tArtist A"
    assert not Path(seen["cmd"][2]).exists()
    assert [t.name for t in result.added] == ["Added", "Present"]
    assert [t.name for t in result.unresolved] == ["Catalog only", "Broken"]
    assert result.remote_id == "NEWPID"


def test_apple_add_script_failure_is_platform_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(1, cmd, stderr="Music no responde")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(PlatformOperationError, match="Music no responde"):
        _apple(tmp_path).add_to_playlist("Mix", "PID", [Track(name="A", artist="B")])


# ---------------------------------------------------------------------------
# Tidal (mocked HTTP, read-only)
# ---------------------------------------------------------------------------


def _tidal(base: Path, responses: list[dict[str, Any]]) -> tuple[TidalProvider, MagicMock]:
    session = requests.Session()
    get = MagicMock(
        side_effect=[MagicMock(status_code=200, json=lambda r=r: r) for r in responses]
    )
    session.get = get  # type: ignore[method-assign]
    provider = TidalProvider(base_dir=base, session=session)
    provider._write_cache(  # noqa: SLF001
        {"access_token": "tok", "expires_in": 3600, "obtained_at": 9_999_999_999.0}
    )
    return provider, get


def test_tidal_list_playlists_pages_owned_playlists(tidal_env: Path) -> None:
    page1 = {
        "data": [{"id": "u1", "attributes": {"name": "Mix", "numberOfItems": 5}}],
        "links": {"next": "/playlists?page%5Bcursor%5D=c2"},
    }
    page2 = {"data": [{"id": "u2", "attributes": {"name": "Gym"}}], "links": {}}
    provider, get = _tidal(tidal_env, [page1, page2])

    summaries = provider.list_playlists()

    assert [(s.name, s.remote_id, s.track_count) for s in summaries] == [
        ("Mix", "u1", 5),
        ("Gym", "u2", 0),
    ]
    first_params = get.call_args_list[0].kwargs["params"]
    assert first_params["filter[owners.id]"] == "me"
    assert get.call_args_list[1].kwargs["params"]["page[cursor]"] == "c2"


def test_tidal_read_playlist_parses_tracks_only(tidal_env: Path) -> None:
    page = {
        "data": [{"type": "tracks", "id": "1"}, {"type": "videos", "id": "9"}],
        "included": [
            {
                "type": "tracks",
                "id": "1",
                "attributes": {"title": "Song"},
                "relationships": {"artists": {"data": [{"type": "artists", "id": "a"}]}},
            },
            {"type": "artists", "id": "a", "attributes": {"name": "Band"}},
            {"type": "videos", "id": "9", "attributes": {"title": "Clip"}},
        ],
        "links": {},
    }
    provider, get = _tidal(tidal_env, [page])

    playlist = provider.read_playlist(
        PlaylistSummary(platform="tidal", name="Mix", remote_id="u1")
    )

    assert [(t.name, t.artist) for t in playlist.tracks] == [("Song", "Band")]
    assert get.call_args.args[0].endswith("/playlists/u1/relationships/items")
    assert get.call_args.kwargs["params"]["include"].startswith("items")


# ---------------------------------------------------------------------------
# B1 — requested scopes never narrow the cached grant
# ---------------------------------------------------------------------------

_LIKED_AND_PLAYLIST = "user-library-read playlist-read-private playlist-read-collaborative"


def _write_spotify_cache(base: Path, scope: str, *, expires_in_sec: int = 3600) -> None:
    import time

    (base / ".cache").write_text(
        json.dumps(
            {
                "access_token": "tok",
                "token_type": "Bearer",
                "expires_in": 3600,
                "refresh_token": "ref",
                "scope": scope,
                "expires_at": int(time.time()) + expires_in_sec,
            }
        ),
        encoding="utf-8",
    )


def _requested(base: Path, scope: PlaylistScope, need_write: bool = False) -> set[str]:
    provider = SpotifyProvider(
        base_dir=base,
        unmatched_log_path=base / "u.log",
        need_write=need_write,
        playlist_scope=scope,
    )
    return set(provider._load_config(need_write)["scope"].split())  # noqa: SLF001


def test_spotify_liked_run_after_playlist_run_keeps_playlist_scopes(
    spotify_env: Path,
) -> None:
    _write_spotify_cache(spotify_env, _LIKED_AND_PLAYLIST)

    assert _requested(spotify_env, PlaylistScope.NONE) == set(_LIKED_AND_PLAYLIST.split())
    assert "playlist-read-private" in _requested(
        spotify_env, PlaylistScope.NONE, need_write=True
    )


def test_spotify_write_scope_only_requested_with_apply(spotify_env: Path) -> None:
    _write_spotify_cache(spotify_env, _LIKED_AND_PLAYLIST)

    read_only = _requested(spotify_env, PlaylistScope.READ)
    with_apply = _requested(spotify_env, PlaylistScope.WRITE)

    assert not read_only & {"user-library-modify", "playlist-modify-private"}
    assert {"playlist-modify-private", "playlist-modify-public"} <= with_apply


def test_spotify_playlist_read_after_liked_run_does_not_reauthorize(
    spotify_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_spotify_cache(spotify_env, _LIKED_AND_PLAYLIST)
    monkeypatch.setattr(
        spotify_mod.SpotifyOAuth,
        "get_auth_response",
        MagicMock(side_effect=AssertionError("interactive OAuth started")),
    )
    liked = SpotifyProvider(base_dir=spotify_env, unmatched_log_path=spotify_env / "u.log")
    playlists = _spotify(spotify_env, scope=PlaylistScope.READ)

    for provider in (liked, playlists):
        auth = provider._get_client().auth_manager  # noqa: SLF001
        assert auth.get_access_token(as_dict=False) == "tok"


# ---------------------------------------------------------------------------
# B2 — web-built providers never start an interactive OAuth
# ---------------------------------------------------------------------------


def test_spotify_non_interactive_raises_instead_of_opening_browser(
    spotify_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_spotify_cache(spotify_env, "user-library-read")  # lacks playlist scope
    browser = MagicMock()
    monkeypatch.setattr("webbrowser.open", browser)
    provider = SpotifyProvider(
        base_dir=spotify_env,
        unmatched_log_path=spotify_env / "u.log",
        playlist_scope=PlaylistScope.READ,
        interactive=False,
    )

    with pytest.raises(PlatformOperationError, match="reconecta Spotify"):
        provider.list_playlists()
    browser.assert_not_called()


def test_tidal_non_interactive_expired_unrefreshable_token_raises(
    tidal_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from musicsync.infrastructure.tidal_provider import TidalError

    browser = MagicMock()
    monkeypatch.setattr("webbrowser.open", browser)
    session = requests.Session()
    session.post = MagicMock(return_value=MagicMock(status_code=400))  # type: ignore[method-assign]
    session.get = MagicMock()  # type: ignore[method-assign]
    provider = TidalProvider(base_dir=tidal_env, session=session, interactive=False)
    provider._write_cache(  # noqa: SLF001
        {"access_token": "old", "refresh_token": "ref", "expires_in": 1, "obtained_at": 0.0}
    )

    with pytest.raises(TidalError, match="reconecta Tidal"):
        provider.list_playlists()
    with pytest.raises(TidalError, match="reconecta Tidal"):
        provider.read_liked()
    browser.assert_not_called()
    session.get.assert_not_called()


# ---------------------------------------------------------------------------
# B5 — never write a track without an artist; Tidal playlists enrich artists
# ---------------------------------------------------------------------------


def test_spotify_mirror_add_with_empty_artist_never_searches(spotify_env: Path) -> None:
    sp = MagicMock()
    sp.playlist_items.return_value = _page([])
    title_only = Track(name="Intro", artist="")

    result = _spotify(spotify_env, sp, PlaylistScope.WRITE).add_to_playlist(
        "Mix", "p1", [title_only]
    )

    sp.search.assert_not_called()
    sp.playlist_add_items.assert_not_called()
    assert result.unresolved == [title_only]
    assert result.added == []


def test_tidal_read_playlist_fetches_artist_missing_from_included(tidal_env: Path) -> None:
    page = {
        "data": [{"type": "tracks", "id": "1"}],
        "included": [
            {
                "type": "tracks",
                "id": "1",
                "attributes": {"title": "212"},
                "relationships": {"artists": {"data": [{"type": "artists", "id": "a1"}]}},
            }
        ],
        "links": {},
    }
    artist = {"data": {"type": "artists", "id": "a1", "attributes": {"name": "Azealia Banks"}}}
    provider, get = _tidal(tidal_env, [page, artist])

    playlist = provider.read_playlist(
        PlaylistSummary(platform="tidal", name="Mix", remote_id="u1")
    )

    assert [(t.name, t.artist) for t in playlist.tracks] == [("212", "Azealia Banks")]
    assert get.call_args.args[0].endswith("/artists/a1")


def test_apple_add_script_never_looks_up_playlists_by_name() -> None:
    script = (
        Path(__file__).parent.parent / "applescript" / "add_to_playlist.applescript"
    ).read_text(encoding="utf-8")
    code = "\n".join(
        line for line in script.splitlines() if not line.lstrip().startswith("--")
    )

    assert "whose persistent ID is plPid" in code
    assert "whose name is plName" not in code
    assert "make new user playlist with properties {name:plName}" in code


def test_apple_reserves_system_playlist_names(tmp_path: Path) -> None:
    apple = _apple(tmp_path)

    assert "favourite songs" in apple.reserved_playlist_names
    assert "music videos" in apple.reserved_playlist_names
    assert SpotifyProvider.reserved_playlist_names == frozenset()
    assert TidalProvider.reserved_playlist_names == frozenset()


def test_apple_list_playlists_flags_smart_and_folders_not_owned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stdout = (
        "PID1\tRoad Trip\t12\t1\n"
        "PID2\tWorkout\t40\t0\n"  # smart playlist or folder
        "PID3\tLegacy\t3\n"  # 3-field line from an older script
    )
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: _completed(stdout))

    summaries = _apple(tmp_path).list_playlists()

    assert [(s.name, s.owned) for s in summaries] == [
        ("Road Trip", True),
        ("Workout", False),
        ("Legacy", True),
    ]


def test_apple_add_script_errors_when_given_id_is_not_found() -> None:
    script = (
        Path(__file__).parent.parent / "applescript" / "add_to_playlist.applescript"
    ).read_text(encoding="utf-8")
    code = [line.strip() for line in script.splitlines() if not line.lstrip().startswith("--")]
    lookup = code.index("if plPid is not \"\" then")
    end = code.index("end if", code.index("end try", lookup))

    id_branch = code[lookup:end]
    assert 'if pl is missing value then error "playlist " & plPid & " no encontrada"' in id_branch
    assert not any("make new" in line for line in id_branch)
