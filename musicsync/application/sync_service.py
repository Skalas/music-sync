"""Sync orchestration: read → union → apply → persist."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from musicsync.application.output_paths import TO_APPLE_PATH, TO_SPOTIFY_REVIEW_PATH
from musicsync.domain._time import utc_now_iso
from musicsync.domain.errors import PlatformOperationError
from musicsync.domain.platforms import PLATFORMS
from musicsync.domain.ports import LibraryProvider, TrackRepository
from musicsync.domain.track import Track
from musicsync.domain.union import (
    canonicalize_presence,
    compute_tidal_catalog,
    compute_to_sync,
    remap_synced_keys,
)

logger = logging.getLogger(__name__)

# Explicit apply order — apple before spotify before tidal — preserved because
# the order can affect which review files / log lines appear. (Distinct from the
# union read order in PLATFORMS.)
_APPLY_ORDER: tuple[str, ...] = ("apple", "spotify", "tidal")

_REVIEW_PATHS: dict[str, Path | None] = {
    "apple": TO_APPLE_PATH,
    "spotify": TO_SPOTIFY_REVIEW_PATH,
    "tidal": None,
}


@dataclass
class _ApplyPlan:
    """Per-platform apply decision: whether to skip, whether to write, where to review."""

    skip: bool
    apply_flag: bool
    review_path: Path | None


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
    skip_unavailable_providers: bool = False
    tidal_reorder: bool = False


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
                except SystemExit as exc:
                    # A provider that calls sys.exit() on missing creds (e.g.
                    # SpotifyProvider). Only the web flag tolerates it; the CLI
                    # keeps its "fill your .env" exit UX.
                    if options.skip_unavailable_providers:
                        self._skip_provider(provider, exc, result)
                    else:
                        raise
                except Exception as exc:
                    graceful = (
                        options.skip_unavailable_providers
                        or provider.graceful_on_error
                    )
                    if graceful:
                        self._skip_provider(provider, exc, result)
                    else:
                        raise

        presence = self._repo.get_liked_by_platform()
        merged = self._repo.dedupe_title_only_keys()
        if merged:
            logger.info("  Fusionadas %d clave(s) title-only duplicada(s)", merged)
            presence = self._repo.get_liked_by_platform()
        active_platforms = [
            p
            for p in PLATFORMS
            if not self._should_skip_provider(p, options)
        ]
        presence_filtered = {p: presence.get(p, {}) for p in active_platforms}
        presence_filtered, key_remap = canonicalize_presence(presence_filtered)

        already_synced = remap_synced_keys(
            {p: self._repo.get_synced_keys(p) for p in active_platforms},
            key_remap,
        )
        to_sync = compute_to_sync(presence_filtered, already_synced)
        result.to_sync = to_sync

        for platform, tracks in to_sync.items():
            logger.info("  Faltan en %s: %d", platform, len(tracks))

        if options.dry_run:
            logger.info("(dry-run) No se escribe nada. Diff calculado arriba.")
            return result

        now = utc_now_iso()
        plans = self._apply_plans(options)

        for platform in _APPLY_ORDER:
            plan = plans[platform]
            if plan.skip:
                continue
            if platform in result.skipped_providers:
                continue

            tracks = to_sync.get(platform, [])
            reorder = False
            if platform == "tidal" and options.apply_tidal:
                reorder = options.tidal_reorder
                if reorder:
                    tracks = compute_tidal_catalog(presence_filtered)
                    logger.info(
                        "Tidal reorder: catálogo completo con %d pista(s)",
                        len(tracks),
                    )

            if not tracks:
                if plan.apply_flag:
                    logger.info("%s ya está al día (0 nuevas).", platform.capitalize())
                continue

            self._apply_platform(
                platform,
                tracks,
                plan.apply_flag,
                result,
                now,
                review_path=plan.review_path,
                reorder=reorder,
            )

        return result

    @staticmethod
    def _apply_plans(options: SyncOptions) -> dict[str, _ApplyPlan]:
        """Build the per-platform apply plan from options (keyed by platform)."""
        skip = {
            "apple": options.no_apple,
            "spotify": options.no_spotify,
            "tidal": options.no_tidal,
        }
        apply_flag = {
            "apple": options.apply_apple,
            "spotify": options.apply_spotify,
            "tidal": options.apply_tidal,
        }
        return {
            platform: _ApplyPlan(
                skip=skip[platform],
                apply_flag=apply_flag[platform],
                review_path=_REVIEW_PATHS[platform],
            )
            for platform in _APPLY_ORDER
        }

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

    def _skip_provider(
        self,
        provider: LibraryProvider,
        exc: BaseException,
        result: SyncResult,
    ) -> None:
        """Log a read failure and mark the provider as skipped for this run."""
        logger.error(
            "%s: omitiendo dirección: %s",
            provider.name.capitalize(),
            exc,
        )
        result.skipped_providers.append(provider.name)

    def _apply_platform(
        self,
        platform: str,
        tracks: list[Track],
        apply_flag: bool,
        result: SyncResult,
        now: str,
        *,
        review_path: Path | None = None,
        reorder: bool = False,
    ) -> None:
        """Shared skeleton: guard empty → look up provider → apply or review."""
        if not tracks:
            logger.info("%s ya está al día (0 nuevas).", platform.capitalize())
            return

        provider = self._providers.get(platform)
        if provider is None:
            return

        if apply_flag:
            applied_count = 0

            def on_batch(batch: list[Track]) -> None:
                nonlocal applied_count
                if not batch:
                    return
                self._repo.mark_synced(platform, [t.key for t in batch], when=now)
                applied_count += len(batch)

            try:
                applied = provider.apply_likes(
                    tracks, on_batch=on_batch, reorder=reorder
                )
                result.applied[platform] = len(applied)
            except PlatformOperationError:
                if applied_count:
                    result.applied[platform] = applied_count
                raise
            except Exception as exc:
                if provider.graceful_on_error:
                    logger.error(
                        "%s: fallo al aplicar likes: %s",
                        provider.name.capitalize(),
                        exc,
                    )
                    result.skipped_providers.append(provider.name)
                    if applied_count:
                        result.applied[platform] = applied_count
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
