"""P2 — SQLite playlists + playlist_tracks tables."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from musicsync.domain.playlist import Playlist
from musicsync.domain.ports import PlaylistRepository
from musicsync.domain.track import Track
from musicsync.infrastructure.sqlite_repository import SqliteTrackRepository

A = Track(name="Song A", artist="Artist A")
B = Track(name="Song B", artist="Artist B")
C = Track(name="Song C", artist="Artist C")

_PRE_PLAYLIST_SCHEMA = """
CREATE TABLE tracks (
    key TEXT PRIMARY KEY, name TEXT NOT NULL, artist TEXT NOT NULL,
    first_seen TEXT NOT NULL, last_seen TEXT NOT NULL
);
CREATE TABLE presence (
    key TEXT NOT NULL, platform TEXT NOT NULL, liked INTEGER NOT NULL DEFAULT 0,
    platform_id TEXT, synced_at TEXT, PRIMARY KEY (key, platform)
);
"""


def _count(repo: SqliteTrackRepository, table: str) -> int:
    return int(repo._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])  # noqa: SLF001


def _playlist(platform: str, name: str, *tracks: Track, remote_id: str = "r1") -> Playlist:
    return Playlist(platform=platform, name=name, remote_id=remote_id, tracks=tracks)


def test_repository_satisfies_playlist_port(tmp_path: Path) -> None:
    repo = SqliteTrackRepository(tmp_path / "lib.db")
    assert isinstance(repo, PlaylistRepository)
    repo.close()


def test_existing_db_upgrades_in_place_without_data_loss(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript(_PRE_PLAYLIST_SCHEMA)
    conn.execute(
        "INSERT INTO tracks VALUES (?, 'Song A', 'Artist A', 't0', 't0')", (A.key,)
    )
    conn.execute("INSERT INTO presence VALUES (?, 'spotify', 1, 'sp1', NULL)", (A.key,))
    conn.commit()
    conn.close()

    for _ in range(2):  # opening twice proves the migration is idempotent
        repo = SqliteTrackRepository(db)
        assert _count(repo, "playlists") == 0
        assert _count(repo, "playlist_tracks") == 0
        assert A.key in repo.get_liked_by_platform()["spotify"]
        repo.close()


def test_upsert_playlist_is_idempotent(tmp_path: Path) -> None:
    repo = SqliteTrackRepository(tmp_path / "lib.db")
    playlist = _playlist("spotify", "Mix", A, B)

    repo.upsert_playlist(playlist)
    repo.upsert_playlist(playlist)

    assert _count(repo, "playlists") == 1
    assert _count(repo, "playlist_tracks") == 2
    stored = repo.get_playlists({"mix"})["spotify"][0]
    assert stored.name == "Mix"
    assert stored.remote_id == "r1"
    assert [t.key for t in stored.tracks] == [A.key, B.key]
    repo.close()


def test_upsert_playlist_matches_name_case_insensitively(tmp_path: Path) -> None:
    repo = SqliteTrackRepository(tmp_path / "lib.db")

    repo.upsert_playlist(_playlist("apple", "Road Trip", A))
    repo.upsert_playlist(_playlist("apple", "  road trip ", A, B))

    assert _count(repo, "playlists") == 1
    assert len(repo.get_playlists({"road trip"})["apple"][0].tracks) == 2
    repo.close()


def test_reread_drops_removed_tracks_but_keeps_mirror_state(tmp_path: Path) -> None:
    repo = SqliteTrackRepository(tmp_path / "lib.db")
    repo.upsert_playlist(_playlist("apple", "Mix", A, B))
    repo.mark_playlist_synced("apple", "Mix", "r1", [C], when="2026-10-08T00:00:00Z")

    repo.upsert_playlist(_playlist("apple", "Mix", A))

    stored = repo.get_playlists({"mix"})["apple"][0]
    assert [t.key for t in stored.tracks] == [A.key]
    assert repo.get_playlist_synced_keys({"mix"}) == {"mix": {"apple": {C.key}}}
    repo.close()


def test_mark_playlist_synced_creates_target_row_with_remote_id(tmp_path: Path) -> None:
    repo = SqliteTrackRepository(tmp_path / "lib.db")

    repo.mark_playlist_synced("spotify", "Mix", "new-id", [A, B], when="t1")
    repo.mark_playlist_synced("spotify", "mix", None, [A], when="t2")

    assert repo.get_playlist_synced_keys({"mix"}) == {"mix": {"spotify": {A.key, B.key}}}
    assert repo.get_playlists({"mix"})["spotify"][0].remote_id == "new-id"
    assert repo.get_playlist_synced_keys({"other"}) == {}
    repo.close()


def test_get_playlists_filters_by_name_and_groups_by_platform(tmp_path: Path) -> None:
    repo = SqliteTrackRepository(tmp_path / "lib.db")
    repo.upsert_playlist(_playlist("spotify", "Mix", A))
    repo.upsert_playlist(_playlist("apple", "Mix", B))
    repo.upsert_playlist(_playlist("apple", "Other", C))

    result = repo.get_playlists({"mix"})

    assert set(result) == {"spotify", "apple"}
    assert [p.name for p in result["apple"]] == ["Mix"]
    assert repo.get_playlists(set()) == {}
    repo.close()


def test_new_remote_id_drops_stale_mirror_state(tmp_path: Path) -> None:
    repo = SqliteTrackRepository(tmp_path / "lib.db")
    repo.upsert_playlist(_playlist("spotify", "Mix", A, remote_id="old"))
    repo.mark_playlist_synced("spotify", "Mix", "old", [B], when="t1")

    repo.mark_playlist_synced("spotify", "Mix", "recreated", [C], when="t2")

    assert repo.get_playlist_synced_keys({"mix"}) == {"mix": {"spotify": {C.key}}}
    stored = repo.get_playlists({"mix"})["spotify"][0]
    assert stored.remote_id == "recreated"
    assert stored.tracks == ()
    repo.close()
