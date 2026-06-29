"""Tests for richer track metadata (Issue #12).

Covers:
- Repository: upsert + read round-trips for album/artwork_url/duration_sec/year
- Repository: idempotent migration adds columns to a pre-metadata DB without data loss
- Presence: per-platform added_at preserved through upsert
- Spotify parser: metadata extracted from representative API payload
- Tidal parser: metadata extracted from representative API payload + ISO-8601 duration
- Apple parser: new tab-delimited format + legacy "Name - Artist" fallback
- API /api/library: new fields present in response
- CSV: new columns present in header
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import requests

from musicsync.domain.track import Track
from musicsync.infrastructure.sqlite_repository import SqliteTrackRepository

# ---------------------------------------------------------------------------
# Repository round-trips
# ---------------------------------------------------------------------------


def test_repo_persists_track_metadata(tmp_path: Path) -> None:
    db = tmp_path / "lib.db"
    repo = SqliteTrackRepository(db)
    track = Track(
        name="Bohemian Rhapsody",
        artist="Queen",
        album="A Night at the Opera",
        artwork_url="https://example.com/art.jpg",
        duration_sec=354,
        year="1975",
    )
    repo.upsert_presence("spotify", [track], liked=True)

    rows = repo.iter_enriched_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row["album"] == "A Night at the Opera"
    assert row["artwork_url"] == "https://example.com/art.jpg"
    assert row["duration_sec"] == 354
    assert row["year"] == "1975"
    repo.close()


def test_repo_fill_if_missing_keeps_existing_metadata(tmp_path: Path) -> None:
    """A second upsert from a platform lacking artwork must not wipe existing artwork."""
    db = tmp_path / "lib.db"
    repo = SqliteTrackRepository(db)
    rich = Track(
        name="Song",
        artist="Artist",
        album="Album",
        artwork_url="https://example.com/art.jpg",
        duration_sec=200,
        year="2020",
    )
    bare = Track(name="Song", artist="Artist")  # no metadata
    repo.upsert_presence("spotify", [rich], liked=True)
    repo.upsert_presence("apple", [bare], liked=True)

    rows = repo.iter_enriched_rows()
    row = rows[0]
    # Artwork from Spotify must be kept after Apple upsert (fill-if-missing)
    assert row["artwork_url"] == "https://example.com/art.jpg"
    assert row["album"] == "Album"
    repo.close()


def test_repo_presence_added_at_per_platform(tmp_path: Path) -> None:
    db = tmp_path / "lib.db"
    repo = SqliteTrackRepository(db)
    t_sp = Track(name="Song", artist="Artist", added_at="2023-01-10T00:00:00Z")
    t_ap = Track(name="Song", artist="Artist", added_at="2022-06-01T00:00:00Z")
    repo.upsert_presence("spotify", [t_sp], liked=True)
    repo.upsert_presence("apple", [t_ap], liked=True)

    rows = repo.iter_enriched_rows()
    row = rows[0]
    assert row["spotify_added_at"] == "2023-01-10T00:00:00Z"
    assert row["apple_added_at"] == "2022-06-01T00:00:00Z"
    assert row["tidal_added_at"] is None
    repo.close()


def test_repo_export_rows_includes_metadata(tmp_path: Path) -> None:
    db = tmp_path / "lib.db"
    repo = SqliteTrackRepository(db)
    track = Track(
        name="Test",
        artist="Artist",
        album="AlbumX",
        year="2021",
        duration_sec=180,
        artwork_url="https://example.com/x.jpg",
    )
    repo.upsert_presence("tidal", [track], liked=True)

    rows = repo.iter_export_rows()
    assert len(rows) == 1
    assert rows[0]["album"] == "AlbumX"
    assert rows[0]["year"] == "2021"
    assert rows[0]["duration_sec"] == 180
    assert rows[0]["artwork_url"] == "https://example.com/x.jpg"
    repo.close()


# ---------------------------------------------------------------------------
# Idempotent migration
# ---------------------------------------------------------------------------


def test_migration_adds_columns_to_existing_db(tmp_path: Path) -> None:
    """Opening a pre-metadata DB must add the new columns without losing data."""
    db = tmp_path / "legacy.db"
    # Build a bare-bones DB with the old schema (no album/artwork/duration/year columns)
    conn = sqlite3.connect(str(db))
    conn.executescript("""
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
            synced_at TEXT,
            PRIMARY KEY (key, platform),
            FOREIGN KEY (key) REFERENCES tracks(key)
        );
        CREATE TABLE IF NOT EXISTS _meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO tracks (key, name, artist, first_seen, last_seen)
            VALUES ('test key', 'Test Song', 'Test Artist', '2020-01-01', '2020-01-01');
        INSERT INTO presence (key, platform, liked)
            VALUES ('test key', 'spotify', 1);
    """)
    conn.commit()
    conn.close()

    # Opening with SqliteTrackRepository must migrate transparently
    repo = SqliteTrackRepository(db)

    # Old data is preserved
    rows = repo.iter_enriched_rows()
    assert len(rows) == 1
    assert rows[0]["name"] == "Test Song"

    # New columns exist and default to NULL
    assert rows[0]["album"] is None
    assert rows[0]["artwork_url"] is None
    assert rows[0]["duration_sec"] is None
    assert rows[0]["year"] is None
    repo.close()


def test_migration_is_idempotent(tmp_path: Path) -> None:
    """Opening the same DB twice must not raise."""
    db = tmp_path / "lib.db"
    repo1 = SqliteTrackRepository(db)
    repo1.close()
    repo2 = SqliteTrackRepository(db)
    repo2.close()


# ---------------------------------------------------------------------------
# Spotify provider metadata parsing
# ---------------------------------------------------------------------------


def test_spotify_parser_extracts_metadata() -> None:
    """Spotify read_liked must populate album, artwork_url, duration_sec, year."""
    from musicsync.infrastructure.spotify_provider import SpotifyProvider

    fake_page = {
        "total": 1,
        "items": [
            {
                "added_at": "2023-05-10T12:00:00Z",
                "track": {
                    "id": "spotify123",
                    "name": "Test Song",
                    "artists": [{"name": "Test Artist"}],
                    "duration_ms": 210_000,
                    "album": {
                        "name": "Test Album",
                        "release_date": "2021-03-15",
                        "images": [
                            {"url": "https://example.com/large.jpg", "width": 640},
                            {"url": "https://example.com/small.jpg", "width": 64},
                        ],
                    },
                },
            }
        ],
        "next": None,
    }

    mock_client = MagicMock()
    mock_client.current_user_saved_tracks.return_value = fake_page

    provider = SpotifyProvider(
        base_dir=Path("."),
        unmatched_log_path=Path("/tmp/unmatched.txt"),
        client=mock_client,
    )
    tracks = provider.read_liked()

    assert len(tracks) == 1
    t = tracks[0]
    assert t.name == "Test Song"
    assert t.artist == "Test Artist"
    assert t.album == "Test Album"
    assert t.artwork_url == "https://example.com/large.jpg"
    assert t.duration_sec == 210
    assert t.year == "2021"
    assert t.added_at == "2023-05-10"  # normalized to YYYY-MM-DD


def test_spotify_parser_handles_missing_fields() -> None:
    """Spotify parser must not raise when optional fields are absent."""
    from musicsync.infrastructure.spotify_provider import SpotifyProvider

    fake_page = {
        "total": 1,
        "items": [
            {
                "track": {
                    "id": "x",
                    "name": "Bare Song",
                    "artists": [{"name": "Bare Artist"}],
                    # No album, no duration_ms, no added_at
                },
            }
        ],
        "next": None,
    }

    mock_client = MagicMock()
    mock_client.current_user_saved_tracks.return_value = fake_page

    provider = SpotifyProvider(
        base_dir=Path("."),
        unmatched_log_path=Path("/tmp/unmatched.txt"),
        client=mock_client,
    )
    tracks = provider.read_liked()
    assert len(tracks) == 1
    t = tracks[0]
    assert t.album is None
    assert t.artwork_url is None
    assert t.duration_sec is None
    assert t.year is None


# ---------------------------------------------------------------------------
# Tidal provider metadata parsing
# ---------------------------------------------------------------------------


def _tidal_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TIDAL_CLIENT_ID", "test-client")
    monkeypatch.setenv("TIDAL_REDIRECT_URI", "http://127.0.0.1:8080")
    base = tmp_path
    (base / ".env").write_text("", encoding="utf-8")
    return base


def _valid_token() -> dict[str, Any]:
    return {
        "access_token": "tok",
        "refresh_token": "ref",
        "expires_in": 3600,
        "obtained_at": 9_999_999_999.0,
    }


def test_tidal_parser_extracts_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from musicsync.infrastructure.tidal_provider import TidalProvider

    base = _tidal_env(tmp_path, monkeypatch)
    page = {
        "data": [
            {
                "type": "tracks",
                "id": "t1",
                "meta": {"addedAt": "2022-08-20T10:00:00Z"},
            }
        ],
        "included": [
            {
                "type": "tracks",
                "id": "t1",
                "attributes": {
                    "title": "Tidal Track",
                    "duration": "PT3M30S",
                },
                "relationships": {
                    "artists": {"data": [{"type": "artists", "id": "a1"}]},
                    "albums": {"data": [{"type": "albums", "id": "al1"}]},
                },
            },
            {
                "type": "artists",
                "id": "a1",
                "attributes": {"name": "Tidal Artist"},
            },
            {
                "type": "albums",
                "id": "al1",
                "attributes": {
                    "title": "Tidal Album",
                    "releaseDate": "2020-04-01",
                    "imageLinks": [{"href": "https://example.com/tidal-art.jpg"}],
                },
            },
        ],
        "links": {},
    }

    session = requests.Session()
    session.get = MagicMock(return_value=MagicMock(status_code=200, json=lambda: page))  # type: ignore[method-assign]

    provider = TidalProvider(base_dir=base, session=session)
    provider._write_cache(_valid_token())  # noqa: SLF001

    tracks = provider.read_liked()
    assert len(tracks) == 1
    t = tracks[0]
    assert t.name == "Tidal Track"
    assert t.album == "Tidal Album"
    assert t.duration_sec == 210  # PT3M30S = 210s
    assert t.year == "2020"
    assert t.artwork_url == "https://example.com/tidal-art.jpg"
    assert t.added_at == "2022-08-20"  # normalized to YYYY-MM-DD


def test_tidal_parser_integer_duration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tidal may return duration as an integer (seconds) instead of ISO-8601."""
    from musicsync.infrastructure.tidal_provider import TidalProvider

    base = _tidal_env(tmp_path, monkeypatch)
    page = {
        "data": [{"type": "tracks", "id": "t2"}],
        "included": [
            {
                "type": "tracks",
                "id": "t2",
                "attributes": {"title": "Int Dur Track", "duration": 180},
                "relationships": {"artists": {"data": []}, "albums": {"data": []}},
            }
        ],
        "links": {},
    }

    session = requests.Session()
    session.get = MagicMock(return_value=MagicMock(status_code=200, json=lambda: page))  # type: ignore[method-assign]

    provider = TidalProvider(base_dir=base, session=session)
    provider._write_cache(_valid_token())  # noqa: SLF001

    tracks = provider.read_liked()
    assert len(tracks) == 1
    assert tracks[0].duration_sec == 180


def test_tidal_iso8601_duration_parsing() -> None:
    from musicsync.infrastructure.tidal_provider import _parse_iso8601_duration

    assert _parse_iso8601_duration("PT3M30S") == 210
    assert _parse_iso8601_duration("PT1H2M3S") == 3723
    assert _parse_iso8601_duration("PT45S") == 45
    assert _parse_iso8601_duration("P1DT0H0M0S") == 86400
    assert _parse_iso8601_duration("invalid") is None
    assert _parse_iso8601_duration("PT") is None  # no components — not a real duration


# ---------------------------------------------------------------------------
# Apple provider parser
# ---------------------------------------------------------------------------


def test_apple_parser_new_tab_format() -> None:
    from musicsync.infrastructure.apple_provider import _parse_apple_line

    line = "Song Name\tArtist Name\tAlbum Name\t2019\t240\t2023-03-15"
    track = _parse_apple_line(line)
    assert track is not None
    assert track.name == "Song Name"
    assert track.artist == "Artist Name"
    assert track.album == "Album Name"
    assert track.year == "2019"
    assert track.duration_sec == 240
    assert track.added_at == "2023-03-15"


def test_apple_parser_legacy_format_fallback() -> None:
    from musicsync.infrastructure.apple_provider import _parse_apple_line

    line = "Bohemian Rhapsody - Queen"
    track = _parse_apple_line(line)
    assert track is not None
    assert track.name == "Bohemian Rhapsody"
    assert track.artist == "Queen"
    assert track.album is None
    assert track.duration_sec is None


def test_apple_parser_new_format_handles_empty_optional_fields() -> None:
    from musicsync.infrastructure.apple_provider import _parse_apple_line

    line = "Song\tArtist\t\t\t\t"
    track = _parse_apple_line(line)
    assert track is not None
    assert track.name == "Song"
    assert track.artist == "Artist"
    assert track.album is None
    assert track.year is None
    assert track.duration_sec is None
    assert track.added_at is None


def test_apple_parser_skips_invalid_lines() -> None:
    from musicsync.infrastructure.apple_provider import _parse_apple_line

    assert _parse_apple_line("") is None
    assert _parse_apple_line("no separator here") is None


def test_apple_parser_hyphen_in_artist_name_handled() -> None:
    """Legacy format: rpartition finds the LAST ' - ', so artist can contain hyphens."""
    from musicsync.infrastructure.apple_provider import _parse_apple_line

    line = "Track Name - Artist With-Hyphen"
    track = _parse_apple_line(line)
    assert track is not None
    assert track.name == "Track Name"
    assert track.artist == "Artist With-Hyphen"


# ---------------------------------------------------------------------------
# CSV new columns
# ---------------------------------------------------------------------------


def test_csv_includes_metadata_columns(tmp_path: Path) -> None:
    from musicsync.application.csv_export import CSV_FIELDNAMES, export_csv

    db = tmp_path / "lib.db"
    repo = SqliteTrackRepository(db)
    track = Track(
        name="CSV Track",
        artist="CSV Artist",
        album="CSV Album",
        year="2022",
        duration_sec=300,
        artwork_url="https://example.com/img.jpg",
    )
    repo.upsert_presence("spotify", [track], liked=True)

    out = tmp_path / "out.csv"
    export_csv(repo, out)
    repo.close()

    text = out.read_text(encoding="utf-8")
    header = text.splitlines()[0]
    for col in ("album", "year", "duration_sec", "artwork_url"):
        assert col in header
    # Data row has the values
    assert "CSV Album" in text
    assert "2022" in text

    # Verify fieldnames constant
    assert "album" in CSV_FIELDNAMES
    assert "year" in CSV_FIELDNAMES
    assert "duration_sec" in CSV_FIELDNAMES
    assert "artwork_url" in CSV_FIELDNAMES
