"""T1 — ISRC capture from Spotify and Tidal, persisted on tracks.isrc."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from musicsync.domain.track import Track
from musicsync.domain.union import canonicalize_presence, compute_tidal_catalog
from musicsync.infrastructure.spotify_provider import SpotifyProvider
from musicsync.infrastructure.sqlite_repository import SqliteTrackRepository
from musicsync.infrastructure.tidal_provider import _parse_collection_item


def _spotify_item(external_ids: dict[str, str] | None) -> dict[str, Any]:
    track: dict[str, Any] = {
        "id": "sp1",
        "name": "Song",
        "artists": [{"name": "Artist"}],
        "album": {"name": "Album"},
    }
    if external_ids is not None:
        track["external_ids"] = external_ids
    return {"added_at": "2024-01-01T00:00:00Z", "track": track}


def _read_spotify(items: list[dict[str, Any]]) -> list[Track]:
    client = MagicMock()
    client.current_user_saved_tracks.return_value = {
        "total": len(items),
        "items": items,
        "next": None,
    }
    provider = SpotifyProvider(
        base_dir=Path("."), unmatched_log_path=Path("/tmp/unmatched.txt"), client=client
    )
    return provider.read_liked()


def test_spotify_reads_isrc_from_external_ids() -> None:
    tracks = _read_spotify([_spotify_item({"isrc": "USUM71703861"})])
    assert tracks[0].isrc == "USUM71703861"


def test_spotify_missing_isrc_is_none() -> None:
    assert _read_spotify([_spotify_item(None)])[0].isrc is None
    assert _read_spotify([_spotify_item({"isrc": ""})])[0].isrc is None


def _tidal_resource(attributes: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    item = {"type": "tracks", "id": "t1"}
    included = {
        "tracks:t1": {"type": "tracks", "id": "t1", "attributes": attributes},
    }
    return item, included


def test_tidal_reads_isrc_from_attributes() -> None:
    item, included = _tidal_resource({"title": "Song", "isrc": "GBAYE0601498"})
    track = _parse_collection_item(item, included)
    assert track is not None
    assert track.isrc == "GBAYE0601498"


def test_tidal_missing_isrc_is_none() -> None:
    item, included = _tidal_resource({"title": "Song"})
    track = _parse_collection_item(item, included)
    assert track is not None
    assert track.isrc is None


def _stored_isrc(repo: SqliteTrackRepository, key: str) -> str | None:
    row = repo._conn.execute("SELECT isrc FROM tracks WHERE key = ?", (key,)).fetchone()  # noqa: SLF001
    return None if row is None else row["isrc"]


def test_repo_persists_isrc_and_keeps_it_on_null_upsert(tmp_path: Path) -> None:
    repo = SqliteTrackRepository(tmp_path / "lib.db")
    with_isrc = Track(name="Song", artist="Artist", isrc="USUM71703861")
    repo.upsert_presence("spotify", [with_isrc])
    repo.upsert_presence("apple", [Track(name="Song", artist="Artist")])
    repo.upsert_presence("spotify", [with_isrc])  # idempotent

    assert _stored_isrc(repo, with_isrc.key) == "USUM71703861"
    repo.close()


def test_migration_adds_isrc_column_to_existing_db(tmp_path: Path) -> None:
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE tracks (
            key TEXT PRIMARY KEY, name TEXT NOT NULL, artist TEXT NOT NULL,
            first_seen TEXT NOT NULL, last_seen TEXT NOT NULL
        );
        INSERT INTO tracks VALUES ('k', 'Song', 'Artist', '2020-01-01', '2020-01-01');
        """
    )
    conn.commit()
    conn.close()

    repo = SqliteTrackRepository(db)
    assert _stored_isrc(repo, "k") is None
    repo.close()


def test_union_copies_keep_isrc() -> None:
    title_only = Track(name="Song", artist="")
    full = Track(name="Song", artist="Artist", isrc="USUM71703861")
    canonical, _remap = canonicalize_presence(
        {"apple": {title_only.key: title_only}, "spotify": {full.key: full}}
    )
    assert canonical["spotify"][full.key].isrc == "USUM71703861"

    catalog = compute_tidal_catalog({"spotify": {full.key: full}})
    assert catalog[0].isrc == "USUM71703861"
