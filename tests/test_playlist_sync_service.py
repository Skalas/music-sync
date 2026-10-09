"""P4–P6 — PlaylistSyncService: dry-run, guarded mirror, graceful degradation."""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

import pytest

from musicsync.application import playlist_sync_service
from musicsync.application.playlist_sync_service import (
    PlaylistSyncOptions,
    PlaylistSyncService,
)
from musicsync.domain.errors import PlatformOperationError
from musicsync.domain.platforms import PLATFORMS
from musicsync.domain.track import Track
from musicsync.infrastructure.sqlite_repository import SqliteTrackRepository
from tests.playlist_fakes import FakePlaylistProvider, track

A, B, C, D = track("Song A"), track("Song B"), track("Song C"), track("Song D")


@pytest.fixture
def repo(tmp_path: Path) -> Iterator[SqliteTrackRepository]:
    repository = SqliteTrackRepository(tmp_path / "lib.db")
    yield repository
    repository.close()


@pytest.fixture
def review(tmp_path: Path) -> Path:
    return tmp_path / "playlists_review.txt"


def _providers() -> tuple[FakePlaylistProvider, FakePlaylistProvider, FakePlaylistProvider]:
    spotify = FakePlaylistProvider("spotify", {"Road Trip": [A, B]})
    apple = FakePlaylistProvider("apple", {"road trip": [B, C], "Other": [D]})
    tidal = FakePlaylistProvider("tidal", {"ROAD TRIP": [D]}, can_playlist_write=False)
    return spotify, apple, tidal


def _service(
    repo: SqliteTrackRepository, review: Path, *providers: FakePlaylistProvider
) -> PlaylistSyncService:
    return PlaylistSyncService(repo, list(providers), review_path=review)


def _names(tracks: list[Track]) -> list[str]:
    return [t.name for t in tracks]


def _writes(*providers: FakePlaylistProvider) -> int:
    return sum(len(p.add_calls) for p in providers)


# ---------------------------------------------------------------------------
# P4 — dry run
# ---------------------------------------------------------------------------


def test_dry_run_computes_per_playlist_diff_and_writes_nothing(
    repo: SqliteTrackRepository, review: Path
) -> None:
    spotify, apple, tidal = _providers()

    result = _service(repo, review, spotify, apple, tidal).run(
        PlaylistSyncOptions(
            names=("Road Trip",), apply_platforms=frozenset({"spotify", "apple"}), dry_run=True
        )
    )

    diff = result.diffs["road trip"]
    assert set(diff.to_add) == {"spotify", "apple"}  # Tidal is read-only: never a target
    assert _names(diff.to_add["spotify"]) == ["Song C", "Song D"]
    assert _names(diff.to_add["apple"]) == ["Song A", "Song D"]
    assert _writes(spotify, apple, tidal) == 0
    assert not review.exists()
    assert set(repo.get_playlists({"road trip"})) == {"spotify", "apple", "tidal"}


def test_dry_run_only_reads_selected_playlists(
    repo: SqliteTrackRepository, review: Path
) -> None:
    spotify, apple, tidal = _providers()

    result = _service(repo, review, spotify, apple, tidal).run(
        PlaylistSyncOptions(names=("Other", "Ghost"), dry_run=True)
    )

    assert list(result.diffs) == ["other"]
    assert _names(result.diffs["other"].to_add["spotify"]) == ["Song D"]
    assert result.missing_names == ["ghost"]
    assert repo.get_playlists({"road trip"}) == {}


def test_dry_run_semantics_without_apply_flags_zero_remote_writes(
    repo: SqliteTrackRepository, review: Path
) -> None:
    spotify, apple, tidal = _providers()

    result = _service(repo, review, spotify, apple, tidal).run(
        PlaylistSyncOptions(names=("Road Trip",))
    )

    assert _writes(spotify, apple, tidal) == 0
    assert result.applied == {}
    text = review.read_text(encoding="utf-8")
    assert "[PENDIENTE] «Road Trip» -> apple (2)" in text
    assert "Song A - Artist" in text


def test_dry_run_with_no_names_is_a_no_op(repo: SqliteTrackRepository, review: Path) -> None:
    spotify, apple, tidal = _providers()

    result = _service(repo, review, spotify, apple, tidal).run(
        PlaylistSyncOptions(names=("  ",), dry_run=True)
    )

    assert result.diffs == {}
    assert spotify.add_calls == apple.add_calls == []


# ---------------------------------------------------------------------------
# P5 — guarded mirror
# ---------------------------------------------------------------------------


def test_mirror_writes_only_opted_in_platform(
    repo: SqliteTrackRepository, review: Path
) -> None:
    spotify, apple, tidal = _providers()

    result = _service(repo, review, spotify, apple, tidal).run(
        PlaylistSyncOptions(names=("Road Trip",), apply_platforms=frozenset({"apple"}))
    )

    assert spotify.add_calls == []
    assert len(apple.add_calls) == 1
    call = apple.add_calls[0]
    assert call.remote_id == "apple:road trip"  # existing target playlist reused
    assert _names(call.tracks) == ["Song A", "Song D"]
    assert result.applied == {"Road Trip": {"apple": 2}}
    assert "[PENDIENTE] «Road Trip» -> spotify (2)" in review.read_text(encoding="utf-8")


def test_mirror_creates_absent_playlist_and_records_remote_id(
    repo: SqliteTrackRepository, review: Path
) -> None:
    spotify = FakePlaylistProvider("spotify", {"Fresh": [A]})
    apple = FakePlaylistProvider("apple")

    _service(repo, review, spotify, apple).run(
        PlaylistSyncOptions(names=("Fresh",), apply_platforms=frozenset({"apple"}))
    )

    assert apple.add_calls[0].remote_id is None
    stored = repo.get_playlists({"fresh"})["apple"][0]
    assert stored.remote_id == "apple:Fresh"
    assert repo.get_playlist_synced_keys({"fresh"}) == {"fresh": {"apple": {A.key}}}


def test_mirror_rerun_is_idempotent_and_never_duplicates(
    repo: SqliteTrackRepository, review: Path
) -> None:
    spotify, apple, tidal = _providers()
    service = _service(repo, review, spotify, apple, tidal)
    options = PlaylistSyncOptions(
        names=("Road Trip",), apply_platforms=frozenset({"spotify", "apple"})
    )

    service.run(options)
    second = service.run(options)

    assert len(spotify.add_calls) == len(apple.add_calls) == 1
    assert second.diffs["road trip"].total == 0
    assert _names(apple.playlists["road trip"]) == ["Song B", "Song C", "Song A", "Song D"]


def test_mirror_unresolved_tracks_go_to_review_and_are_retried(
    repo: SqliteTrackRepository, review: Path
) -> None:
    spotify = FakePlaylistProvider("spotify", {"Mix": [A, B]})
    apple = FakePlaylistProvider("apple", {"Mix": []}, unresolvable={"Song B"})
    service = _service(repo, review, spotify, apple)
    options = PlaylistSyncOptions(names=("Mix",), apply_platforms=frozenset({"apple"}))

    result = service.run(options)

    assert _names(result.unresolved["Mix"]["apple"]) == ["Song B"]
    assert "[SIN MATCH] «Mix» -> apple (1)" in review.read_text(encoding="utf-8")
    assert _names(service.run(options).diffs["mix"].to_add["apple"]) == ["Song B"]


def test_mirror_never_targets_read_only_platform(
    repo: SqliteTrackRepository, review: Path
) -> None:
    spotify, apple, tidal = _providers()

    _service(repo, review, spotify, apple, tidal).run(
        PlaylistSyncOptions(
            names=("Road Trip",), apply_platforms=frozenset({"tidal", "apple"})
        )
    )

    assert tidal.add_calls == []
    assert len(apple.add_calls) == 1


def test_mirror_service_has_no_platform_name_branches() -> None:
    source = Path(playlist_sync_service.__file__).read_text(encoding="utf-8")
    literals = {
        node.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert not literals & set(PLATFORMS)


# ---------------------------------------------------------------------------
# P6 — graceful degradation
# ---------------------------------------------------------------------------


def test_graceful_read_failure_skips_only_that_platform(
    repo: SqliteTrackRepository, review: Path
) -> None:
    spotify, apple, tidal = _providers()
    tidal.read_error = RuntimeError("HTTP 403\nstack details")

    result = _service(repo, review, spotify, apple, tidal).run(
        PlaylistSyncOptions(names=("Road Trip",), apply_platforms=frozenset({"apple"}))
    )

    assert result.skipped == {"tidal": "HTTP 403"}
    assert _names(apple.add_calls[0].tracks) == ["Song A"]


def test_graceful_target_read_failure_is_not_written(
    repo: SqliteTrackRepository, review: Path
) -> None:
    spotify, apple, tidal = _providers()
    spotify.read_error = PlatformOperationError("token sin scope")

    result = _service(repo, review, spotify, apple, tidal).run(
        PlaylistSyncOptions(
            names=("Road Trip",), apply_platforms=frozenset({"spotify", "apple"})
        )
    )

    assert "spotify" in result.skipped
    assert spotify.add_calls == []
    assert set(result.diffs["road trip"].to_add) == {"apple"}
    assert _names(apple.add_calls[0].tracks) == ["Song D"]


def test_graceful_write_failure_does_not_stop_other_platform(
    repo: SqliteTrackRepository, review: Path
) -> None:
    spotify, apple, tidal = _providers()
    spotify.write_error = PlatformOperationError("HTTP 403 — scope insuficiente")

    result = _service(repo, review, spotify, apple, tidal).run(
        PlaylistSyncOptions(
            names=("Road Trip",), apply_platforms=frozenset({"spotify", "apple"})
        )
    )

    assert result.skipped == {"spotify": "HTTP 403 — scope insuficiente"}
    assert result.applied == {"Road Trip": {"apple": 2}}
    assert repo.get_playlist_synced_keys({"road trip"}) == {
        "road trip": {"apple": {A.key, D.key}}
    }


def test_graceful_listing_reports_failed_platform(repo: SqliteTrackRepository) -> None:
    spotify, apple, tidal = _providers()
    apple.read_error = PlatformOperationError("'osascript' no está disponible")

    listing = PlaylistSyncService(repo, [spotify, apple, tidal]).list_playlists()

    assert set(listing.playlists) == {"spotify", "tidal"}
    assert listing.skipped == {"apple": "'osascript' no está disponible"}


def test_graceful_system_exit_propagates_unless_web_opts_in(
    repo: SqliteTrackRepository, review: Path
) -> None:
    spotify, apple, _ = _providers()
    spotify.read_error = SystemExit("faltan credenciales")
    service = _service(repo, review, spotify, apple)

    with pytest.raises(SystemExit):
        service.run(PlaylistSyncOptions(names=("Road Trip",), dry_run=True))

    result = service.run(
        PlaylistSyncOptions(
            names=("Road Trip",), dry_run=True, skip_unavailable_providers=True
        )
    )
    assert result.skipped == {"spotify": "faltan credenciales"}


# ---------------------------------------------------------------------------
# Freshness (B3) and ambiguous names (B4)
# ---------------------------------------------------------------------------


def test_mirror_deleted_remote_playlist_recreates_instead_of_stale_id(
    repo: SqliteTrackRepository, review: Path
) -> None:
    spotify, apple, tidal = _providers()
    service = _service(repo, review, spotify, apple, tidal)
    options = PlaylistSyncOptions(names=("Road Trip",), apply_platforms=frozenset({"apple"}))
    service.run(options)
    apple.playlists.pop("road trip")  # user deleted it in Music.app

    service.run(options)

    second = apple.add_calls[1]
    assert second.remote_id is None
    assert _names(second.tracks) == ["Song A", "Song B", "Song D"]


def test_graceful_excluded_platform_old_rows_neither_source_nor_target(
    repo: SqliteTrackRepository, review: Path
) -> None:
    spotify, apple, tidal = _providers()
    _service(repo, review, spotify, apple, tidal).run(
        PlaylistSyncOptions(names=("Road Trip",), dry_run=True)
    )

    result = _service(repo, review, spotify, apple).run(  # --no-tidal
        PlaylistSyncOptions(names=("Road Trip",), dry_run=True)
    )

    assert _names(result.diffs["road trip"].to_add["spotify"]) == ["Song C"]
    assert _names(result.diffs["road trip"].to_add["apple"]) == ["Song A"]


def test_graceful_failed_read_old_rows_neither_source_nor_target(
    repo: SqliteTrackRepository, review: Path
) -> None:
    spotify, apple, tidal = _providers()
    service = _service(repo, review, spotify, apple, tidal)
    service.run(PlaylistSyncOptions(names=("Road Trip",), dry_run=True))
    tidal.read_error = RuntimeError("HTTP 500")
    apple.read_error = RuntimeError("Music no responde")

    result = service.run(
        PlaylistSyncOptions(
            names=("Road Trip",), apply_platforms=frozenset({"spotify", "apple"})
        )
    )

    assert set(result.skipped) == {"tidal", "apple"}
    assert result.diffs["road trip"].to_add == {"spotify": []}
    assert spotify.add_calls == apple.add_calls == []


def test_graceful_duplicate_names_skip_platform_for_that_name(
    repo: SqliteTrackRepository, review: Path
) -> None:
    spotify = FakePlaylistProvider(
        "spotify", {"Gym": [A], "gym ": [B], "Road Trip": [C]}
    )
    apple = FakePlaylistProvider("apple", {"Gym": [D], "Road Trip": []})

    result = _service(repo, review, spotify, apple).run(
        PlaylistSyncOptions(
            names=("Gym", "Road Trip"), apply_platforms=frozenset({"spotify", "apple"})
        )
    )

    assert result.ambiguous == {"gym": ["spotify"]}
    assert result.diffs["gym"].to_add == {"apple": []}
    assert "gym" not in result.missing_names
    assert "spotify" not in repo.get_playlists({"gym"})
    assert [_names(c.tracks) for c in apple.add_calls] == [["Song C"]]
    assert spotify.add_calls == []


# ---------------------------------------------------------------------------
# Title-only tracks (B5) and non-owned / collaborative playlists (B6)
# ---------------------------------------------------------------------------


def test_mirror_lone_title_only_track_is_not_written_and_goes_to_review(
    repo: SqliteTrackRepository, review: Path
) -> None:
    lone = Track(name="Intro", artist="")
    spotify = FakePlaylistProvider("spotify", {"Mix": [A]})
    apple = FakePlaylistProvider("apple", {"Mix": []})
    tidal = FakePlaylistProvider("tidal", {"Mix": [lone]}, can_playlist_write=False)

    result = _service(repo, review, spotify, apple, tidal).run(
        PlaylistSyncOptions(names=("Mix",), apply_platforms=frozenset({"spotify", "apple"}))
    )

    written = [t for p in (spotify, apple) for c in p.add_calls for t in c.tracks]
    assert lone not in written
    assert result.diffs["mix"].unresolved == [lone]
    assert "[SIN ARTISTA] «Mix» (1)" in review.read_text(encoding="utf-8")


def test_mirror_title_only_with_artisted_twin_is_not_missing(
    repo: SqliteTrackRepository, review: Path
) -> None:
    spotify = FakePlaylistProvider("spotify", {"Mix": [A]})
    tidal = FakePlaylistProvider(
        "tidal", {"Mix": [Track(name="Song A", artist="")]}, can_playlist_write=False
    )

    result = _service(repo, review, spotify, tidal).run(
        PlaylistSyncOptions(names=("Mix",), apply_platforms=frozenset({"spotify"}))
    )

    assert result.diffs["mix"].to_add == {"spotify": []}
    assert spotify.add_calls == []


def test_mirror_followed_same_name_is_ambiguous_and_never_created(
    repo: SqliteTrackRepository, review: Path
) -> None:
    spotify = FakePlaylistProvider("spotify", {"Gym": [B]}, followed={"Gym"})
    apple = FakePlaylistProvider("apple", {"Gym": [A]})

    result = _service(repo, review, spotify, apple).run(
        PlaylistSyncOptions(names=("Gym",), apply_platforms=frozenset({"spotify", "apple"}))
    )

    assert result.ambiguous == {"gym": ["spotify"]}
    assert spotify.add_calls == []
    assert apple.add_calls == []  # followed playlist is not a source either


def test_graceful_listing_hides_followed_playlists(repo: SqliteTrackRepository) -> None:
    spotify = FakePlaylistProvider("spotify", {"Mine": [A], "Theirs": [B]}, followed={"Theirs"})

    listing = PlaylistSyncService(repo, [spotify]).list_playlists()

    assert [s.name for s in listing.playlists["spotify"]] == ["Mine"]


def test_mirror_own_collaborative_spotify_playlist_is_target_not_recreated(
    repo: SqliteTrackRepository,
    review: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import MagicMock

    from musicsync.infrastructure import spotify_provider as spotify_mod
    from musicsync.infrastructure.spotify_provider import PlaylistScope, SpotifyProvider

    sp = MagicMock()
    sp.current_user.return_value = {"id": "me"}
    sp.current_user_playlists.return_value = {
        "items": [{"id": "gym1", "name": "Gym", "owner": {"id": "me"}, "collaborative": True}],
        "next": None,
    }
    sp.playlist_items.return_value = {"items": [], "next": None}
    sp.search.return_value = {"tracks": {"items": [{"id": "sA"}]}}
    monkeypatch.setattr(spotify_mod, "SPOTIFY_SEARCH_DELAY_SEC", 0)
    spotify = SpotifyProvider(
        base_dir=tmp_path,
        unmatched_log_path=tmp_path / "u.log",
        playlist_scope=PlaylistScope.WRITE,
        client=sp,
    )
    apple = FakePlaylistProvider("apple", {"Gym": [A]})

    PlaylistSyncService(repo, [spotify, apple], review_path=review).run(
        PlaylistSyncOptions(names=("Gym",), apply_platforms=frozenset({"spotify"}))
    )

    sp.current_user_playlist_create.assert_not_called()
    sp.playlist_add_items.assert_called_once_with("gym1", ["sA"])


def test_mirror_system_playlist_name_is_reserved_for_that_platform(
    repo: SqliteTrackRepository, review: Path
) -> None:
    from musicsync.infrastructure.apple_provider import SYSTEM_PLAYLIST_NAMES

    spotify = FakePlaylistProvider("spotify", {"Favourite Songs": [A]})
    apple = FakePlaylistProvider(
        "apple", {"Favourite Songs": [B]}, reserved_playlist_names=SYSTEM_PLAYLIST_NAMES
    )

    result = _service(repo, review, spotify, apple).run(
        PlaylistSyncOptions(
            names=("Favourite Songs",), apply_platforms=frozenset({"spotify", "apple"})
        )
    )

    assert result.ambiguous == {"favourite songs": ["apple"]}
    assert apple.add_calls == []
    assert spotify.add_calls == []  # Apple's system list is not a source either
    assert "apple" not in repo.get_playlists({"favourite songs"})


def _apple_with_scripts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outputs: dict[str, object]
) -> tuple[object, list[str]]:
    """Real AppleProvider whose osascript calls are answered per script name."""
    import subprocess

    from musicsync.infrastructure.apple_provider import AppleProvider

    called: list[str] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        script = Path(cmd[1]).name
        called.append(script)
        answer = outputs[script]
        if isinstance(answer, BaseException):
            raise answer
        return subprocess.CompletedProcess(cmd, 0, stdout=str(answer), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    provider = AppleProvider(applescript_dir=tmp_path, output_path=tmp_path / "o.txt")
    return provider, called


def test_mirror_smart_or_folder_same_name_is_ambiguous_and_never_created(
    repo: SqliteTrackRepository,
    review: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    apple, called = _apple_with_scripts(
        tmp_path, monkeypatch, {"read_playlists.applescript": "PID9\tWorkout\t40\t0\n"}
    )
    spotify = FakePlaylistProvider("spotify", {"Workout": [A]})

    result = PlaylistSyncService(repo, [spotify, apple], review_path=review).run(  # type: ignore[list-item]
        PlaylistSyncOptions(names=("Workout",), apply_platforms=frozenset({"apple"}))
    )

    assert result.ambiguous == {"workout": ["apple"]}
    assert called == ["read_playlists.applescript"]  # never read, never add/create


def test_graceful_apple_add_failure_for_missing_id_skips_platform(
    repo: SqliteTrackRepository,
    review: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess

    failure = subprocess.CalledProcessError(
        1, ["osascript"], stderr="playlist PID1 no encontrada"
    )
    apple, called = _apple_with_scripts(
        tmp_path,
        monkeypatch,
        {
            "read_playlists.applescript": "PID1\tMix\t0\t1\n",
            "read_playlist_tracks.applescript": "",
            "add_to_playlist.applescript": failure,
        },
    )
    spotify = FakePlaylistProvider("spotify", {"Mix": [A]})

    result = PlaylistSyncService(repo, [spotify, apple], review_path=review).run(  # type: ignore[list-item]
        PlaylistSyncOptions(names=("Mix",), apply_platforms=frozenset({"apple", "spotify"}))
    )

    assert "add_to_playlist.applescript" in result.skipped["apple"]
    assert called[-1] == "add_to_playlist.applescript"
    assert result.applied == {}
    assert repo.get_playlist_synced_keys({"mix"}) == {}
