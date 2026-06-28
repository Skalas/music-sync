"""T3 — SQLite repository; T4 — state.json migration."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from musicsync.domain.track import Track
from musicsync.infrastructure.sqlite_repository import DatabaseError, SqliteTrackRepository


def test_idempotent_upsert(tmp_path: Path) -> None:
    db = tmp_path / "lib.db"
    repo = SqliteTrackRepository(db)
    track = Track(name="Test Song", artist="Test Artist", platform_id="abc")
    repo.upsert_presence("spotify", [track], liked=True)
    repo.upsert_presence("spotify", [track], liked=True)

    rows = repo._conn.execute("SELECT COUNT(*) FROM tracks").fetchone()
    assert rows[0] == 1
    presence = repo._conn.execute("SELECT COUNT(*) FROM presence").fetchone()
    assert presence[0] == 1
    repo.close()


def test_synced_at_only_after_mark(tmp_path: Path) -> None:
    db = tmp_path / "lib.db"
    repo = SqliteTrackRepository(db)
    track = Track(name="A", artist="B")
    repo.upsert_presence("spotify", [track], liked=True)

    assert repo.get_synced_keys("spotify") == set()
    repo.mark_synced("spotify", [track.key], when="2026-01-01T00:00:00+00:00")
    assert track.key in repo.get_synced_keys("spotify")
    repo.close()


def test_missing_db_fails_loudly(tmp_path: Path) -> None:
    missing = tmp_path / "nope.db"
    with pytest.raises(DatabaseError, match="no encontrada"):
        SqliteTrackRepository(missing, require_exists=True)


def test_locked_db_fails_loudly(tmp_path: Path) -> None:
    db = tmp_path / "locked.db"
    repo = SqliteTrackRepository(db)
    repo.close()

    lock = sqlite3.connect(str(db), timeout=0.1)
    lock.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(DatabaseError, match="no se pudo abrir"):
            SqliteTrackRepository(db)
    finally:
        lock.close()


def test_state_json_migration(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps({"apple": ["song␟artist"], "spotify": ["other␟band"]}),
        encoding="utf-8",
    )
    db = tmp_path / "lib.db"
    repo = SqliteTrackRepository(db)

    migrated = repo.migrate_state_json(str(state_path))
    assert migrated is True
    assert "song␟artist" in repo.get_synced_keys("apple")
    assert "other␟band" in repo.get_synced_keys("spotify")

    assert repo.migrate_state_json(str(state_path)) is False
    repo.close()


def test_absent_state_json_clean_first_run(tmp_path: Path) -> None:
    db = tmp_path / "lib.db"
    repo = SqliteTrackRepository(db)
    assert repo.migrate_state_json(str(tmp_path / "missing.json")) is False
    assert repo.get_synced_keys("apple") == set()
    repo.close()
