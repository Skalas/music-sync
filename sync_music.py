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
import sys
from pathlib import Path

from musicsync.application.csv_export import export_csv
from musicsync.application.sync_service import SyncOptions, SyncService
from musicsync.domain.ports import LibraryProvider
from musicsync.infrastructure.apple_provider import AppleProvider
from musicsync.infrastructure.spotify_provider import SpotifyProvider
from musicsync.infrastructure.sqlite_repository import DatabaseError, SqliteTrackRepository
from musicsync.infrastructure.tidal_provider import TidalProvider

BASE_DIR = Path(__file__).resolve().parent
APPLESCRIPT_DIR = BASE_DIR / "applescript"
STATE_PATH = BASE_DIR / "state.json"
DEFAULT_DB = BASE_DIR / "library.db"


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
        help="aplica realmente las altas en Apple Music (Atajo + Love)",
    )
    p.add_argument(
        "--apply-tidal",
        action="store_true",
        help="aplica realmente las altas en Tidal Favorites (scope de escritura)",
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
    return p.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = parse_args()

    try:
        repo = SqliteTrackRepository(
            args.db,
            require_exists=args.offline or args.export is not None,
        )
    except DatabaseError as exc:
        sys.exit(str(exc))

    if args.export is not None:
        n = export_csv(repo, args.export)
        repo.close()
        print(f"Exportadas {n} filas a {args.export}")
        return

    providers: list[LibraryProvider] = []
    if not args.offline:
        if not args.no_spotify:
            providers.append(
                SpotifyProvider(
                    base_dir=BASE_DIR,
                    need_write=args.apply_spotify,
                )
            )
        if not args.no_apple:
            providers.append(AppleProvider(applescript_dir=APPLESCRIPT_DIR))
        if not args.no_tidal:
            providers.append(
                TidalProvider(
                    base_dir=BASE_DIR,
                    need_write=args.apply_tidal,
                )
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
    )

    service = SyncService(repo, providers, state_json_path=STATE_PATH)

    if not args.offline:
        print("Leyendo fuentes...")

    service.run(options)
    repo.close()
    print("\nListo.")


if __name__ == "__main__":
    main()
