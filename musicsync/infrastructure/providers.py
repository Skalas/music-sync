"""Shared provider construction factory.

Both the CLI (sync_music.py) and the web container build the same Spotify /
Apple / Tidal providers. This factory centralizes that wiring.

Architecture note: this lives in the infrastructure layer and must NOT import
the application layer. Output/log paths are passed in as parameters so callers
(presentation) can supply the application constants without creating an
infrastructure → application dependency.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from musicsync.domain.ports import LibraryProvider
from musicsync.infrastructure.apple_provider import AppleProvider
from musicsync.infrastructure.spotify_provider import SpotifyProvider
from musicsync.infrastructure.tidal_provider import TidalProvider

logger = logging.getLogger(__name__)


def _build_spotify(
    base_dir: Path, need_write: bool, unmatched_log_path: Path
) -> SpotifyProvider:
    return SpotifyProvider(
        base_dir=base_dir,
        need_write=need_write,
        unmatched_log_path=unmatched_log_path,
    )


def _build_tidal(base_dir: Path, need_write: bool) -> TidalProvider:
    return TidalProvider(base_dir=base_dir, need_write=need_write)


def build_providers(
    base_dir: Path,
    *,
    applescript_dir: Path,
    output_path: Path,
    unmatched_log_path: Path,
    need_write: bool = False,
    need_write_spotify: bool | None = None,
    need_write_tidal: bool | None = None,
    include_spotify: bool = True,
    include_apple: bool = True,
    include_tidal: bool = True,
    skip_on_error: bool = False,
) -> list[LibraryProvider]:
    """Construct the Spotify, Apple and Tidal providers.

    Parameters
    ----------
    base_dir:
        Project root used by Spotify/Tidal for .env and token caches.
    applescript_dir, output_path:
        Paths for the Apple provider.
    unmatched_log_path:
        Path for the Spotify provider's unmatched-track log.
    need_write:
        Default OAuth write-scope request for Spotify/Tidal.
    need_write_spotify, need_write_tidal:
        Per-platform write-scope overrides (fall back to *need_write* when None).
        The CLI requests write scope only for the platform it is applying.
    include_spotify, include_apple, include_tidal:
        Per-platform inclusion toggles (the CLI's --no-X flags).
    skip_on_error:
        When True, a provider whose construction fails (including SystemExit
        from missing .env) is logged and skipped so the caller still boots with
        the remaining providers. When False, the error propagates (CLI behavior).
    """
    write_spotify = need_write if need_write_spotify is None else need_write_spotify
    write_tidal = need_write if need_write_tidal is None else need_write_tidal

    providers: list[LibraryProvider] = []

    def _add(name: str, build: Callable[[], LibraryProvider]) -> None:
        if not skip_on_error:
            providers.append(build())
            return
        try:
            providers.append(build())
        except (Exception, SystemExit) as exc:
            logger.warning("%s provider unavailable, skipping: %s", name, exc)

    if include_spotify:
        _add(
            "spotify",
            lambda: _build_spotify(base_dir, write_spotify, unmatched_log_path),
        )
    if include_apple:
        _add(
            "apple",
            lambda: AppleProvider(
                applescript_dir=applescript_dir,
                output_path=output_path,
            ),
        )
    if include_tidal:
        _add(
            "tidal",
            lambda: _build_tidal(base_dir, write_tidal),
        )

    return providers


def build_connect_provider(
    platform: str,
    base_dir: Path,
    *,
    unmatched_log_path: Path | None = None,
) -> LibraryProvider:
    """Construct a SINGLE OAuth provider for the connect flow.

    Unlike build_providers, this ignores connected-gating and does NOT swallow
    construction errors — the connect endpoint needs a provider regardless of
    current connection status so it can run the OAuth that establishes it. Read
    scope only (need_write=False); a missing-cred error is allowed to surface.
    """
    if platform == "spotify":
        if unmatched_log_path is None:
            raise ValueError("spotify connect requires unmatched_log_path")
        return _build_spotify(base_dir, False, unmatched_log_path)

    if platform == "tidal":
        return _build_tidal(base_dir, False)

    raise ValueError(f"no OAuth connect provider for platform: {platform}")
