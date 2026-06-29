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
from musicsync.application.sync_service import SyncService
from musicsync.domain.ports import LibraryProvider
from musicsync.infrastructure.providers import build_providers
from musicsync.infrastructure.sqlite_repository import SqliteTrackRepository


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
    # Serializes whole SyncService.run sequences across concurrent web threads
    # (post_sync, the SSE executor, apply_platform).
    sync_lock: threading.Lock = field(default_factory=threading.Lock)


def _build_providers() -> list[LibraryProvider]:
    """Construct only providers whose platform is actually CONNECTED.

    The web app must read only platforms that are set up, so unconnected
    platforms are never built — live sync then never touches them and never
    triggers an interactive OAuth/browser popup.  skip_on_error=True remains a
    belt-and-suspenders guard for any construction-time failure.
    """
    connected = {p: all(connection_status(p, BASE_DIR)) for p in ("spotify", "apple", "tidal")}
    return build_providers(
        BASE_DIR,
        applescript_dir=APPLESCRIPT_DIR,
        output_path=TO_APPLE_PATH,
        unmatched_log_path=UNMATCHED_LOG_PATH,
        need_write=False,
        skip_on_error=True,
        include_spotify=connected["spotify"],
        include_apple=connected["apple"],
        include_tidal=connected["tidal"],
    )


def build_container(
    db_path: Path | None = None,
    providers: list[LibraryProvider] | None = None,
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
    """
    # Populate the process env once so any os.environ reader sees the .env creds.
    load_dotenv(BASE_DIR / ".env")

    resolved_db = db_path or DEFAULT_DB
    repo = SqliteTrackRepository(resolved_db)

    if providers is None:
        providers = _build_providers()

    service = SyncService(repo, providers, state_json_path=STATE_PATH)

    return Container(
        repo=repo,
        providers=providers,
        sync_service=service,
    )
