"""P7 — CLI --list-playlists / --mirror-playlist (providers faked, no network)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

import sync_music
from tests.playlist_fakes import FakePlaylistProvider, track

A, B = track("Song A"), track("Song B")


@pytest.fixture
def fakes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[dict[str, Any], list[FakePlaylistProvider]]:
    """Patch the playlist factory; record the kwargs the CLI builds it with."""
    built: dict[str, Any] = {}
    providers = [
        FakePlaylistProvider("spotify", {"Road Trip": [A], "Gym\tMix": [A, B]}),
        FakePlaylistProvider("apple", {"Road Trip": [B]}),
    ]

    def fake_build(base_dir: Path, **kwargs: Any) -> list[FakePlaylistProvider]:
        built.update(kwargs)
        return [p for p in providers if kwargs.get(f"include_{p.name}", True)]

    monkeypatch.setattr(sync_music, "build_playlist_providers", fake_build)
    monkeypatch.setattr(sync_music, "PLAYLIST_REVIEW_PATH", tmp_path / "review.txt")
    return built, providers


def _run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *argv: str) -> None:
    monkeypatch.setattr(
        sys, "argv", ["sync_music.py", "--db", str(tmp_path / "lib.db"), *argv]
    )
    sync_music.main()


def test_list_playlists_prints_platform_name_count_lines(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    fakes: tuple[dict[str, Any], list[FakePlaylistProvider]],
) -> None:
    built, providers = fakes

    _run(monkeypatch, tmp_path, "--list-playlists", "--no-tidal")

    lines = capsys.readouterr().out.splitlines()
    assert lines == [
        "spotify\tRoad Trip\t1",
        "spotify\tGym Mix\t2",
        "apple\tRoad Trip\t1",
    ]
    assert built["include_tidal"] is False
    assert built["write_spotify"] is False
    assert all(p.add_calls == [] for p in providers)


def test_list_playlists_exits_nonzero_when_every_platform_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fakes: tuple[dict[str, Any], list[FakePlaylistProvider]],
) -> None:
    for provider in fakes[1]:
        provider.read_error = RuntimeError("down")

    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, tmp_path, "--list-playlists")

    assert "No se pudo listar" in str(exc.value.code)


def test_mirror_playlist_default_run_makes_zero_remote_writes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fakes: tuple[dict[str, Any], list[FakePlaylistProvider]],
) -> None:
    built, providers = fakes

    _run(monkeypatch, tmp_path, "--mirror-playlist", "road trip")

    assert built["write_spotify"] is False
    assert all(p.add_calls == [] for p in providers)
    assert "PENDIENTE" in (tmp_path / "review.txt").read_text(encoding="utf-8")


def test_mirror_playlist_applies_only_opted_in_platform(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fakes: tuple[dict[str, Any], list[FakePlaylistProvider]],
) -> None:
    built, (spotify, apple) = fakes

    _run(
        monkeypatch,
        tmp_path,
        "--mirror-playlist",
        "Road Trip",
        "--mirror-playlist",
        "Nope",
        "--apply-spotify",
    )

    assert built["write_spotify"] is True
    assert [c.tracks for c in spotify.add_calls] == [[B]]
    assert apple.add_calls == []


def test_mirror_playlist_dry_run_with_apply_still_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fakes: tuple[dict[str, Any], list[FakePlaylistProvider]],
) -> None:
    _run(
        monkeypatch, tmp_path, "--mirror-playlist", "Road Trip", "--apply-apple", "--dry-run"
    )

    assert all(p.add_calls == [] for p in fakes[1])


@pytest.mark.parametrize(
    "argv",
    [
        ["--list-playlists", "--apply-spotify"],
        ["--list-playlists", "--mirror-playlist", "X"],
        ["--mirror-playlist", "X", "--apply-tidal"],
        ["--mirror-playlist", "X", "--offline"],
        ["--mirror-playlist", "X", "--export", "out.csv"],
    ],
)
def test_playlist_flags_reject_conflicting_options(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["sync_music.py", *argv])

    with pytest.raises(SystemExit) as exc:
        sync_music.parse_args()

    assert exc.value.code == 2
    assert "incompatible" in capsys.readouterr().err


def test_liked_flow_does_not_build_playlist_providers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> list[Any]:
        raise AssertionError("playlist providers built for a liked-songs run")

    monkeypatch.setattr(sync_music, "build_playlist_providers", forbidden)
    seeded = tmp_path / "lib.db"
    seeded.touch()

    _run(monkeypatch, tmp_path, "--offline", "--dry-run")
