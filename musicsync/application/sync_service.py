"""Sync orchestration: read → union → apply → persist."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from musicsync.domain.ports import LibraryProvider, TrackRepository
from musicsync.domain.track import Track
from musicsync.domain.union import compute_to_sync
from musicsync.infrastructure.spotify_provider import SpotifyProvider

logger = logging.getLogger(__name__)

TO_APPLE_PATH = Path("canciones_to_apple.txt")
TO_SPOTIFY_REVIEW_PATH = Path("to_spotify_review.txt")
UNMATCHED_LOG_PATH = Path("unmatched.log")


@dataclass
class SyncOptions:
    apply_spotify: bool = False
    apply_apple: bool = False
    apply_tidal: bool = False
    no_apple: bool = False
    no_spotify: bool = False
    no_tidal: bool = False
    dry_run: bool = False
    full: bool = False
    offline: bool = False


@dataclass
class SyncResult:
    to_sync: dict[str, list[Track]] = field(default_factory=dict)
    applied: dict[str, int] = field(default_factory=dict)
    skipped_providers: list[str] = field(default_factory=list)


class SyncService:
    def __init__(
        self,
        repo: TrackRepository,
        providers: list[LibraryProvider],
        *,
        state_json_path: Path | None = None,
    ) -> None:
        self._repo = repo
        self._providers = {p.name: p for p in providers}
        self._state_json_path = state_json_path

    def run(self, options: SyncOptions) -> SyncResult:
        result = SyncResult()

        if self._state_json_path is not None:
            self._repo.migrate_state_json(str(self._state_json_path))

        if options.full:
            self._repo.clear_synced()

        if not options.offline:
            for provider in self._providers.values():
                if self._should_skip_provider(provider.name, options):
                    continue
                try:
                    tracks = provider.read_liked()
                    self._repo.upsert_presence(provider.name, tracks, liked=True)
                    logger.info("  %s liked: %d", provider.name, len(tracks))
                except Exception as exc:
                    if provider.name == "tidal":
                        logger.error(
                            "Tidal: omitiendo dirección (%s). "
                            "Spotify⇄Apple continúan.",
                            exc,
                        )
                        result.skipped_providers.append("tidal")
                    else:
                        raise

        presence = self._repo.get_liked_by_platform()
        active_platforms = [
            p
            for p in ("spotify", "apple", "tidal")
            if not self._should_skip_provider(p, options)
        ]
        presence_filtered = {p: presence.get(p, {}) for p in active_platforms}

        already_synced = {
            p: self._repo.get_synced_keys(p) for p in active_platforms
        }
        to_sync = compute_to_sync(presence_filtered, already_synced)
        result.to_sync = to_sync

        for platform, tracks in to_sync.items():
            logger.info("  Faltan en %s: %d", platform, len(tracks))

        if options.dry_run:
            logger.info("(dry-run) No se escribe nada. Diff calculado arriba.")
            return result

        now = _utc_now_iso()

        if not options.no_apple and "apple" in to_sync:
            self._apply_apple(to_sync["apple"], options, result, now)

        if not options.no_spotify and "spotify" in to_sync:
            self._apply_spotify(to_sync["spotify"], options, result, now)

        if not options.no_tidal and "tidal" in to_sync and "tidal" not in result.skipped_providers:
            self._apply_tidal(to_sync["tidal"], options, result, now)

        return result

    def _should_skip_provider(self, platform: str, options: SyncOptions) -> bool:
        if platform == "tidal" and options.no_tidal:
            return True
        return False

    def _apply_apple(
        self,
        tracks: list[Track],
        options: SyncOptions,
        result: SyncResult,
        now: str,
    ) -> None:
        if not tracks:
            logger.info("Apple Music ya está al día (0 nuevas).")
            return

        provider = self._providers.get("apple")
        if provider is None:
            return

        if options.apply_apple:
            applied = provider.apply_likes(tracks)
            result.applied["apple"] = len(applied)
            self._repo.mark_synced("apple", [t.key for t in applied], when=now)
        else:
            TO_APPLE_PATH.write_text(
                "\n".join(t.line for t in tracks) + "\n", encoding="utf-8"
            )
            logger.info(
                "-> %d candidatos escritos en %s.\n"
                "   Revísalos y corre de nuevo con --apply-apple para aplicarlos.",
                len(tracks),
                TO_APPLE_PATH.name,
            )

    def _apply_spotify(
        self,
        tracks: list[Track],
        options: SyncOptions,
        result: SyncResult,
        now: str,
    ) -> None:
        if not tracks:
            logger.info("Spotify ya está al día (0 nuevas).")
            return

        provider = self._providers.get("spotify")
        if provider is None:
            return

        if options.apply_spotify:
            applied = provider.apply_likes(tracks)
            result.applied["spotify"] = len(applied)
            self._repo.mark_synced("spotify", [t.key for t in applied], when=now)
        else:
            if isinstance(provider, SpotifyProvider):
                provider.write_review(tracks, TO_SPOTIFY_REVIEW_PATH)
            logger.info(
                "-> %d candidatos escritos en %s.\n"
                "   Revísalos y corre de nuevo con --apply-spotify para aplicarlos.",
                len(tracks),
                TO_SPOTIFY_REVIEW_PATH.name,
            )

    def _apply_tidal(
        self,
        tracks: list[Track],
        options: SyncOptions,
        result: SyncResult,
        now: str,
    ) -> None:
        if not tracks:
            logger.info("Tidal ya está al día (0 nuevas).")
            return

        provider = self._providers.get("tidal")
        if provider is None:
            return

        if not options.apply_tidal:
            logger.info(
                "-> %d candidatos para Tidal (usa --apply-tidal para aplicar).",
                len(tracks),
            )
            return

        try:
            applied = provider.apply_likes(tracks)
            result.applied["tidal"] = len(applied)
            self._repo.mark_synced("tidal", [t.key for t in applied], when=now)
        except Exception as exc:
            logger.error(
                "Tidal: fallo al aplicar likes (%s). Dirección omitida.", exc
            )
            result.skipped_providers.append("tidal")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
