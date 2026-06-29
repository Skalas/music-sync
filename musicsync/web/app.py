"""FastAPI application factory for music-sync.

Presentation layer only — all business logic stays in musicsync.application.
Route handlers are module-level functions taking the Container (and any deps) as
parameters; create_app registers thin wrappers so the factory stays small and the
handlers are unit-testable.
"""

from __future__ import annotations

import asyncio
import io
import json as _json
import logging
import threading
from pathlib import Path
from typing import Annotated, Any

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from musicsync.application.csv_export import write_csv
from musicsync.application.output_paths import UNMATCHED_LOG_PATH
from musicsync.application.sync_service import SyncOptions
from musicsync.domain.platforms import PLATFORMS
from musicsync.domain.ports import LibraryProvider
from musicsync.infrastructure.providers import build_connect_provider
from musicsync.web.container import BASE_DIR, Container, build_container, connection_status
from musicsync.web.links import track_url

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pydantic schemas — defined at module scope (not inside create_app) so Pydantic
# can fully resolve their forward references when the routes are registered.
# ---------------------------------------------------------------------------


class TrackLinks(BaseModel):
    spotify: str | None
    apple: str
    tidal: str | None


class TrackPresence(BaseModel):
    spotify: bool
    apple: bool
    tidal: bool


class LibraryTrack(BaseModel):
    key: str
    name: str
    artist: str
    presence: TrackPresence
    links: TrackLinks
    on_all_three: bool
    album: str | None = None
    artwork_url: str | None = None
    duration_sec: int | None = None
    year: str | None = None
    # Per-platform date the track was added; keys are platform ids, values are ISO strings or null.
    added_at: dict[str, str | None] = {}


class LibraryPage(BaseModel):
    items: list[LibraryTrack]
    total: int
    page: int
    page_size: int


class AuthStatus(BaseModel):
    platform: str
    configured: bool
    connected: bool


class ConnectResult(BaseModel):
    started: bool
    message: str


class SyncRequest(BaseModel):
    # "Sync now" defaults to a live read that refreshes the DB from platforms.
    offline: bool = False


class SyncDiff(BaseModel):
    to_sync: dict[str, list[dict[str, str]]]
    counts: dict[str, int]


class ApplyResult(BaseModel):
    platform: str
    applied: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _web_sync_options(offline: bool = False) -> SyncOptions:
    """Shared dry-run defaults for the web "Sync now" / stream live read."""
    return SyncOptions(dry_run=True, offline=offline, skip_unavailable_providers=True)


def _auth_status(platform: str) -> AuthStatus:
    """Check whether credentials are configured and a token/cache exists."""
    if platform not in PLATFORMS:
        raise HTTPException(status_code=404, detail=f"Unknown platform: {platform}")

    configured, connected = connection_status(platform, BASE_DIR)
    return AuthStatus(platform=platform, configured=configured, connected=connected)


def _row_to_library_track(row: dict[str, Any]) -> LibraryTrack:
    """Convert an enriched repo row to the API response shape."""
    presence = TrackPresence(
        spotify=bool(row["spotify"]),
        apple=bool(row["apple"]),
        tidal=bool(row["tidal"]),
    )
    # Enriched rows carry spotify_id / tidal_id (from iter_enriched_rows) so we
    # can build deep links; Apple has no stored id and falls back to a search URL.
    links = TrackLinks(
        spotify=track_url(
            "spotify",
            platform_id=row.get("spotify_id"),
            name=row["name"],
            artist=row["artist"],
        ),
        apple=track_url("apple", platform_id=None, name=row["name"], artist=row["artist"]) or "",
        tidal=track_url(
            "tidal",
            platform_id=row.get("tidal_id"),
            name=row["name"],
            artist=row["artist"],
        ),
    )
    on_all_three = presence.spotify and presence.apple and presence.tidal
    added_at = {
        "spotify": row.get("spotify_added_at"),
        "apple": row.get("apple_added_at"),
        "tidal": row.get("tidal_added_at"),
    }
    return LibraryTrack(
        key=row["key"],
        name=row["name"],
        artist=row["artist"],
        presence=presence,
        links=links,
        on_all_three=on_all_three,
        album=row.get("album"),
        artwork_url=row.get("artwork_url"),
        duration_sec=row.get("duration_sec"),
        year=row.get("year"),
        added_at=added_at,
    )


def require_xhr_header(
    x_requested_with: str | None = Header(default=None),
) -> None:
    """CSRF guard for state-changing endpoints.

    Requiring a custom request header forces a CORS preflight for cross-origin
    requests, which the localhost:5173-only CORS policy then rejects.  A simple
    cross-origin form/POST cannot set this header, so it is blocked here with 403.
    """
    if x_requested_with != "XMLHttpRequest":
        raise HTTPException(
            status_code=403,
            detail="Missing or invalid X-Requested-With header.",
        )


# ---------------------------------------------------------------------------
# Connect-flow concurrency guard: at most one OAuth flow per platform in flight
# (they would collide on the :8080 callback port).
# ---------------------------------------------------------------------------

_connect_in_progress: set[str] = set()
_connect_lock = threading.Lock()


def _diff_to_response(result: Any) -> SyncDiff:
    to_sync: dict[str, list[dict[str, str]]] = {}
    counts: dict[str, int] = {}
    for platform, tracks in result.to_sync.items():
        to_sync[platform] = [
            {"key": t.key, "name": t.name, "artist": t.artist} for t in tracks
        ]
        counts[platform] = len(tracks)
    return SyncDiff(to_sync=to_sync, counts=counts)


# ---------------------------------------------------------------------------
# Route handlers (module-level; take the Container as an explicit dependency)
# ---------------------------------------------------------------------------


def get_library(
    container: Container,
    *,
    q: str = "",
    presence_filter: str = "",
    sort_by: str = "",
    page: int = 1,
    page_size: int = 50,
) -> LibraryPage:
    rows = container.repo.iter_enriched_rows(
        sort_by=sort_by if sort_by else None
    )
    tracks = [_row_to_library_track(r) for r in rows]

    if q:
        ql = q.lower()
        tracks = [t for t in tracks if ql in t.name.lower() or ql in t.artist.lower()]

    if presence_filter == "all_three":
        tracks = [t for t in tracks if t.on_all_three]
    elif presence_filter == "missing_somewhere":
        tracks = [t for t in tracks if not t.on_all_three]

    total = len(tracks)
    start = (page - 1) * page_size
    return LibraryPage(
        items=tracks[start : start + page_size],
        total=total,
        page=page,
        page_size=page_size,
    )


def get_auth_status(platform: str) -> AuthStatus:
    return _auth_status(platform)


def connect_platform(container: Container, platform: str) -> ConnectResult:
    if platform == "apple":
        return ConnectResult(
            started=False,
            message="Apple Music uses local automation (AppleScript/Shortcuts), not OAuth.",
        )

    if platform not in PLATFORMS:
        raise HTTPException(status_code=404, detail=f"Unknown platform: {platform}")

    status = _auth_status(platform)
    if not status.configured:
        raise HTTPException(
            status_code=400,
            detail=f"{platform} credentials not configured in .env.",
        )

    # Reject a duplicate connect: a second OAuth flow would collide on :8080.
    with _connect_lock:
        if platform in _connect_in_progress:
            raise HTTPException(
                status_code=409,
                detail=f"A {platform} connect flow is already in progress.",
            )
        _connect_in_progress.add(platform)

    # Build a fresh provider on demand: connected-gating keeps unconnected
    # platforms out of container.providers, but the connect flow is exactly
    # the unconnected case — we only need configured creds (checked above).
    provider = build_connect_provider(
        platform, BASE_DIR, unmatched_log_path=UNMATCHED_LOG_PATH
    )

    # Start the OAuth flow in a background thread so the HTTP response returns
    # immediately.  read_liked() triggers the browser + uses :8080 for the
    # callback — we must not await it here.
    def _run() -> None:
        try:
            provider.read_liked()
        except Exception as exc:
            logger.warning("connect %s failed: %s", platform, exc, exc_info=True)
        finally:
            with _connect_lock:
                _connect_in_progress.discard(platform)

    threading.Thread(target=_run, daemon=True).start()
    return ConnectResult(started=True, message=f"{platform} OAuth flow started in browser.")


def post_sync(container: Container, body: SyncRequest) -> SyncDiff:
    # Live read refreshes DB presence from connected platforms; dry_run keeps
    # remote writes off. Unconnected platforms are skipped, not fatal.
    with container.sync_lock:
        result = container.sync_service.run(_web_sync_options(body.offline))
    return _diff_to_response(result)


def apply_platform(container: Container, platform: str) -> ApplyResult:
    if platform not in PLATFORMS:
        raise HTTPException(status_code=404, detail=f"Unknown platform: {platform}")

    # offline=True applies the diff from existing DB presence without a fresh
    # network read of every provider; the apply phase (provider.apply_likes)
    # still runs for the requested platform.
    options = SyncOptions(
        apply_spotify=(platform == "spotify"),
        apply_apple=(platform == "apple"),
        apply_tidal=(platform == "tidal"),
        dry_run=False,
        offline=True,
    )
    with container.sync_lock:
        result = container.sync_service.run(options)
    applied = result.applied.get(platform, 0)
    return ApplyResult(platform=platform, applied=applied)


def export_csv_endpoint(container: Container) -> StreamingResponse:
    rows = container.repo.iter_export_rows()
    buf = io.StringIO()
    write_csv(rows, buf)
    buf.seek(0)

    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=library.csv"},
    )


# ---------------------------------------------------------------------------
# App factory — thin: builds the container and registers wrappers.
# ---------------------------------------------------------------------------


def create_app(
    db_path: Path | None = None,
    providers: list[LibraryProvider] | None = None,
) -> FastAPI:
    """Create and configure the FastAPI application.

    Parameters
    ----------
    db_path:
        Optional override for the SQLite library path.  Tests pass a seeded
        temp-DB path here so no real library is touched.
    providers:
        Optional provider list override.  Pass mock providers in tests to avoid
        requiring .env credentials.  Defaults to the real providers built from .env.
    """
    container = build_container(db_path, providers=providers)

    app = FastAPI(title="music-sync API", version="0.2.0")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173"],
        allow_methods=["GET", "POST"],
        allow_headers=["X-Requested-With", "Content-Type"],
    )

    @app.get("/api/library", response_model=LibraryPage)
    def _library(
        q: str = Query(default="", description="Substring filter on name or artist"),
        presence_filter: str = Query(
            default="",
            alias="filter",
            description="Presence filter: 'all_three' | 'missing_somewhere'",
        ),
        sort_by: str = Query(
            default="",
            description="Sort order: 'added_at' sorts by date added descending",
        ),
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=50, ge=1, le=500),
    ) -> LibraryPage:
        return get_library(
            container,
            q=q,
            presence_filter=presence_filter,
            sort_by=sort_by,
            page=page,
            page_size=page_size,
        )

    @app.get("/api/auth/{platform}", response_model=AuthStatus)
    def _auth(platform: str) -> AuthStatus:
        return get_auth_status(platform)

    @app.post(
        "/api/auth/{platform}/connect",
        response_model=ConnectResult,
        dependencies=[Depends(require_xhr_header)],
    )
    def _connect(platform: str) -> ConnectResult:
        return connect_platform(container, platform)

    @app.post(
        "/api/sync",
        response_model=SyncDiff,
        dependencies=[Depends(require_xhr_header)],
    )
    def _sync(body: Annotated[SyncRequest, Body()] = SyncRequest()) -> SyncDiff:
        return post_sync(container, body)

    @app.get("/api/sync/stream")
    async def _sync_stream() -> EventSourceResponse:
        # NOTE: SyncService has no per-track progress callback — we emit coarse
        # staged events.  Finer progress would require a SyncService progress
        # hook, which is out of scope for Sprint 2.
        async def _event_generator() -> Any:
            yield {"event": "progress", "data": "reading"}
            await asyncio.sleep(0)

            def _run() -> Any:
                with container.sync_lock:
                    return container.sync_service.run(_web_sync_options(offline=False))

            result = await asyncio.to_thread(_run)

            yield {"event": "progress", "data": "computing"}
            await asyncio.sleep(0)

            counts = {p: len(t) for p, t in result.to_sync.items()}
            yield {"event": "done", "data": _json.dumps(counts)}

        return EventSourceResponse(_event_generator())

    @app.post(
        "/api/apply/{platform}",
        response_model=ApplyResult,
        dependencies=[Depends(require_xhr_header)],
    )
    def _apply(platform: str) -> ApplyResult:
        return apply_platform(container, platform)

    @app.get("/api/export.csv")
    def _export_csv() -> StreamingResponse:
        return export_csv_endpoint(container)

    return app
