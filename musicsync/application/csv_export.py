"""CSV export of the SQLite library (pivot view)."""

from __future__ import annotations

import csv
from pathlib import Path

from musicsync.domain.platforms import PLATFORMS
from musicsync.domain.ports import TrackRepository

PLATFORM_COLUMNS = PLATFORMS


def export_csv(repo: TrackRepository, path: Path) -> int:
    """Write UTF-8 CSV with one row per track. Returns row count."""
    rows = repo.iter_export_rows()
    fieldnames = ["key", "name", "artist", *PLATFORM_COLUMNS]

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({col: row.get(col, "") for col in fieldnames})

    return len(rows)
