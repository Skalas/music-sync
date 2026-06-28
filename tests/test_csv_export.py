"""T5 — CSV export."""

from __future__ import annotations

from pathlib import Path

from musicsync.application.csv_export import export_csv
from musicsync.domain.track import Track
from musicsync.infrastructure.seed import seed_db
from musicsync.infrastructure.sqlite_repository import SqliteTrackRepository


def test_csv_export_utf8_and_columns(tmp_path: Path) -> None:
    db = tmp_path / "seed.db"
    seed_db(db)

    out = tmp_path / "export.csv"
    n = export_csv(SqliteTrackRepository(db), out)
    assert n > 0

    text = out.read_text(encoding="utf-8")
    assert "key,name,artist,spotify,apple,tidal" in text.splitlines()[0]
    assert "Bohemian Rhapsody" in text

    n2 = export_csv(SqliteTrackRepository(db), out)
    assert n2 == n


def test_csv_one_row_per_track(tmp_path: Path) -> None:
    db = tmp_path / "lib.db"
    repo = SqliteTrackRepository(db)
    repo.upsert_presence("spotify", [Track(name="A", artist="B")], liked=True)
    repo.upsert_presence("apple", [Track(name="A", artist="B")], liked=True)
    repo.close()

    out = tmp_path / "out.csv"
    export_csv(SqliteTrackRepository(db), out)
    lines = out.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
