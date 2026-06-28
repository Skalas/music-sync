"""T9 — Offline dry-run + seed smoke."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from musicsync.application.csv_export import export_csv
from musicsync.application.sync_service import SyncOptions, SyncService
from musicsync.infrastructure.seed import seed_db
from musicsync.infrastructure.sqlite_repository import SqliteTrackRepository


def test_offline_dry_run_zero_network(tmp_path: Path) -> None:
    db = tmp_path / "smoke.db"
    seed_db(db)
    repo = SqliteTrackRepository(db, require_exists=True)

    with patch("requests.Session.get") as mock_get, patch("requests.Session.post") as mock_post:
        service = SyncService(repo, [])
        result = service.run(SyncOptions(offline=True, dry_run=True))

    mock_get.assert_not_called()
    mock_post.assert_not_called()

    total_pending = sum(len(v) for v in result.to_sync.values())
    assert total_pending > 0
    repo.close()


def test_seed_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "seed.db"
    n1 = seed_db(db)
    n2 = seed_db(db)
    assert n1 == n2

    repo = SqliteTrackRepository(db)
    rows = repo._conn.execute("SELECT COUNT(*) FROM tracks").fetchone()
    repo.close()
    assert rows[0] > 0


def test_offline_export_after_seed(tmp_path: Path) -> None:
    db = tmp_path / "smoke.db"
    seed_db(db)
    out = tmp_path / "export.csv"
    n = export_csv(SqliteTrackRepository(db, require_exists=True), out)
    assert n > 0
    assert out.stat().st_size > 0
