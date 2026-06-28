"""SQLite track repository — source of truth for presence and sync state."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from musicsync.domain.track import Track

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tracks (
    key TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    artist TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL
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

PLATFORMS = ("spotify", "apple", "tidal")


class DatabaseError(Exception):
    """Raised when the SQLite database cannot be opened or used."""


class SqliteTrackRepository:
    def __init__(self, db_path: Path, *, require_exists: bool = False) -> None:
        self._path = db_path
        if require_exists and not db_path.exists():
            raise DatabaseError(
                f"ERROR: base de datos no encontrada: {db_path}\n"
                "Usa --db con una ruta existente o ejecuta el seed."
            )
        try:
            db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(db_path), timeout=5.0)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.executescript(SCHEMA_SQL)
            self._conn.commit()
        except sqlite3.OperationalError as exc:
            raise DatabaseError(
                f"ERROR: no se pudo abrir la base de datos {db_path}: {exc}\n"
                "¿Está bloqueada por otro proceso?"
            ) from exc

    def close(self) -> None:
        self._conn.close()

    def upsert_presence(
        self,
        platform: str,
        tracks: list[Track],
        *,
        liked: bool = True,
    ) -> None:
        now = _utc_now()
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
        self._conn.execute(
            """
            INSERT INTO tracks (key, name, artist, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                name = excluded.name,
                artist = excluded.artist,
                last_seen = excluded.last_seen
            """,
            (track.key, track.name, track.artist, now, now),
        )

    def get_liked_by_platform(self) -> dict[str, dict[str, Track]]:
        result: dict[str, dict[str, Track]] = {p: {} for p in PLATFORMS}
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
        rows = self._conn.execute(
            """
            SELECT key FROM presence
            WHERE platform = ? AND synced_at IS NOT NULL
            """,
            (platform,),
        ).fetchall()
        return {row["key"] for row in rows}

    def mark_synced(self, platform: str, keys: list[str], *, when: str) -> None:
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
        self._conn.execute("UPDATE presence SET synced_at = NULL")
        self._conn.commit()

    def migrate_state_json(self, path: str) -> bool:
        migrated = self._conn.execute(
            "SELECT value FROM _meta WHERE key = 'state_json_migrated'"
        ).fetchone()
        if migrated is not None:
            return False

        state_path = Path(path)
        if not state_path.exists():
            self._conn.execute(
                "INSERT OR REPLACE INTO _meta (key, value) VALUES ('state_json_migrated', 'absent')"
            )
            self._conn.commit()
            return False

        data = json.loads(state_path.read_text(encoding="utf-8"))
        now = _utc_now()
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

    def iter_export_rows(self) -> list[dict[str, str | int]]:
        rows = self._conn.execute(
            """
            SELECT t.key, t.name, t.artist,
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
                "spotify": row["spotify"],
                "apple": row["apple"],
                "tidal": row["tidal"],
            }
            for row in rows
        ]


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
