"""CSV export of the SQLite library (pivot view)."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import IO, Any

from musicsync.domain.platforms import PLATFORMS
from musicsync.domain.ports import TrackRepository

PLATFORM_COLUMNS = PLATFORMS
CSV_FIELDNAMES = [
    "key", "name", "artist",
    *PLATFORM_COLUMNS,
    "album", "year", "duration_sec", "artwork_url",
]


def write_csv(rows: list[dict[str, Any]], out: IO[str]) -> None:
    """Write CSV header + data rows to any text file-like object.

    Single source of write logic — used by both the CLI export and the web endpoint.
    """
    writer = csv.DictWriter(out, fieldnames=CSV_FIELDNAMES)
    writer.writeheader()
    for row in rows:
        writer.writerow({col: row.get(col, "") for col in CSV_FIELDNAMES})


def export_csv(repo: TrackRepository, path: Path) -> int:
    """Write UTF-8 CSV with one row per track. Returns row count."""
    rows = repo.iter_export_rows()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        write_csv(rows, fh)
    return len(rows)
