"""Sync orchestration: read → union → apply → persist."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from musicsync.application.output_paths import TO_APPLE_PATH, TO_SPOTIFY_REVIEW_PATH
from musicsync.domain._time import utc_now_iso
from musicsync.domain.platforms import PLATFORMS
from musicsync.domain.ports import LibraryProvider, TrackRepository
from musicsync.domain.track import Track
from musicsync.domain.union import compute_to_sync

logger = logging.getLogger(__name__)


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
                            exc.__class__.__name__,
                        )
                        result.skipped_providers.append("tidal")
                    else:
                        raise

        presence = self._repo.get_liked_by_platform()
        active_platforms = [
            p
            for p in PLATFORMS
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

        now = utc_now_iso()

        # Explicit apply order — apple before spotify before tidal — preserved
        # because the order can affect which review files / log lines appear.
        apply_order = ("apple", "spotify", "tidal")
        review_paths: dict[str, Path | None] = {
            "apple": TO_APPLE_PATH,
            "spotify": TO_SPOTIFY_REVIEW_PATH,
            "tidal": None,
        }
        skip_apply = {
            "apple": options.no_apple,
            "spotify": options.no_spotify,
            "tidal": options.no_tidal,
        }
        apply_flags = {
            "apple": options.apply_apple,
            "spotify": options.apply_spotify,
            "tidal": options.apply_tidal,
        }

        for platform in apply_order:
            if skip_apply[platform] or platform not in to_sync:
                continue
            if platform in result.skipped_providers:
                continue
            self._apply_platform(
                platform,
                to_sync[platform],
                apply_flags[platform],
                result,
                now,
                review_path=review_paths[platform],
            )

        return result

    def _should_skip_provider(self, platform: str, options: SyncOptions) -> bool:
        """Return True only for tidal when --no-tidal is set.

        INTENTIONAL ASYMMETRY: --no-tidal excludes Tidal from the union entirely
        (no read, no write). --no-spotify and --no-apple are directional only —
        they suppress writes toward that platform but still include it in the N-way
        union reads. Only tidal is handled here because it is the only full-exclude flag.
        """
        if platform == "tidal" and options.no_tidal:
            return True
        return False

    def _apply_platform(
        self,
        platform: str,
        tracks: list[Track],
        apply_flag: bool,
        result: SyncResult,
        now: str,
        *,
        review_path: Path | None = None,
    ) -> None:
        """Shared skeleton: guard empty → look up provider → apply or review."""
        if not tracks:
            logger.info("%s ya está al día (0 nuevas).", platform.capitalize())
            return

        provider = self._providers.get(platform)
        if provider is None:
            return

        if apply_flag:
            try:
                applied = provider.apply_likes(tracks)
                result.applied[platform] = len(applied)
                self._repo.mark_synced(platform, [t.key for t in applied], when=now)
            except Exception as exc:
                if provider.graceful_on_apply_error:
                    logger.error(
                        "%s: fallo al aplicar likes (%s). Dirección omitida.",
                        provider.name.capitalize(),
                        exc.__class__.__name__,
                    )
                    result.skipped_providers.append(provider.name)
                else:
                    raise
        else:
            if review_path is not None:
                provider.write_review(tracks, review_path)
                logger.info(
                    "-> %d candidatos escritos en %s.\n"
                    "   Revísalos y corre de nuevo con --apply-%s para aplicarlos.",
                    len(tracks),
                    review_path.name,
                    provider.name,
                )
            else:
                # No review file for this platform — just surface the count
                logger.info(
                    "-> %d candidatos para %s (usa --apply-%s para aplicar).",
                    len(tracks),
                    provider.name.capitalize(),
                    provider.name,
                )
