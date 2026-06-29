"""SQLite track repository — source of truth for presence and sync state."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from pathlib import Path

from musicsync.domain._time import utc_now_iso
from musicsync.domain.platforms import PLATFORMS
from musicsync.domain.track import Track

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tracks (
    key TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    artist TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    album TEXT,
    artwork_url TEXT,
    duration_sec INTEGER,
    year TEXT
);
CREATE TABLE IF NOT EXISTS presence (
    key TEXT NOT NULL,
    platform TEXT NOT NULL,
    liked INTEGER NOT NULL DEFAULT 0,
    platform_id TEXT,
    added_at TEXT,
    synced_at TEXT,
    PRIMARY KEY (key, platform),
    FOREIGN KEY (key) REFERENCES tracks(key)
);
CREATE TABLE IF NOT EXISTS _meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# Allowlist for migration SQL fragments (W1).
_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
_TYPE_ALLOWLIST = {"TEXT", "INTEGER", "REAL", "BLOB"}

# Columns to add when upgrading an existing database that pre-dates a schema change.
_TRACKS_MIGRATIONS = [
    ("album", "TEXT"),
    ("artwork_url", "TEXT"),
    ("duration_sec", "INTEGER"),
    ("year", "TEXT"),
]
_PRESENCE_MIGRATIONS = [
    ("added_at", "TEXT"),
]

# Track-level metadata columns shared across read queries (D1).
_METADATA_COLUMNS = ("album", "artwork_url", "duration_sec", "year")


def _metadata_from_row(row: sqlite3.Row) -> dict[str, str | int | None]:
    """Extract the four track-level metadata fields from a query row."""
    return {col: row[col] for col in _METADATA_COLUMNS}

class DatabaseError(Exception):
    """Raised when the SQLite database cannot be opened or used."""


class SqliteTrackRepository:
    def __init__(self, db_path: Path, *, require_exists: bool = False) -> None:
        self._path = db_path
        # A single sqlite3.Connection is shared across threads (FastAPI
        # threadpool + SSE executor + connect background thread). Serialize all
        # connection access through a reentrant lock to avoid corruption.
        self._lock = threading.RLock()
        if require_exists and not db_path.exists():
            raise DatabaseError(
                f"ERROR: base de datos no encontrada: {db_path}\n"
                "Usa --db con una ruta existente o ejecuta el seed."
            )
        try:
            db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(db_path), timeout=5.0, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.executescript(SCHEMA_SQL)
            self._run_migrations()
            self._conn.commit()
        except sqlite3.OperationalError as exc:
            raise DatabaseError(
                f"ERROR: no se pudo abrir la base de datos {db_path}: {exc}\n"
                "¿Está bloqueada por otro proceso?"
            ) from exc

    def _run_migrations(self) -> None:
        """Idempotently add new columns to existing databases."""
        self._migrate_table("tracks", _TRACKS_MIGRATIONS)
        self._migrate_table("presence", _PRESENCE_MIGRATIONS)

    def _migrate_table(
        self, table: str, migrations: list[tuple[str, str]]
    ) -> None:
        """Add missing columns to *table*, validating names/types against an allowlist."""
        assert _IDENT_RE.match(table), f"unsafe table name in migration: {table!r}"
        existing = {
            row[1]
            for row in self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for col, typedef in migrations:
            assert _IDENT_RE.match(col), f"unsafe column name in migration: {col!r}"
            assert typedef in _TYPE_ALLOWLIST, f"unsafe type in migration: {typedef!r}"
            if col not in existing:
                self._conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {col} {typedef}"
                )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def upsert_presence(
        self,
        platform: str,
        tracks: list[Track],
        *,
        liked: bool = True,
    ) -> None:
        with self._lock:
            now = utc_now_iso()
            for track in tracks:
                self._upsert_track(track, now)
                self._conn.execute(
                    """
                    INSERT INTO presence (key, platform, liked, platform_id, added_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(key, platform) DO UPDATE SET
                        liked = excluded.liked,
                        platform_id = COALESCE(excluded.platform_id, platform_id),
                        added_at = COALESCE(excluded.added_at, added_at)
                    """,
                    (
                        track.key,
                        platform,
                        1 if liked else 0,
                        track.platform_id,
                        track.added_at,
                    ),
                )
            self._conn.commit()

    def _upsert_track(self, track: Track, now: str) -> None:
        """Insert or update the track row. Caller must hold self._lock."""
        self._conn.execute(
            """
            INSERT INTO tracks (key, name, artist, first_seen, last_seen,
                                album, artwork_url, duration_sec, year)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                name = excluded.name,
                artist = excluded.artist,
                last_seen = excluded.last_seen,
                album = COALESCE(excluded.album, album),
                artwork_url = COALESCE(excluded.artwork_url, artwork_url),
                duration_sec = COALESCE(excluded.duration_sec, duration_sec),
                year = COALESCE(excluded.year, year)
            """,
            (
                track.key,
                track.name,
                track.artist,
                now,
                now,
                track.album,
                track.artwork_url,
                track.duration_sec,
                track.year,
            ),
        )

    def get_liked_by_platform(self) -> dict[str, dict[str, Track]]:
        result: dict[str, dict[str, Track]] = {p: {} for p in PLATFORMS}
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT p.key, p.platform, p.platform_id, p.added_at,
                       t.name, t.artist
                FROM presence p
                JOIN tracks t ON t.key = p.key
                WHERE p.liked = 1
                """
            ).fetchall()
        for row in rows:
            platform = row["platform"]
            if platform not in result:
                result[platform] = {}
            result[platform][row["key"]] = Track(
                name=row["name"],
                artist=row["artist"],
                platform_id=row["platform_id"],
                added_at=row["added_at"],
            )
        return result

    def get_synced_keys(self, platform: str) -> set[str]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT key FROM presence
                WHERE platform = ? AND synced_at IS NOT NULL
                """,
                (platform,),
            ).fetchall()
        return {row["key"] for row in rows}

    def mark_synced(self, platform: str, keys: list[str], *, when: str) -> None:
        with self._lock:
            for key in keys:
                cur = self._conn.execute(
                    """
                    UPDATE presence SET synced_at = ?
                    WHERE key = ? AND platform = ?
                    """,
                    (when, key, platform),
                )
                if cur.rowcount == 0:
                    row = self._conn.execute(
                        "SELECT name, artist FROM tracks WHERE key = ?", (key,)
                    ).fetchone()
                    if row is None:
                        continue
                    self._conn.execute(
                        """
                        INSERT INTO presence (key, platform, liked, synced_at)
                        VALUES (?, ?, 0, ?)
                        ON CONFLICT(key, platform) DO UPDATE SET synced_at = excluded.synced_at
                        """,
                        (key, platform, when),
                    )
            self._conn.commit()

    def clear_synced(self) -> None:
        with self._lock:
            self._conn.execute("UPDATE presence SET synced_at = NULL")
            self._conn.commit()

    def migrate_state_json(self, path: str) -> bool:
        with self._lock:
            migrated = self._conn.execute(
                "SELECT value FROM _meta WHERE key = 'state_json_migrated'"
            ).fetchone()
            if migrated is not None:
                return False

            state_path = Path(path)
            if not state_path.exists():
                self._conn.execute(
                    "INSERT OR REPLACE INTO _meta (key, value) "
                    "VALUES ('state_json_migrated', 'absent')"
                )
                self._conn.commit()
                return False

            data = json.loads(state_path.read_text(encoding="utf-8"))
            now = utc_now_iso()
            for platform in ("apple", "spotify"):
                for key in data.get(platform, []):
                    row = self._conn.execute(
                        "SELECT name, artist FROM tracks WHERE key = ?", (key,)
                    ).fetchone()
                    if row is None:
                        self._conn.execute(
                            """
                            INSERT OR IGNORE INTO tracks (key, name, artist, first_seen, last_seen)
                            VALUES (?, ?, ?, ?, ?)
                            """,
                            (key, key.split("␟")[0] if "␟" in key else key, "", now, now),
                        )
                    self._conn.execute(
                        """
                        INSERT INTO presence (key, platform, liked, synced_at)
                        VALUES (?, ?, 0, ?)
                        ON CONFLICT(key, platform) DO UPDATE SET synced_at = excluded.synced_at
                        """,
                        (key, platform, now),
                    )

            self._conn.execute(
                "INSERT OR REPLACE INTO _meta (key, value) VALUES ('state_json_migrated', 'done')"
            )
            self._conn.commit()
            return True

    def iter_export_rows(self) -> list[dict[str, str | int | None]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT t.key, t.name, t.artist,
                       t.album, t.artwork_url, t.duration_sec, t.year,
                       MAX(CASE WHEN p.platform = 'spotify' AND p.liked = 1
                           THEN 1 ELSE 0 END) AS spotify,
                       MAX(CASE WHEN p.platform = 'apple' AND p.liked = 1
                           THEN 1 ELSE 0 END) AS apple,
                       MAX(CASE WHEN p.platform = 'tidal' AND p.liked = 1
                           THEN 1 ELSE 0 END) AS tidal
                FROM tracks t
                LEFT JOIN presence p ON p.key = t.key
                GROUP BY t.key, t.name, t.artist
                ORDER BY t.name, t.artist
                """
            ).fetchall()
        return [
            {
                "key": row["key"],
                "name": row["name"],
                "artist": row["artist"],
                **_metadata_from_row(row),
                "spotify": row["spotify"],
                "apple": row["apple"],
                "tidal": row["tidal"],
            }
            for row in rows
        ]

    def iter_enriched_rows(
        self, *, sort_by: str | None = None
    ) -> list[dict[str, str | int | None]]:
        """Export rows plus per-platform platform_id, added_at, and track metadata.

        When sort_by='added_at', rows are ordered by most-recently-added first (SQL-side).
        """
        order_clause = (
            "ORDER BY added_at_representative DESC, t.name, t.artist"
            if sort_by == "added_at"
            else "ORDER BY t.name, t.artist"
        )
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT
                    t.key, t.name, t.artist,
                    t.album, t.artwork_url, t.duration_sec, t.year,
                    MAX(CASE WHEN p.platform = 'spotify' AND p.liked = 1
                        THEN 1 ELSE 0 END) AS spotify,
                    MAX(CASE WHEN p.platform = 'apple'   AND p.liked = 1
                        THEN 1 ELSE 0 END) AS apple,
                    MAX(CASE WHEN p.platform = 'tidal'   AND p.liked = 1
                        THEN 1 ELSE 0 END) AS tidal,
                    MAX(CASE WHEN p.platform = 'spotify' THEN p.platform_id END) AS spotify_id,
                    MAX(CASE WHEN p.platform = 'tidal'   THEN p.platform_id END) AS tidal_id,
                    MAX(CASE WHEN p.platform = 'spotify' THEN p.added_at END) AS spotify_added_at,
                    MAX(CASE WHEN p.platform = 'apple'   THEN p.added_at END) AS apple_added_at,
                    MAX(CASE WHEN p.platform = 'tidal'   THEN p.added_at END) AS tidal_added_at,
                    MAX(p.added_at) AS added_at_representative
                FROM tracks t
                LEFT JOIN presence p ON p.key = t.key
                GROUP BY t.key, t.name, t.artist
                {order_clause}
                """
            ).fetchall()
        return [
            {
                "key": row["key"],
                "name": row["name"],
                "artist": row["artist"],
                **_metadata_from_row(row),
                "spotify": row["spotify"],
                "apple": row["apple"],
                "tidal": row["tidal"],
                "spotify_id": row["spotify_id"],
                "tidal_id": row["tidal_id"],
                "spotify_added_at": row["spotify_added_at"],
                "apple_added_at": row["apple_added_at"],
                "tidal_added_at": row["tidal_added_at"],
                "added_at_representative": row["added_at_representative"],
            }
            for row in rows
        ]
