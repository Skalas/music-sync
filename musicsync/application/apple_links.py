"""Batch use case: resolve stored tracks to Apple Music catalog ids.

Runs only from the CLI batch step — never on the web request path. Each track
gets a row in ``apple_catalog``: a catalog id when an exact match exists, or
NULL (negative cache) that is retried after ``NEGATIVE_RETRY_DAYS``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from musicsync.domain.apple_catalog import (
    AppleLinkTarget,
    CatalogSong,
    select_isrc_match,
    select_search_match,
)
from musicsync.domain.errors import PlatformOperationError
from musicsync.domain.track import primary_artist

logger = logging.getLogger(__name__)

NEGATIVE_RETRY_DAYS = 30
"""A track Apple could not match is looked up again after this many days."""


class AppleCatalogLookup(Protocol):
    """Read-only access to the Apple Music catalog of one storefront."""

    storefront: str

    def songs_by_isrc(self, isrc: str) -> list[CatalogSong]:
        """Return catalog songs carrying *isrc* (may include compilations)."""
        ...

    def search_songs(self, term: str) -> list[CatalogSong]:
        """Return catalog songs matching a free-text *term*."""
        ...


class AppleCatalogStore(Protocol):
    """Persistence for resolved Apple catalog ids."""

    def pending_apple_links(
        self, storefront: str, *, stale_before: str, limit: int | None = None
    ) -> list[AppleLinkTarget]:
        """Tracks with no row for *storefront*, or a NULL row older than *stale_before*."""
        ...

    def save_apple_link(
        self, key: str, catalog_id: str | None, *, storefront: str, resolved_at: str
    ) -> None:
        """Idempotently record the outcome for *key* (``None`` = not found)."""
        ...


@dataclass
class AppleLinkReport:
    resolved: int = 0
    unresolved: int = 0
    error: str | None = None


def _iso(moment: datetime) -> str:
    return moment.replace(microsecond=0).isoformat()


def find_catalog_song(
    lookup: AppleCatalogLookup, target: AppleLinkTarget
) -> CatalogSong | None:
    """ISRC first; fall back to a validated name + primary-artist search."""
    if target.isrc:
        match = select_isrc_match(lookup.songs_by_isrc(target.isrc), target)
        if match is not None:
            return match
    if not target.artist.strip():
        return None
    term = f"{target.name} {primary_artist(target.artist)}"
    return select_search_match(lookup.search_songs(term), target)


def resolve_apple_links(
    store: AppleCatalogStore,
    lookup: AppleCatalogLookup,
    *,
    limit: int | None = None,
    now: datetime | None = None,
) -> AppleLinkReport:
    """Resolve pending tracks and persist each outcome as soon as it is known.

    A platform error (auth, quota, network) stops the batch without writing a
    negative row for the track in flight, so it is simply retried next run.
    """
    moment = now or datetime.now(timezone.utc)
    stale_before = _iso(moment - timedelta(days=NEGATIVE_RETRY_DAYS))
    targets = store.pending_apple_links(
        lookup.storefront, stale_before=stale_before, limit=limit
    )
    report = AppleLinkReport()
    for target in targets:
        try:
            song = find_catalog_song(lookup, target)
        except PlatformOperationError as exc:
            report.error = str(exc)
            logger.error("Apple Music: resolución detenida: %s", exc)
            break
        catalog_id = song.catalog_id if song is not None else None
        store.save_apple_link(
            target.key, catalog_id, storefront=lookup.storefront, resolved_at=_iso(moment)
        )
        if catalog_id is None:
            report.unresolved += 1
        else:
            report.resolved += 1
    return report
