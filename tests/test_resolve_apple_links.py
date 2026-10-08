"""T6 — batch resolve step (fake catalog lookup; never touches the network)."""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import sync_music
from musicsync.application.apple_links import NEGATIVE_RETRY_DAYS, resolve_apple_links
from musicsync.domain.apple_catalog import CatalogSong
from musicsync.domain.errors import PlatformOperationError
from musicsync.domain.track import Track
from musicsync.infrastructure.sqlite_repository import DatabaseError, SqliteTrackRepository

NOW = datetime(2026, 10, 8, tzinfo=timezone.utc)


class FakeLookup:
    storefront = "sv"

    def __init__(
        self,
        by_isrc: dict[str, list[CatalogSong]] | None = None,
        by_term: dict[str, list[CatalogSong]] | None = None,
        fail_on: str | None = None,
    ) -> None:
        self.by_isrc = by_isrc or {}
        self.by_term = by_term or {}
        self.fail_on = fail_on
        self.isrc_calls: list[str] = []
        self.search_calls: list[str] = []

    def songs_by_isrc(self, isrc: str) -> list[CatalogSong]:
        self.isrc_calls.append(isrc)
        return self.by_isrc.get(isrc, [])

    def search_songs(self, term: str) -> list[CatalogSong]:
        self.search_calls.append(term)
        if self.fail_on and self.fail_on in term:
            raise PlatformOperationError("GET /catalog/sv/search → HTTP 401")
        return self.by_term.get(term, [])


def _song(catalog_id: str, name: str, artist: str, duration: int = 200) -> CatalogSong:
    return CatalogSong(catalog_id=catalog_id, name=name, artist=artist, duration_sec=duration)


@pytest.fixture()
def repo(tmp_path: Path) -> Iterator[SqliteTrackRepository]:
    r = SqliteTrackRepository(tmp_path / "lib.db")
    yield r
    r.close()


def _catalog(repo: SqliteTrackRepository) -> dict[str, str | None]:
    rows = repo._conn.execute("SELECT key, catalog_id FROM apple_catalog").fetchall()  # noqa: SLF001
    return {row["key"]: row["catalog_id"] for row in rows}


def test_resolves_by_isrc_without_searching(repo: SqliteTrackRepository) -> None:
    track = Track(name="Song", artist="Artist", duration_sec=200, isrc="ISRC1")
    repo.upsert_presence("spotify", [track])
    lookup = FakeLookup(by_isrc={"ISRC1": [_song("111", "Song", "Artist")]})

    report = resolve_apple_links(repo, lookup, now=NOW)

    assert (report.resolved, report.unresolved) == (1, 0)
    assert _catalog(repo) == {track.key: "111"}
    assert lookup.search_calls == []


def test_falls_back_to_search_with_primary_artist(repo: SqliteTrackRepository) -> None:
    track = Track(name="Song", artist="Artist, Guest", duration_sec=200)
    repo.upsert_presence("spotify", [track])
    lookup = FakeLookup(by_term={"Song Artist": [_song("222", "Song", "Artist")]})

    resolve_apple_links(repo, lookup, now=NOW)

    assert lookup.search_calls == ["Song Artist"]
    assert _catalog(repo) == {track.key: "222"}


def test_unmatched_track_is_negative_cached(repo: SqliteTrackRepository) -> None:
    track = Track(name="Song", artist="Artist")
    repo.upsert_presence("spotify", [track])
    lookup = FakeLookup(by_term={"Song Artist": [_song("x", "Other", "Artist")]})

    report = resolve_apple_links(repo, lookup, now=NOW)
    again = resolve_apple_links(repo, lookup, now=NOW + timedelta(days=1))

    assert report.unresolved == 1
    assert _catalog(repo) == {track.key: None}
    assert (again.resolved, again.unresolved) == (0, 0)
    assert len(lookup.search_calls) == 1


def test_negative_cache_retried_after_window(repo: SqliteTrackRepository) -> None:
    track = Track(name="Song", artist="Artist", duration_sec=200)
    repo.upsert_presence("spotify", [track])
    resolve_apple_links(repo, FakeLookup(), now=NOW)

    later = NOW + timedelta(days=NEGATIVE_RETRY_DAYS + 1)
    found = FakeLookup(by_term={"Song Artist": [_song("333", "Song", "Artist")]})
    report = resolve_apple_links(repo, found, now=later)

    assert report.resolved == 1
    assert _catalog(repo) == {track.key: "333"}


def test_limit_caps_lookups(repo: SqliteTrackRepository) -> None:
    repo.upsert_presence("spotify", [Track(name=n, artist="Artist") for n in "ABC"])
    lookup = FakeLookup()

    report = resolve_apple_links(repo, lookup, limit=2, now=NOW)

    assert report.unresolved == 2
    assert len(lookup.search_calls) == 2


def test_title_only_track_is_not_searched(repo: SqliteTrackRepository) -> None:
    repo.upsert_presence("apple", [Track(name="Song", artist="")])
    lookup = FakeLookup()

    report = resolve_apple_links(repo, lookup, now=NOW)

    assert report.unresolved == 1
    assert lookup.search_calls == []


def test_platform_error_stops_batch_without_negative_row(
    repo: SqliteTrackRepository,
) -> None:
    ok = Track(name="Alpha", artist="Artist", duration_sec=200)
    broken = Track(name="Broken", artist="Artist")
    repo.upsert_presence("spotify", [ok, broken])
    lookup = FakeLookup(
        by_term={"Alpha Artist": [_song("1", "Alpha", "Artist")]}, fail_on="Broken"
    )

    report = resolve_apple_links(repo, lookup, now=NOW)

    assert report.error is not None
    assert report.resolved == 1
    assert _catalog(repo) == {ok.key: "1"}


# --- CLI wiring ------------------------------------------------------------


def _seeded_db(tmp_path: Path) -> Path:
    db = tmp_path / "cli.db"
    r = SqliteTrackRepository(db)
    r.upsert_presence("spotify", [Track(name="Song", artist="Artist", duration_sec=200)])
    r.close()
    return db


def test_cli_resolve_flag_uses_lookup_and_exits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db = _seeded_db(tmp_path)
    lookup = FakeLookup(by_term={"Song Artist": [_song("444", "Song", "Artist")]})
    monkeypatch.setattr(sync_music, "build_apple_catalog_client", lambda _base: (lookup, None))
    monkeypatch.setattr(
        sys, "argv", ["sync_music.py", "--resolve-apple-links", "--limit", "5", "--db", str(db)]
    )

    sync_music.main()

    assert "1 resueltas" in capsys.readouterr().out
    check = SqliteTrackRepository(db)
    assert list(_catalog(check).values()) == ["444"]
    check.close()


def test_cli_missing_creds_exits_with_one_line_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _seeded_db(tmp_path)
    monkeypatch.setattr(
        sync_music,
        "build_apple_catalog_client",
        lambda _base: (None, "faltan APPLE_TEAM_ID en .env"),
    )
    monkeypatch.setattr(sys, "argv", ["sync_music.py", "--resolve-apple-links", "--db", str(db)])

    with pytest.raises(SystemExit) as exc:
        sync_music.main()

    message = str(exc.value.code)
    assert "APPLE_TEAM_ID" in message
    assert "\n" not in message


def test_post_sync_step_reports_disabled_without_crashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = SqliteTrackRepository(_seeded_db(tmp_path))
    monkeypatch.setattr(
        sync_music, "build_apple_catalog_client", lambda _base: (None, "faltan claves")
    )

    sync_music.run_apple_link_resolution(repo, limit=None, required=False)

    assert "desactivados" in capsys.readouterr().out
    repo.close()


def test_offline_skips_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _seeded_db(tmp_path)

    def _forbidden(_base: Path) -> None:
        raise AssertionError("--offline must not build the Apple catalog client")

    monkeypatch.setattr(sync_music, "build_apple_catalog_client", _forbidden)
    monkeypatch.setattr(sync_music, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(sys, "argv", ["sync_music.py", "--offline", "--dry-run", "--db", str(db)])

    sync_music.main()


def test_offline_with_resolve_flag_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["sync_music.py", "--offline", "--resolve-apple-links"])
    with pytest.raises(SystemExit) as exc:
        sync_music.parse_args()
    assert exc.value.code == 2


class _RecordingResolver:
    def __init__(self, error: Exception | None = None) -> None:
        self.limits: list[int | None] = []
        self.error = error

    def __call__(self, repo: object, *, limit: int | None, required: bool) -> None:
        self.limits.append(limit)
        if self.error is not None:
            raise self.error


def _no_providers(*_args: object, **_kwargs: object) -> list[object]:
    return []


def _run_sync_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, resolver: object, *flags: str
) -> None:
    db = _seeded_db(tmp_path)
    monkeypatch.setattr(sync_music, "build_providers", _no_providers)
    monkeypatch.setattr(sync_music, "_resolve_apple_links", resolver)
    monkeypatch.setattr(sync_music, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(sys, "argv", ["sync_music.py", "--db", str(db), *flags])
    sync_music.main()


def test_dry_run_does_not_invoke_resolver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resolver = _RecordingResolver()
    _run_sync_main(tmp_path, monkeypatch, resolver, "--dry-run")
    assert resolver.limits == []


def test_post_sync_applies_default_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resolver = _RecordingResolver()
    _run_sync_main(tmp_path, monkeypatch, resolver)
    assert resolver.limits == [sync_music.POST_SYNC_APPLE_LINK_LIMIT]


def test_post_sync_explicit_limit_overrides_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resolver = _RecordingResolver()
    _run_sync_main(tmp_path, monkeypatch, resolver, "--limit", "7")
    assert resolver.limits == [7]


@pytest.mark.parametrize(
    "error",
    [
        PlatformOperationError("GET /catalog/sv/songs → respuesta no JSON"),
        sqlite3.OperationalError("database is locked"),
        DatabaseError("ERROR: no se pudo abrir\nsegunda línea"),
    ],
)
def test_post_sync_resolver_failure_keeps_sync_green(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: Exception,
) -> None:
    _run_sync_main(tmp_path, monkeypatch, _RecordingResolver(error))

    out = capsys.readouterr().out
    assert "links omitidos" in out
    assert "segunda línea" not in out
    assert "Listo." in out


def test_explicit_resolve_failure_exits_non_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _seeded_db(tmp_path)
    resolver = _RecordingResolver(sqlite3.OperationalError("database is locked"))
    monkeypatch.setattr(sync_music, "_resolve_apple_links", resolver)
    monkeypatch.setattr(sys, "argv", ["sync_music.py", "--resolve-apple-links", "--db", str(db)])

    with pytest.raises(SystemExit) as exc:
        sync_music.main()

    assert "database is locked" in str(exc.value.code)
    assert resolver.limits == [None]


@pytest.mark.parametrize(
    "flag",
    [
        ["--apply-spotify"],
        ["--apply-apple"],
        ["--apply-tidal"],
        ["--dry-run"],
        ["--full"],
        ["--tidal-reorder"],
        ["--export", "out.csv"],
    ],
)
def test_resolve_flag_rejects_sync_flags(
    flag: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["sync_music.py", "--resolve-apple-links", *flag])

    with pytest.raises(SystemExit) as exc:
        sync_music.parse_args()

    assert exc.value.code == 2
    assert flag[0] in capsys.readouterr().err


def test_resolve_flag_accepts_db_and_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sys, "argv", ["sync_music.py", "--resolve-apple-links", "--limit", "3", "--db", "x.db"]
    )
    args = sync_music.parse_args()
    assert (args.resolve_apple_links, args.limit) == (True, 3)
