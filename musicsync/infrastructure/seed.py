"""Idempotent seed data for offline smoke tests."""

from __future__ import annotations

import argparse
from pathlib import Path

from musicsync.domain.track import Track
from musicsync.infrastructure.sqlite_repository import SqliteTrackRepository

SEED_DATA: dict[str, list[tuple[str, str]]] = {
    "spotify": [
        ("Bohemian Rhapsody", "Queen"),
        ("Imagine", "John Lennon"),
        ("Stairway to Heaven", "Led Zeppelin"),
    ],
    "apple": [
        ("Bohemian Rhapsody", "Queen"),
        ("Hotel California", "Eagles"),
    ],
    "tidal": [
        ("Imagine", "John Lennon"),
        ("Billie Jean", "Michael Jackson"),
    ],
}


def seed_db(db_path: Path) -> int:
    """Insert seed tracks idempotently. Returns total presence rows written."""
    repo = SqliteTrackRepository(db_path)
    count = 0
    try:
        for platform, entries in SEED_DATA.items():
            tracks = [Track(name=n, artist=a) for n, a in entries]
            repo.upsert_presence(platform, tracks, liked=True)
            count += len(tracks)
    finally:
        repo.close()
    return count


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed a throwaway SQLite library.")
    parser.add_argument("--db", required=True, type=Path, help="SQLite database path")
    args = parser.parse_args()
    n = seed_db(args.db)
    print(f"Seed OK: {n} presence rows en {args.db}")


if __name__ == "__main__":
    main()
