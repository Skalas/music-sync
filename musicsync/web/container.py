"""Composition root for the web layer.

Mirrors sync_music.py's provider wiring, but as a reusable factory so
tests can inject a seeded temp DB path and/or mock providers instead of
building the real ones (which require .env credentials).
"""

from __future__ import annotations

import os
import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from musicsync.application.output_paths import (
    APPLESCRIPT_DIR,
    BASE_DIR,
    DEFAULT_DB,
    STATE_PATH,
    TO_APPLE_PATH,
    UNMATCHED_LOG_PATH,
)
from musicsync.application.playlist_sync_service import PlaylistSyncService
from musicsync.application.sync_service import SyncService
from musicsync.domain.platforms import PLATFORMS
from musicsync.domain.ports import LibraryProvider, PlaylistProvider
from musicsync.infrastructure.providers import build_playlist_providers, build_providers
from musicsync.infrastructure.spotify_provider import (
    PlaylistScope,
    SpotifyProvider,
    cached_token_scopes,
    playlist_scopes,
)
from musicsync.infrastructure.sqlite_repository import SqliteTrackRepository
from musicsync.infrastructure.tidal_provider import TidalProvider


def connection_status(platform: str, base_dir: Path = BASE_DIR) -> tuple[bool, bool]:
    """Return ``(configured, connected)`` for a platform.

    Single source of truth shared by ``_auth_status`` (status endpoint) and the
    web container's connected-gating, so the two never diverge. Callers must have
    already loaded ``.env`` (``build_container`` does this once at startup).

    - spotify: configured = SPOTIPY_* creds present; connected = .cache token exists.
    - tidal:   configured = CLIENT_ID + REDIRECT_URI present (PKCE, no secret);
               connected = .tidal-cache token exists.
    - apple:   local automation — configured/connected = osascript available.
    """
    if platform == "spotify":
        configured = bool(
            os.environ.get("SPOTIPY_CLIENT_ID")
            and os.environ.get("SPOTIPY_CLIENT_SECRET")
            and os.environ.get("SPOTIPY_REDIRECT_URI")
        )
        connected = (base_dir / ".cache").exists()
        return configured, connected

    if platform == "tidal":
        configured = bool(
            os.environ.get("TIDAL_CLIENT_ID") and os.environ.get("TIDAL_REDIRECT_URI")
        )
        connected = (base_dir / ".tidal-cache").exists()
        return configured, connected

    if platform == "apple":
        available = shutil.which("osascript") is not None
        return available, available

    return False, False


@dataclass
class Container:
    """Holds the wired-up services for a single request lifetime."""

    repo: SqliteTrackRepository
    providers: list[LibraryProvider]
    sync_service: SyncService
    playlist_service: PlaylistSyncService
    playlist_gating: dict[str, str] = field(default_factory=dict)
    """{platform: reason} for connected platforms left out of playlist reads."""
    # Serializes whole SyncService.run sequences across concurrent web threads
    # (post_sync, the SSE executor, apply_platform).
    sync_lock: threading.Lock = field(default_factory=threading.Lock)


def _build_providers(
    *,
    need_write_spotify: bool = False,
    need_write_tidal: bool = False,
    interactive: bool = False,
) -> list[LibraryProvider]:
    """Construct only providers whose platform is actually CONNECTED.

    The web app must read only platforms that are set up, so unconnected
    platforms are never built — live sync then never touches them.  Read paths
    keep *interactive* False: an expired/unrefreshable token raises (and the
    platform is skipped) instead of opening a browser OAuth inside a request.
    Only the explicit Apply click passes True.
    skip_on_error=True remains a belt-and-suspenders guard for any
    construction-time failure.
    """
    connected = {p: all(connection_status(p, BASE_DIR)) for p in ("spotify", "apple", "tidal")}
    return build_providers(
        BASE_DIR,
        applescript_dir=APPLESCRIPT_DIR,
        output_path=TO_APPLE_PATH,
        unmatched_log_path=UNMATCHED_LOG_PATH,
        need_write=False,
        need_write_spotify=need_write_spotify,
        need_write_tidal=need_write_tidal,
        skip_on_error=True,
        include_spotify=connected["spotify"],
        include_apple=connected["apple"],
        include_tidal=connected["tidal"],
        interactive=interactive,
    )


SPOTIFY_PLAYLIST_SCOPE_MISSING = (
    "Spotify token lacks playlist access; run `uv run python sync_music.py "
    "--list-playlists` once to re-authorize."
)


def _build_playlist_providers() -> tuple[list[PlaylistProvider], dict[str, str]]:
    """Read-only playlist providers for CONNECTED platforms, plus gating notes.

    Spotify joins only when its cached token already carries every playlist read
    scope: building it otherwise would pop an interactive OAuth from the server.
    """
    connected = {p: all(connection_status(p, BASE_DIR)) for p in PLATFORMS}
    gating: dict[str, str] = {}
    read_scopes = set(playlist_scopes(PlaylistScope.READ))
    if connected["spotify"] and not read_scopes <= cached_token_scopes(BASE_DIR):
        connected["spotify"] = False
        gating["spotify"] = SPOTIFY_PLAYLIST_SCOPE_MISSING
    providers = build_playlist_providers(
        BASE_DIR,
        applescript_dir=APPLESCRIPT_DIR,
        output_path=TO_APPLE_PATH,
        unmatched_log_path=UNMATCHED_LOG_PATH,
        include_spotify=connected["spotify"],
        include_apple=connected["apple"],
        include_tidal=connected["tidal"],
        skip_on_error=True,
        interactive=False,
    )
    return providers, gating


def _upgrade_write_scope(
    providers: list[LibraryProvider], platform: str
) -> list[LibraryProvider]:
    """Swap the apply target for a write-scoped OAuth instance when applicable.

    Interactive on purpose: Apply is an explicit click on the user's own machine,
    so a missing write grant may open the browser consent (unlike read paths).
    """
    upgraded: list[LibraryProvider] = []
    for provider in providers:
        if provider.name != platform:
            upgraded.append(provider)
            continue
        if platform == "spotify" and isinstance(provider, SpotifyProvider):
            upgraded.append(
                SpotifyProvider(
                    base_dir=provider._base_dir,  # noqa: SLF001
                    unmatched_log_path=provider._unmatched_log_path,  # noqa: SLF001
                    need_write=True,
                )
            )
        elif platform == "tidal" and isinstance(provider, TidalProvider):
            upgraded.append(
                TidalProvider(
                    base_dir=provider._base_dir,  # noqa: SLF001
                    need_write=True,
                )
            )
        else:
            upgraded.append(provider)
    return upgraded


def build_sync_service_for_apply(container: Container, platform: str) -> SyncService:
    """SyncService wired with write OAuth scope for the platform being applied."""
    if container.providers:
        providers = _upgrade_write_scope(container.providers, platform)
    else:
        providers = _build_providers(
            need_write_spotify=(platform == "spotify"),
            need_write_tidal=(platform == "tidal"),
            interactive=True,
        )
    return SyncService(container.repo, providers, state_json_path=STATE_PATH)


def build_container(
    db_path: Path | None = None,
    providers: list[LibraryProvider] | None = None,
    playlist_providers: list[PlaylistProvider] | None = None,
) -> Container:
    """Build the full application container.

    Parameters
    ----------
    db_path:
        Override the SQLite path (useful for tests pointing at a seeded temp DB).
        Defaults to *library.db* next to the project root.
    providers:
        Override the provider list entirely.  Pass an empty list or mock
        providers to avoid touching .env / network during tests.
    playlist_providers:
        Override the (read-only) playlist providers.  Defaults to an empty list
        when *providers* is overridden, so tests never build real ones.
    """
    # Populate the process env once so any os.environ reader sees the .env creds.
    load_dotenv(BASE_DIR / ".env")

    resolved_db = db_path or DEFAULT_DB
    repo = SqliteTrackRepository(resolved_db)

    providers_overridden = providers is not None
    if providers is None:
        providers = _build_providers()

    service = SyncService(repo, providers, state_json_path=STATE_PATH)

    gating: dict[str, str] = {}
    if playlist_providers is None:
        playlist_providers, gating = (
            ([], {}) if providers_overridden else _build_playlist_providers()
        )

    return Container(
        repo=repo,
        providers=providers,
        sync_service=service,
        # Web is read-only for playlists (dry-run preview): no review file.
        playlist_service=PlaylistSyncService(repo, playlist_providers),
        playlist_gating=gating,
    )
