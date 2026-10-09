"""Playlist mirroring: read selected playlists → upsert → union → report / guarded apply.

Additive only. A default or dry run makes zero remote writes; a platform is
written only when it advertises ``can_playlist_write`` AND the caller opted in
for it (``apply_platforms``). Any per-platform failure skips that platform only.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from musicsync.domain._time import utc_now_iso
from musicsync.domain.playlist import (
    Playlist,
    PlaylistDiff,
    PlaylistSummary,
    compute_playlist_union,
    normalize_playlist_name,
)
from musicsync.domain.ports import PlaylistProvider, PlaylistRepository
from musicsync.domain.track import Track

logger = logging.getLogger(__name__)

REVIEW_HEADER = (
    "# Playlists: canciones que faltan por plataforma.\n"
    "# PENDIENTE = corre con --apply-<plataforma> para agregarlas.\n"
    "# SIN MATCH = la plataforma no pudo resolverlas (Apple: no están en tu biblioteca).\n"
    "# SIN ARTISTA = sin artista en ninguna plataforma; no se agregan (riesgo de otra canción)."
)
_PENDING_LABEL = "PENDIENTE"
_UNRESOLVED_LABEL = "SIN MATCH"
_TITLE_ONLY_LABEL = "SIN ARTISTA"


@dataclass
class PlaylistSyncOptions:
    names: tuple[str, ...] = ()
    apply_platforms: frozenset[str] = frozenset()
    dry_run: bool = False
    skip_unavailable_providers: bool = False
    """Also tolerate SystemExit (missing .env) from a provider — web only."""


@dataclass
class PlaylistListing:
    playlists: dict[str, list[PlaylistSummary]] = field(default_factory=dict)
    skipped: dict[str, str] = field(default_factory=dict)
    """{platform: one-line reason} for platforms that could not be listed."""


@dataclass
class PlaylistSyncResult:
    diffs: dict[str, PlaylistDiff] = field(default_factory=dict)
    applied: dict[str, dict[str, int]] = field(default_factory=dict)
    """{playlist display name: {platform: tracks added}}."""
    unresolved: dict[str, dict[str, list[Track]]] = field(default_factory=dict)
    skipped: dict[str, str] = field(default_factory=dict)
    missing_names: list[str] = field(default_factory=list)
    ambiguous: dict[str, list[str]] = field(default_factory=dict)
    """{name_key: [platforms]} where several playlists share the name; those
    platforms are neither source nor target for that playlist this run."""


def _one_line(exc: BaseException) -> str:
    text = str(exc).strip()
    return text.splitlines()[0] if text else type(exc).__name__


class PlaylistSyncService:
    def __init__(
        self,
        repo: PlaylistRepository,
        providers: Iterable[PlaylistProvider],
        *,
        review_path: Path | None = None,
    ) -> None:
        self._repo = repo
        self._providers = list(providers)
        self._review_path = review_path

    # -- listing -----------------------------------------------------------

    def list_playlists(self, *, skip_unavailable: bool = False) -> PlaylistListing:
        listing = PlaylistListing()
        for provider in self._readers():
            try:
                listing.playlists[provider.name] = [
                    summary for summary in provider.list_playlists() if summary.owned
                ]
            except SystemExit as exc:
                if not skip_unavailable:
                    raise
                self._skip(provider.name, exc, listing.skipped)
            except Exception as exc:
                self._skip(provider.name, exc, listing.skipped)
        return listing

    # -- mirror ------------------------------------------------------------

    def run(self, options: PlaylistSyncOptions) -> PlaylistSyncResult:
        """Mirror from THIS run's reads only; the DB records them but never feeds writes."""
        result = PlaylistSyncResult()
        wanted = {normalize_playlist_name(n) for n in options.names if n.strip()}
        if not wanted:
            return result

        fresh: dict[str, list[Playlist]] = {}
        for provider in self._readers():
            playlists = self._read_fresh(provider, wanted, options, result)
            if playlists is not None:
                fresh[provider.name] = playlists

        found = {pl.key for pls in fresh.values() for pl in pls}
        result.missing_names = sorted(wanted - found - set(result.ambiguous))
        for name_key in result.missing_names:
            logger.warning("Playlist «%s»: no se encontró en ninguna plataforma.", name_key)

        result.diffs = compute_playlist_union(
            fresh,
            self._synced_keys_for_fresh_targets(wanted, fresh),
            targets=[p.name for p in self._writers() if p.name in fresh],
        )
        self._drop_ambiguous_targets(result)
        self._log_diffs(result.diffs)

        if options.dry_run:
            logger.info("(dry-run) No se escribe nada. Diff de playlists calculado arriba.")
            return result

        pending = self._apply_all(fresh, options, result)
        title_only = {d.name: d.unresolved for d in result.diffs.values() if d.unresolved}
        self._write_review(pending, result.unresolved, title_only)
        return result

    # -- internals ---------------------------------------------------------

    def _readers(self) -> list[PlaylistProvider]:
        return [p for p in self._providers if p.can_playlist_read]

    def _writers(self) -> list[PlaylistProvider]:
        return [p for p in self._providers if p.can_playlist_write]

    @staticmethod
    def _skip(platform: str, exc: BaseException, skipped: dict[str, str]) -> None:
        reason = _one_line(exc)
        logger.error("%s: omitiendo playlists: %s", platform.capitalize(), reason)
        skipped[platform] = reason

    def _read_fresh(
        self,
        provider: PlaylistProvider,
        wanted: set[str],
        options: PlaylistSyncOptions,
        result: PlaylistSyncResult,
    ) -> list[Playlist] | None:
        """Read *provider*'s wanted playlists; None → platform skipped this run."""
        try:
            summaries = self._unambiguous(provider, provider.list_playlists(), wanted, result)
            playlists = [provider.read_playlist(summary) for summary in summaries]
        except SystemExit as exc:
            if not options.skip_unavailable_providers:
                raise
            self._skip(provider.name, exc, result.skipped)
            return None
        except Exception as exc:
            self._skip(provider.name, exc, result.skipped)
            return None
        for playlist in playlists:
            self._repo.upsert_playlist(playlist)
            logger.info(
                "  %s «%s»: %d canción(es)", provider.name, playlist.name, len(playlist.tracks)
            )
        return playlists

    @staticmethod
    def _unambiguous(
        provider: PlaylistProvider,
        summaries: list[PlaylistSummary],
        wanted: set[str],
        result: PlaylistSyncResult,
    ) -> list[PlaylistSummary]:
        """Wanted playlists that are the platform's ONLY one with that name, and owned.

        Several same-named playlists, or a followed/others' one (which we may not
        write and must not shadow with a new copy), make the name ambiguous there,
        as does a system playlist name the platform reserves.
        """
        platform = provider.name
        reserved = wanted & provider.reserved_playlist_names
        for key in sorted(reserved):
            logger.warning(
                "Playlist «%s»: es un nombre reservado del sistema en %s; se omite %s para ella.",
                key,
                platform,
                platform,
            )
            result.ambiguous.setdefault(key, []).append(platform)
        by_key: dict[str, list[PlaylistSummary]] = {}
        for summary in summaries:
            if summary.key in wanted - reserved:
                by_key.setdefault(summary.key, []).append(summary)
        unique: list[PlaylistSummary] = []
        for key, group in by_key.items():
            if len(group) == 1 and group[0].owned:
                unique.append(group[0])
                continue
            logger.warning(
                "Playlist «%s»: %d playlist(s) con ese nombre en %s (o no es tuya); "
                "se omite %s para ella.",
                group[0].name,
                len(group),
                platform,
                platform,
            )
            result.ambiguous.setdefault(key, []).append(platform)
        return unique

    def _synced_keys_for_fresh_targets(
        self, wanted: set[str], fresh: dict[str, list[Playlist]]
    ) -> dict[str, dict[str, set[str]]]:
        """Mirror state only for target playlists seen this run (a new one starts clean)."""
        seen = {(platform, pl.key) for platform, pls in fresh.items() for pl in pls}
        return {
            name_key: {p: keys for p, keys in by_platform.items() if (p, name_key) in seen}
            for name_key, by_platform in self._repo.get_playlist_synced_keys(wanted).items()
        }

    @staticmethod
    def _drop_ambiguous_targets(result: PlaylistSyncResult) -> None:
        for name_key, platforms in result.ambiguous.items():
            diff = result.diffs.get(name_key)
            if diff is None:
                continue
            for platform in platforms:
                diff.to_add.pop(platform, None)

    @staticmethod
    def _log_diffs(diffs: dict[str, PlaylistDiff]) -> None:
        for diff in diffs.values():
            for platform, tracks in diff.to_add.items():
                logger.info(
                    "  Playlist «%s»: faltan en %s: %d", diff.name, platform, len(tracks)
                )
            if diff.unresolved:
                logger.warning(
                    "  Playlist «%s»: %d canción(es) sin artista; van a revisión.",
                    diff.name,
                    len(diff.unresolved),
                )

    def _apply_all(
        self,
        fresh: dict[str, list[Playlist]],
        options: PlaylistSyncOptions,
        result: PlaylistSyncResult,
    ) -> dict[str, dict[str, list[Track]]]:
        """Write opted-in targets; return what stays pending for the review file."""
        remote_ids = {
            (platform, pl.key): pl.remote_id
            for platform, pls in fresh.items()
            for pl in pls
        }
        pending: dict[str, dict[str, list[Track]]] = {}
        for provider in self._writers():
            if provider.name in result.skipped:
                continue
            for name_key, diff in result.diffs.items():
                tracks = diff.to_add.get(provider.name, [])
                if not tracks:
                    continue
                if provider.name not in options.apply_platforms:
                    pending.setdefault(diff.name, {})[provider.name] = tracks
                    continue
                remote_id = remote_ids.get((provider.name, name_key))
                if not self._apply_one(provider, diff.name, remote_id, tracks, result):
                    break
        return pending

    def _apply_one(
        self,
        provider: PlaylistProvider,
        name: str,
        remote_id: str | None,
        tracks: list[Track],
        result: PlaylistSyncResult,
    ) -> bool:
        """Mirror one playlist into one platform. False → platform skipped."""
        try:
            outcome = provider.add_to_playlist(name, remote_id, tracks)
        except Exception as exc:
            self._skip(provider.name, exc, result.skipped)
            return False
        self._repo.mark_playlist_synced(
            provider.name, name, outcome.remote_id, outcome.added, when=utc_now_iso()
        )
        result.applied.setdefault(name, {})[provider.name] = len(outcome.added)
        if outcome.unresolved:
            result.unresolved.setdefault(name, {})[provider.name] = outcome.unresolved
        logger.info(
            "%s «%s»: %d agregada(s), %d sin match.",
            provider.name.capitalize(),
            name,
            len(outcome.added),
            len(outcome.unresolved),
        )
        return True

    def _write_review(
        self,
        pending: dict[str, dict[str, list[Track]]],
        unresolved: dict[str, dict[str, list[Track]]],
        title_only: dict[str, list[Track]],
    ) -> None:
        if self._review_path is None or not (pending or unresolved or title_only):
            return
        lines = [REVIEW_HEADER, ""]
        lines += _review_section(_PENDING_LABEL, pending)
        lines += _review_section(_UNRESOLVED_LABEL, unresolved)
        lines += _title_only_section(title_only)
        self._review_path.write_text("\n".join(lines), encoding="utf-8")
        logger.info("-> Revisión de playlists escrita en %s.", self._review_path.name)


def _review_section(
    label: str, tracks_by_playlist: dict[str, dict[str, list[Track]]]
) -> list[str]:
    lines: list[str] = []
    for name, by_platform in tracks_by_playlist.items():
        for platform, tracks in by_platform.items():
            lines.append(f"## [{label}] «{name}» -> {platform} ({len(tracks)})")
            lines += [f"  {track.line}" for track in tracks]
            lines.append("")
    return lines


def _title_only_section(tracks_by_playlist: dict[str, list[Track]]) -> list[str]:
    lines: list[str] = []
    for name, tracks in tracks_by_playlist.items():
        lines.append(f"## [{_TITLE_ONLY_LABEL}] «{name}» ({len(tracks)})")
        lines += [f"  {track.line}" for track in tracks]
        lines.append("")
    return lines
