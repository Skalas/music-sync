#!/usr/bin/env python3
"""Conciliador N-way de "Me Gusta" entre Spotify, Apple Music y Tidal.

Regla de unión: una canción que esté en Liked/Favorites en cualquier plataforma
conectada debe quedar marcada en todas. La conciliación es incremental (SQLite)
y local (sin servidores externos).

Las escrituras remotas son opt-in por plataforma (--apply-spotify, --apply-apple, --apply-tidal).
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path

from musicsync.application.apple_links import resolve_apple_links
from musicsync.application.csv_export import export_csv
from musicsync.application.output_paths import (
    APPLESCRIPT_DIR,
    BASE_DIR,
    DEFAULT_DB,
    STATE_PATH,
    TO_APPLE_PATH,
    UNMATCHED_LOG_PATH,
)
from musicsync.application.sync_service import SyncOptions, SyncService
from musicsync.domain.errors import PlatformOperationError
from musicsync.domain.ports import LibraryProvider
from musicsync.infrastructure.apple_music_api import build_apple_catalog_client
from musicsync.infrastructure.providers import build_providers
from musicsync.infrastructure.sqlite_repository import DatabaseError, SqliteTrackRepository

# Tope por defecto del paso post-sync: la primera corrida sobre una biblioteca grande
# no se vuelve un rastreo largo; --limit lo sobrescribe y --resolve-apple-links no lo usa.
POST_SYNC_APPLE_LINK_LIMIT = 200

# Fallos del resolvedor que nunca deben tumbar un sync que ya terminó bien.
_APPLE_LINK_ERRORS = (PlatformOperationError, sqlite3.Error, DatabaseError)


def _positive_int(raw: str) -> int:
    value = int(raw)
    if value <= 0:
        raise argparse.ArgumentTypeError("debe ser un entero positivo")
    return value


def run_apple_link_resolution(
    repo: SqliteTrackRepository, *, limit: int | None, required: bool
) -> None:
    """Batch step: resolve Apple catalog ids. Missing creds → one-line notice, no crash.

    *required* (explicit --resolve-apple-links) turns missing creds or a failed
    batch into a non-zero exit; after a normal sync they are only reported.
    """
    try:
        _resolve_apple_links(repo, limit=limit, required=required)
    except _APPLE_LINK_ERRORS as exc:
        first_line = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        message = f"Apple Music: links omitidos: {first_line}"
        if required:
            sys.exit(message)
        print(message)


def _resolve_apple_links(
    repo: SqliteTrackRepository, *, limit: int | None, required: bool
) -> None:
    client, reason = build_apple_catalog_client(BASE_DIR)
    if client is None:
        message = f"Links de Apple Music desactivados: {reason}"
        if required:
            sys.exit(message)
        print(message)
        return
    print(f"Resolviendo links de Apple Music (storefront {client.storefront})...")
    report = resolve_apple_links(repo, client, limit=limit)
    print(
        f"Apple Music: {report.resolved} resueltas, {report.unresolved} sin match exacto."
    )
    if report.error and required:
        sys.exit(f"Apple Music: resolución detenida: {report.error}")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--apply-spotify",
        action="store_true",
        help="aplica realmente las altas en Spotify Liked (scope de escritura)",
    )
    p.add_argument(
        "--apply-apple",
        action="store_true",
        help="aplica realmente las altas en Apple Music (Love; Atajo solo si falta importar)",
    )
    p.add_argument(
        "--apply-tidal",
        action="store_true",
        help="aplica realmente las altas en Tidal Favorites (scope de escritura)",
    )
    p.add_argument(
        "--tidal-reorder",
        action="store_true",
        help="con --apply-tidal: quita y vuelve a añadir TODO el catálogo en orden cronológico",
    )
    p.add_argument(
        "--full",
        action="store_true",
        help="ignora synced_at y reconcilia todo desde cero",
    )
    p.add_argument("--no-apple", action="store_true", help="no empujar a Apple Music")
    p.add_argument(
        "--no-spotify",
        action="store_true",
        help="no procesar la dirección hacia Spotify",
    )
    p.add_argument("--no-tidal", action="store_true", help="omitir Tidal por completo")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="no escribe en ningún servicio; solo reporta el diff",
    )
    p.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB,
        help="ruta al SQLite library (default: library.db)",
    )
    p.add_argument(
        "--export",
        type=Path,
        metavar="PATH.csv",
        help="exporta la biblioteca a CSV y termina",
    )
    p.add_argument(
        "--offline",
        action="store_true",
        help="sin red; opera solo sobre la base de datos",
    )
    p.add_argument(
        "--resolve-apple-links",
        action="store_true",
        help="solo resuelve ids del catálogo de Apple Music (links exactos) y termina",
    )
    p.add_argument(
        "--limit",
        type=_positive_int,
        metavar="N",
        help="máximo de pistas a resolver en el catálogo de Apple Music por corrida",
    )
    args = p.parse_args()

    if args.offline and args.resolve_apple_links:
        p.error("--offline es incompatible con --resolve-apple-links: requiere red.")

    # --resolve-apple-links solo resuelve links y termina: cualquier flag de sync
    # se ignoraria en silencio, asi que se rechaza explicitamente.
    if args.resolve_apple_links:
        sync_flags = [
            flag
            for flag, enabled in (
                ("--apply-spotify", args.apply_spotify),
                ("--apply-apple", args.apply_apple),
                ("--apply-tidal", args.apply_tidal),
                ("--dry-run", args.dry_run),
                ("--full", args.full),
                ("--tidal-reorder", args.tidal_reorder),
                ("--export", args.export is not None),
            )
            if enabled
        ]
        if sync_flags:
            p.error(
                f"--resolve-apple-links es incompatible con {', '.join(sync_flags)}: "
                "solo resuelve links de Apple Music y no sincroniza. "
                "Córrelo aparte, sin flags de sync."
            )

    # --offline construye cero proveedores, asi que un --apply-* no tendria a
    # quien aplicar y terminaria en un no-op silencioso con exit 0.
    if args.offline:
        conflicting = [
            flag
            for flag, enabled in (
                ("--apply-spotify", args.apply_spotify),
                ("--apply-apple", args.apply_apple),
                ("--apply-tidal", args.apply_tidal),
            )
            if enabled
        ]
        if conflicting:
            p.error(
                f"--offline es incompatible con {', '.join(conflicting)}: "
                "sin red no hay proveedor al que aplicar. "
                "Usa --dry-run para ver el diff, o quita --offline para aplicar."
            )
    return args


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = parse_args()

    try:
        repo = SqliteTrackRepository(
            args.db,
            require_exists=(
                args.offline or args.export is not None or args.resolve_apple_links
            ),
        )
    except DatabaseError as exc:
        sys.exit(str(exc))

    if args.resolve_apple_links:
        try:
            run_apple_link_resolution(repo, limit=args.limit, required=True)
        finally:
            repo.close()
        return

    if args.export is not None:
        n = export_csv(repo, args.export)
        repo.close()
        print(f"Exportadas {n} filas a {args.export}")
        return

    providers: list[LibraryProvider] = []
    if not args.offline:
        providers = build_providers(
            BASE_DIR,
            applescript_dir=APPLESCRIPT_DIR,
            output_path=TO_APPLE_PATH,
            unmatched_log_path=UNMATCHED_LOG_PATH,
            need_write_spotify=args.apply_spotify,
            need_write_tidal=args.apply_tidal,
            include_spotify=not args.no_spotify,
            include_apple=not args.no_apple,
            include_tidal=not args.no_tidal,
        )

    options = SyncOptions(
        apply_spotify=args.apply_spotify,
        apply_apple=args.apply_apple,
        apply_tidal=args.apply_tidal,
        no_apple=args.no_apple,
        no_spotify=args.no_spotify,
        no_tidal=args.no_tidal,
        dry_run=args.dry_run,
        full=args.full,
        offline=args.offline,
        tidal_reorder=args.tidal_reorder and args.apply_tidal,
    )

    service = SyncService(repo, providers, state_json_path=STATE_PATH)

    if not args.offline:
        print("Leyendo fuentes...")

    try:
        service.run(options)
        if not args.offline and not args.dry_run:
            post_sync_limit = (
                args.limit if args.limit is not None else POST_SYNC_APPLE_LINK_LIMIT
            )
            run_apple_link_resolution(repo, limit=post_sync_limit, required=False)
    except PlatformOperationError as exc:
        sys.exit(str(exc))
    finally:
        repo.close()
    print("\nListo.")


if __name__ == "__main__":
    main()
