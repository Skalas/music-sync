"""T5 — apple_catalog table: idempotent upserts, retry window, key-merge following."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from musicsync.domain.track import Track, normalize_key
from musicsync.infrastructure.sqlite_repository import SqliteTrackRepository

NOW = "2026-10-08T00:00:00+00:00"
LONG_AGO = "2026-08-01T00:00:00+00:00"
STALE_BEFORE = "2026-09-08T00:00:00+00:00"


@pytest.fixture()
def repo(tmp_path: Path) -> Iterator[SqliteTrackRepository]:
    r = SqliteTrackRepository(tmp_path / "lib.db")
    yield r
    r.close()


def _catalog_rows(repo: SqliteTrackRepository) -> dict[str, str | None]:
    rows = repo._conn.execute("SELECT key, catalog_id FROM apple_catalog").fetchall()  # noqa: SLF001
    return {row["key"]: row["catalog_id"] for row in rows}


def _pending_keys(repo: SqliteTrackRepository, storefront: str = "sv") -> list[str]:
    return [t.key for t in repo.pending_apple_links(storefront, stale_before=STALE_BEFORE)]


def test_pending_lists_unresolved_tracks_with_metadata(repo: SqliteTrackRepository) -> None:
    track = Track(name="Song", artist="Artist", album="Album", duration_sec=200, isrc="ISRC1")
    repo.upsert_presence("spotify", [track])

    [target] = repo.pending_apple_links("sv", stale_before=STALE_BEFORE)

    assert target.key == track.key
    assert (target.album, target.duration_sec, target.isrc) == ("Album", 200, "ISRC1")


def test_save_is_idempotent_and_resolved_rows_are_not_pending(
    repo: SqliteTrackRepository,
) -> None:
    track = Track(name="Song", artist="Artist")
    repo.upsert_presence("spotify", [track])

    repo.save_apple_link(track.key, "123", storefront="sv", resolved_at=NOW)
    repo.save_apple_link(track.key, "123", storefront="sv", resolved_at=NOW)

    assert _catalog_rows(repo) == {track.key: "123"}
    assert _pending_keys(repo) == []


def test_negative_row_is_retried_only_after_window(repo: SqliteTrackRepository) -> None:
    fresh = Track(name="Fresh", artist="Artist")
    stale = Track(name="Stale", artist="Artist")
    repo.upsert_presence("spotify", [fresh, stale])

    repo.save_apple_link(fresh.key, None, storefront="sv", resolved_at=NOW)
    repo.save_apple_link(stale.key, None, storefront="sv", resolved_at=LONG_AGO)

    assert _pending_keys(repo) == [stale.key]


def test_storefront_change_makes_rows_pending(repo: SqliteTrackRepository) -> None:
    track = Track(name="Song", artist="Artist")
    repo.upsert_presence("spotify", [track])
    repo.save_apple_link(track.key, "123", storefront="us", resolved_at=NOW)

    assert _pending_keys(repo, "sv") == [track.key]


def test_limit_prefers_never_attempted(repo: SqliteTrackRepository) -> None:
    a = Track(name="A", artist="X")
    b = Track(name="B", artist="X")
    repo.upsert_presence("spotify", [a, b])
    repo.save_apple_link(a.key, None, storefront="sv", resolved_at=LONG_AGO)

    targets = repo.pending_apple_links("sv", stale_before=STALE_BEFORE, limit=1)

    assert [t.key for t in targets] == [b.key]


def test_save_for_unknown_key_is_ignored(repo: SqliteTrackRepository) -> None:
    repo.save_apple_link("ghost", "1", storefront="sv", resolved_at=NOW)
    assert _catalog_rows(repo) == {}


def test_enriched_rows_expose_catalog_id_and_storefront(repo: SqliteTrackRepository) -> None:
    linked = Track(name="Linked", artist="Artist")
    plain = Track(name="Plain", artist="Artist")
    repo.upsert_presence("spotify", [linked, plain])
    repo.save_apple_link(linked.key, "999", storefront="sv", resolved_at=NOW)

    rows = {row["key"]: row for row in repo.iter_enriched_rows()}

    assert rows[linked.key]["apple_catalog_id"] == "999"
    assert rows[linked.key]["apple_storefront"] == "sv"
    assert rows[plain.key]["apple_catalog_id"] is None


def test_dedupe_moves_catalog_id_to_canonical_key(repo: SqliteTrackRepository) -> None:
    title_only = Track(name="Song", artist="")
    full = Track(name="Song", artist="Artist")
    repo.upsert_presence("apple", [title_only])
    repo.upsert_presence("spotify", [full])
    repo.save_apple_link(title_only.key, "777", storefront="sv", resolved_at=NOW)

    assert repo.dedupe_title_only_keys() == 1

    assert _catalog_rows(repo) == {full.key: "777"}


def test_merge_keeps_canonical_id_and_drops_orphan_row(repo: SqliteTrackRepository) -> None:
    orphan = Track(name="Song", artist="")
    canonical = Track(name="Song", artist="Artist")
    repo.upsert_presence("apple", [orphan])
    repo.upsert_presence("spotify", [canonical])
    repo.save_apple_link(orphan.key, "orphan-id", storefront="sv", resolved_at=NOW)
    repo.save_apple_link(canonical.key, "canonical-id", storefront="sv", resolved_at=NOW)

    repo.merge_keys({orphan.key: canonical.key})

    assert _catalog_rows(repo) == {canonical.key: "canonical-id"}


def test_merge_negative_orphan_never_overwrites(repo: SqliteTrackRepository) -> None:
    orphan = Track(name="Song", artist="")
    canonical = Track(name="Song", artist="Artist")
    repo.upsert_presence("apple", [orphan])
    repo.upsert_presence("spotify", [canonical])
    repo.save_apple_link(orphan.key, None, storefront="sv", resolved_at=NOW)

    repo.merge_keys({orphan.key: canonical.key})

    assert _catalog_rows(repo) == {}
    assert canonical.key in _pending_keys(repo)


def test_merge_carries_isrc_to_canonical(repo: SqliteTrackRepository) -> None:
    orphan = Track(name="Song", artist="", isrc="ISRC1")
    canonical = Track(name="Song", artist="Artist")
    repo.upsert_presence("apple", [orphan])
    repo.upsert_presence("spotify", [canonical])

    repo.merge_keys({orphan.key: canonical.key})

    [target] = repo.pending_apple_links("sv", stale_before=STALE_BEFORE)
    assert target.isrc == "ISRC1"


def test_title_only_drop_on_artist_enrich_removes_catalog_row(
    repo: SqliteTrackRepository,
) -> None:
    title_only = Track(name="Song", artist="")
    repo.upsert_presence("tidal", [title_only])
    repo.save_apple_link(title_only.key, None, storefront="sv", resolved_at=NOW)

    repo.upsert_presence("tidal", [Track(name="Song", artist="Artist")])

    assert title_only.key not in _catalog_rows(repo)
    assert normalize_key("Song", "Artist") in _pending_keys(repo)
