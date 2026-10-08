"""Apple Music provider: two-phase apply, status parsing, failure handling."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from musicsync.domain.errors import PlatformOperationError
from musicsync.domain.track import Track
from musicsync.infrastructure.apple_provider import (
    AppleProvider,
    _partition_by_status,
    _search_line,
    _tsv_field,
)


def _provider(tmp_path: Path) -> AppleProvider:
    return AppleProvider(
        applescript_dir=tmp_path, output_path=tmp_path / "to_apple.txt"
    )


def _completed(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["osascript"], returncode=returncode, stdout=stdout, stderr=""
    )


# --- status parsing ---------------------------------------------------------


def test_partition_splits_favorited_missing_and_failed() -> None:
    tracks = [
        Track(name="A", artist="X"),
        Track(name="B", artist="Y"),
        Track(name="C", artist="Z"),
    ]
    stdout = "OK\tA\tX\nMISSING\tB\tY\nERROR\tC\tZ\n"

    outcome = _partition_by_status(tracks, stdout)

    assert [t.name for t in outcome.favorited] == ["A"]
    assert [t.name for t in outcome.missing] == ["B"]
    assert [t.name for t in outcome.failed] == ["C"]


def test_partition_pairs_positionally_so_duplicate_lines_stay_distinct() -> None:
    tracks = [Track(name="Dup", artist="X"), Track(name="Dup", artist="X")]

    outcome = _partition_by_status(tracks, "OK\tDup\tX\nMISSING\tDup\tX\n")

    assert len(outcome.favorited) == 1
    assert len(outcome.missing) == 1


def test_partition_treats_unreported_tail_as_failed_not_synced() -> None:
    tracks = [Track(name="A", artist="X"), Track(name="B", artist="Y")]

    outcome = _partition_by_status(tracks, "OK\tA\tX\n")

    assert [t.name for t in outcome.favorited] == ["A"]
    assert [t.name for t in outcome.failed] == ["B"]


def test_tsv_field_flattens_embedded_tabs_and_newlines() -> None:
    assert _tsv_field("a\tb\nc") == "a b c"


def test_search_line_uses_primary_artist_only() -> None:
    """The catalog credits one artist; a comma-joined list matches nothing."""
    track = Track(name="212", artist="Azealia Banks, Lazy Jay")

    assert _search_line(track) == "212 - Azealia Banks"


def test_search_line_keeps_a_single_artist_intact() -> None:
    track = Track(name="Wonderwall", artist="Oasis")

    assert _search_line(track) == "Wonderwall - Oasis"


def test_search_line_falls_back_when_split_would_empty_the_artist() -> None:
    track = Track(name="Track", artist="& Friends")

    assert _search_line(track) == "Track - & Friends"


# --- two-phase apply -------------------------------------------------------


def test_apply_skips_shortcut_when_everything_is_already_in_library(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _provider(tmp_path)
    tracks = [Track(name="A", artist="X"), Track(name="B", artist="Y")]
    commands: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(cmd)
        return _completed("OK\tA\tX\nOK\tB\tY\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    applied = provider.apply_likes(tracks)

    assert applied == tracks
    assert all("shortcuts" not in cmd[0] for cmd in commands)
    assert not (tmp_path / "to_apple.txt").exists()


def test_apply_runs_shortcut_only_for_missing_tracks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _provider(tmp_path)
    present = Track(name="Have", artist="X")
    absent = Track(name="Need", artist="Y")
    commands: list[list[str]] = []
    outputs = iter(["OK\tHave\tX\nMISSING\tNeed\tY\n", "OK\tNeed\tY\n"])

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(cmd)
        if cmd[0] == "shortcuts":
            return _completed()
        return _completed(next(outputs))

    monkeypatch.setattr(subprocess, "run", fake_run)
    applied = provider.apply_likes([present, absent])

    assert applied == [present, absent]
    assert [cmd[0] for cmd in commands] == ["osascript", "shortcuts", "osascript"]
    # Only the missing track is handed to the Shortcut.
    assert (tmp_path / "to_apple.txt").read_text(encoding="utf-8") == "Need - Y"


def test_shortcut_input_drops_secondary_artists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _provider(tmp_path)
    track = Track(name="212", artist="Azealia Banks, Lazy Jay")
    outputs = iter(["MISSING\t212\tAzealia Banks, Lazy Jay\n", "OK\t212\tAzealia Banks\n"])

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if cmd[0] == "shortcuts":
            return _completed()
        return _completed(next(outputs))

    monkeypatch.setattr(subprocess, "run", fake_run)
    provider.apply_likes([track])

    assert (tmp_path / "to_apple.txt").read_text(encoding="utf-8") == (
        "212 - Azealia Banks"
    )


def test_apply_checkpoints_only_confirmed_tracks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _provider(tmp_path)
    landed = Track(name="Landed", artist="X")
    lost = Track(name="Lost", artist="Y")
    checkpointed: list[Track] = []
    outputs = iter(["OK\tLanded\tX\nMISSING\tLost\tY\n", "MISSING\tLost\tY\n"])

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if cmd[0] == "shortcuts":
            return _completed()
        return _completed(next(outputs))

    monkeypatch.setattr(subprocess, "run", fake_run)
    applied = provider.apply_likes(
        [landed, lost], on_batch=lambda batch: checkpointed.extend(batch)
    )

    # The Shortcut never found "Lost", so it must not be recorded as synced.
    assert applied == [landed]
    assert checkpointed == [landed]


def test_apply_returns_empty_for_no_tracks(tmp_path: Path) -> None:
    assert _provider(tmp_path).apply_likes([]) == []


# --- failure handling ------------------------------------------------------


def test_favorite_failure_raises_platform_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _provider(tmp_path)

    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: _completed(returncode=1)
    )

    with pytest.raises(PlatformOperationError, match="Favorita"):
        provider.apply_likes([Track(name="A", artist="X")])


def test_trigger_shortcut_raises_platform_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _provider(tmp_path)

    def fail_shortcut(*args: object, **kwargs: object) -> None:
        raise subprocess.CalledProcessError(returncode=1, cmd=["shortcuts", "run"])

    monkeypatch.setattr(subprocess, "run", fail_shortcut)

    with pytest.raises(PlatformOperationError, match="SyncToAppleMusic"):
        provider._trigger_shortcut(tmp_path / "input.txt")  # noqa: SLF001


def test_read_liked_raises_when_osascript_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _provider(tmp_path)

    def no_osascript(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError("osascript")

    monkeypatch.setattr(subprocess, "run", no_osascript)

    with pytest.raises(PlatformOperationError, match="osascript"):
        provider.read_liked()


def test_read_liked_raises_on_applescript_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _provider(tmp_path)

    def fail_script(*args: object, **kwargs: object) -> None:
        raise subprocess.CalledProcessError(
            returncode=1, cmd=["osascript"], stderr="Music got an error"
        )

    monkeypatch.setattr(subprocess, "run", fail_script)

    with pytest.raises(PlatformOperationError, match="Music got an error"):
        provider.read_liked()


def test_shortcut_input_has_no_trailing_newline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A trailing newline becomes an empty item in the Shortcut's line split,
    i.e. an extra iteration that searches for "" and may add a junk track."""
    provider = _provider(tmp_path)
    tracks = [Track(name="A", artist="X"), Track(name="B", artist="Y")]
    outputs = iter(["MISSING\tA\tX\nMISSING\tB\tY\n", "OK\tA\tX\nOK\tB\tY\n"])

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if cmd[0] == "shortcuts":
            return _completed()
        return _completed(next(outputs))

    monkeypatch.setattr(subprocess, "run", fake_run)
    provider.apply_likes(tracks)

    written = (tmp_path / "to_apple.txt").read_text(encoding="utf-8")
    assert not written.endswith("\n")
    assert written.split("\n") == ["A - X", "B - Y"]
