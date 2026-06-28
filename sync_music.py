#!/usr/bin/env python3
"""Conciliador bidireccional de "Me Gusta" entre Spotify y Apple Music.

Regla de union: una cancion que este en Liked Songs en cualquiera de las dos
plataformas debe quedar marcada en ambas. La conciliacion es incremental
(recuerda lo ya procesado en state.json) y local (sin servidores externos).

La escritura en Spotify es opt-in: por defecto solo se genera un archivo de
revision; las altas reales requieren --apply-spotify.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import spotipy
from dotenv import load_dotenv
from requests.exceptions import RequestException
from spotipy.exceptions import SpotifyException
from spotipy.oauth2 import SpotifyOAuth
from tqdm import tqdm
from unidecode import unidecode

# --- Rutas y constantes -----------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
APPLESCRIPT_DIR = BASE_DIR / "applescript"
CACHE_PATH = BASE_DIR / ".cache"
STATE_PATH = BASE_DIR / "state.json"
TO_APPLE_PATH = BASE_DIR / "canciones_to_apple.txt"
TO_SPOTIFY_REVIEW_PATH = BASE_DIR / "to_spotify_review.txt"
UNMATCHED_LOG_PATH = BASE_DIR / "unmatched.log"

SHORTCUT_NAME = "SyncToAppleMusic"
SCOPE_READ = "user-library-read"
SCOPE_WRITE = "user-library-modify"

SPOTIFY_PAGE_LIMIT = 50
SPOTIFY_ADD_BATCH = 50
MAX_RETRIES = 3
KEY_SEP = "␟"  # separador no imprimible para la clave de match

_PAREN_RE = re.compile(r"[\(\[].*?[\)\]]")
_NOISE_RE = re.compile(
    r"\b(feat\.?|featuring|remaster(ed)?|remix|deluxe|version|edit|mono|stereo)\b.*",
    re.IGNORECASE,
)
_NONALNUM_RE = re.compile(r"[^a-z0-9 ]+")
_WS_RE = re.compile(r"\s+")


# --- Modelo -----------------------------------------------------------------


@dataclass
class Track:
    name: str
    artist: str
    key: str = field(init=False)
    spotify_id: str | None = None
    added_at: str | None = None

    def __post_init__(self) -> None:
        self.key = normalize_key(self.name, self.artist)

    @property
    def line(self) -> str:
        """Linea de salida en el formato exacto 'Nombre - Artista'."""
        name = self.name.replace("\n", " ").replace("\r", " ").strip()
        artist = self.artist.replace("\n", " ").replace("\r", " ").strip()
        return f"{name} - {artist}"


# --- Utilidades de normalizacion -------------------------------------------


def _normalize_field(value: str) -> str:
    value = unidecode(value or "").lower()
    value = _PAREN_RE.sub(" ", value)
    value = _NOISE_RE.sub(" ", value)
    value = _NONALNUM_RE.sub(" ", value)
    return _WS_RE.sub(" ", value).strip()


def normalize_key(name: str, artist: str) -> str:
    """Clave heuristica para emparejar canciones entre servicios."""
    primary_artist = re.split(r"[,&;/]| feat", artist or "", maxsplit=1)[0]
    return f"{_normalize_field(name)}{KEY_SEP}{_normalize_field(primary_artist)}"


# --- Configuracion y cliente Spotify ---------------------------------------


def load_config(need_write: bool) -> dict[str, str]:
    load_dotenv(BASE_DIR / ".env")
    required = ["SPOTIPY_CLIENT_ID", "SPOTIPY_CLIENT_SECRET", "SPOTIPY_REDIRECT_URI"]
    config = {key: os.environ.get(key, "").strip() for key in required}
    missing = [key for key, value in config.items() if not value]
    if missing:
        sys.exit(
            "ERROR: faltan credenciales en .env: "
            + ", ".join(missing)
            + "\nCopia .env.example a .env y rellena los valores."
        )
    if "localhost" in config["SPOTIPY_REDIRECT_URI"]:
        sys.exit(
            "ERROR: el Redirect URI usa 'localhost', que Spotify ya no acepta.\n"
            "Usa la IP de loopback: http://127.0.0.1:8080"
        )
    config["scope"] = f"{SCOPE_READ} {SCOPE_WRITE}" if need_write else SCOPE_READ
    return config


def get_spotify_client(config: dict[str, str]) -> spotipy.Spotify:
    auth = SpotifyOAuth(
        client_id=config["SPOTIPY_CLIENT_ID"],
        client_secret=config["SPOTIPY_CLIENT_SECRET"],
        redirect_uri=config["SPOTIPY_REDIRECT_URI"],
        scope=config["scope"],
        cache_path=str(CACHE_PATH),
        open_browser=True,
    )
    return spotipy.Spotify(auth_manager=auth, retries=0)


def _with_retries(fn, *args, **kwargs):
    """Ejecuta una llamada de red con backoff exponencial y respeto a 429."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return fn(*args, **kwargs)
        except SpotifyException as exc:
            if exc.http_status == 429:
                wait = int(exc.headers.get("Retry-After", 2)) if exc.headers else 2
            elif attempt < MAX_RETRIES:
                wait = 2**attempt
            else:
                raise
            time.sleep(wait)
        except RequestException:
            if attempt == MAX_RETRIES:
                raise
            time.sleep(2**attempt)
    raise RuntimeError("agotados los reintentos de red")


# --- Lectura de fuentes -----------------------------------------------------


def fetch_spotify_liked(sp: spotipy.Spotify) -> list[Track]:
    """Pagina el 100% de Liked Songs, ordenadas por fecha de like (added_at)."""
    first = _with_retries(sp.current_user_saved_tracks, limit=SPOTIFY_PAGE_LIMIT, offset=0)
    total = first.get("total", 0)
    tracks: list[Track] = []

    with tqdm(total=total, desc="Spotify Liked", unit="cancion") as bar:
        page = first
        offset = 0
        while True:
            items = page.get("items", [])
            if not items:
                break
            for item in items:
                track = item.get("track")
                if not track or not track.get("id"):
                    continue
                tracks.append(
                    Track(
                        name=track["name"],
                        artist=", ".join(a["name"] for a in track["artists"]),
                        spotify_id=track["id"],
                        added_at=item.get("added_at"),
                    )
                )
                bar.update(1)
            if not page.get("next"):
                break
            offset += SPOTIFY_PAGE_LIMIT
            page = _with_retries(
                sp.current_user_saved_tracks, limit=SPOTIFY_PAGE_LIMIT, offset=offset
            )

    tracks.sort(key=lambda t: t.added_at or "")
    return tracks


def read_apple_loved() -> list[Track]:
    """Lee las favoritas de Apple Music via AppleScript."""
    script = APPLESCRIPT_DIR / "read_loved.applescript"
    try:
        result = subprocess.run(
            ["osascript", str(script)],
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError:
        sys.exit("ERROR: 'osascript' no esta disponible (¿estas en macOS?).")
    except subprocess.CalledProcessError as exc:
        sys.exit(f"ERROR leyendo Apple Music:\n{exc.stderr.strip()}")

    tracks: list[Track] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line or " - " not in line:
            continue
        name, _, artist = line.rpartition(" - ")
        tracks.append(Track(name=name.strip(), artist=artist.strip()))
    return tracks


# --- Conciliacion -----------------------------------------------------------


def reconcile(
    spotify: list[Track],
    apple: list[Track],
    state: dict[str, list[str]],
) -> tuple[list[Track], list[Track]]:
    """Calcula que falta empujar a cada lado, ignorando lo ya conciliado."""
    spotify_keys = {t.key for t in spotify}
    apple_keys = {t.key for t in apple}
    done_apple = set(state.get("apple", []))
    done_spotify = set(state.get("spotify", []))

    to_apple = [
        t for t in spotify if t.key not in apple_keys and t.key not in done_apple
    ]
    to_spotify = [
        t for t in apple if t.key not in spotify_keys and t.key not in done_spotify
    ]
    return to_apple, to_spotify


# --- Direccion Spotify -> Apple ---------------------------------------------


def write_export(path: Path, tracks: list[Track]) -> None:
    path.write_text("\n".join(t.line for t in tracks) + "\n", encoding="utf-8")


def trigger_shortcut(input_path: Path) -> None:
    print(f"-> Ejecutando atajo '{SHORTCUT_NAME}'...")
    try:
        subprocess.run(
            ["shortcuts", "run", SHORTCUT_NAME, "-i", str(input_path)],
            check=True,
        )
    except subprocess.CalledProcessError:
        sys.exit(
            f"ERROR: fallo el atajo '{SHORTCUT_NAME}'. "
            "Verifica que exista en Atajos.app (ver README)."
        )


def mark_loved(input_path: Path) -> None:
    print("-> Marcando como Favorita/Love en Apple Music...")
    script = APPLESCRIPT_DIR / "mark_loved.applescript"
    result = subprocess.run(
        ["osascript", str(script), str(input_path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"   aviso: no se pudo marcar Love ({result.stderr.strip()})")
    else:
        print(f"   {result.stdout.strip()}")


# --- Direccion Apple -> Spotify ---------------------------------------------


def match_on_spotify(sp: spotipy.Spotify, tracks: list[Track]) -> list[dict]:
    """Busca cada cancion de Apple en Spotify y devuelve candidatos."""
    candidates: list[dict] = []
    for track in tqdm(tracks, desc="Match en Spotify", unit="cancion"):
        query = f"track:{track.name} artist:{track.artist}"
        try:
            res = _with_retries(sp.search, q=query, type="track", limit=1)
        except (SpotifyException, RequestException):
            res = {}
        items = res.get("tracks", {}).get("items", [])
        if not items:
            candidates.append({"track": track, "match": None})
            continue
        found = items[0]
        candidates.append(
            {
                "track": track,
                "match": {
                    "id": found["id"],
                    "name": found["name"],
                    "artist": ", ".join(a["name"] for a in found["artists"]),
                    "url": found["external_urls"]["spotify"],
                },
            }
        )
    return candidates


def write_spotify_review(path: Path, candidates: list[dict]) -> None:
    lines = ["# Revisa estos matches antes de correr con --apply-spotify", ""]
    for cand in candidates:
        src = cand["track"]
        match = cand["match"]
        if match:
            lines.append(f"[OK]   {src.line}")
            lines.append(f"        -> {match['name']} - {match['artist']}  {match['url']}")
        else:
            lines.append(f"[MISS] {src.line}  (sin match en Spotify)")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def apply_spotify_likes(sp: spotipy.Spotify, candidates: list[dict]) -> int:
    ids = [c["match"]["id"] for c in candidates if c["match"]]
    for start in range(0, len(ids), SPOTIFY_ADD_BATCH):
        batch = ids[start : start + SPOTIFY_ADD_BATCH]
        _with_retries(sp.current_user_saved_tracks_add, tracks=batch)
    return len(ids)


# --- Estado y logs ----------------------------------------------------------


def load_state() -> dict[str, list[str]]:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"apple": [], "spotify": []}


def save_state(state: dict[str, list[str]]) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def log_unmatched(to_spotify_candidates: list[dict]) -> None:
    misses = [c["track"].line for c in to_spotify_candidates if not c["match"]]
    if misses:
        UNMATCHED_LOG_PATH.write_text(
            "Canciones de Apple sin match en Spotify:\n" + "\n".join(misses) + "\n",
            encoding="utf-8",
        )


# --- CLI / orquestacion -----------------------------------------------------


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--apply-spotify", action="store_true",
                   help="aplica realmente las altas en Spotify Liked (scope de escritura)")
    p.add_argument("--full", action="store_true",
                   help="ignora state.json y reconcilia todo desde cero")
    p.add_argument("--no-apple", action="store_true", help="no empujar a Apple Music")
    p.add_argument("--no-spotify", action="store_true", help="no procesar la direccion hacia Spotify")
    p.add_argument("--dry-run", action="store_true",
                   help="no escribe en ningun servicio; solo reporta el diff")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(need_write=args.apply_spotify)
    sp = get_spotify_client(config)

    print("Leyendo fuentes...")
    spotify_tracks = fetch_spotify_liked(sp)
    apple_tracks = read_apple_loved()
    print(f"  Spotify Liked: {len(spotify_tracks)} | Apple favoritas: {len(apple_tracks)}")

    state = {"apple": [], "spotify": []} if args.full else load_state()
    to_apple, to_spotify = reconcile(spotify_tracks, apple_tracks, state)
    print(f"  Faltan en Apple: {len(to_apple)} | Faltan en Spotify: {len(to_spotify)}")

    if args.dry_run:
        print("\n(dry-run) No se escribe nada. Diff calculado arriba.")
        return

    # --- Spotify -> Apple ---
    if to_apple and not args.no_apple:
        write_export(TO_APPLE_PATH, to_apple)
        print(f"\nExportadas {len(to_apple)} canciones a {TO_APPLE_PATH.name}")
        trigger_shortcut(TO_APPLE_PATH)
        mark_loved(TO_APPLE_PATH)
        state["apple"] = sorted(set(state.get("apple", [])) | {t.key for t in to_apple})
    elif not to_apple:
        print("\nApple Music ya esta al dia (0 nuevas).")

    # --- Apple -> Spotify ---
    if to_spotify and not args.no_spotify:
        print()
        candidates = match_on_spotify(sp, to_spotify)
        write_spotify_review(TO_SPOTIFY_REVIEW_PATH, candidates)
        log_unmatched(candidates)
        matched = [c for c in candidates if c["match"]]
        if args.apply_spotify:
            n = apply_spotify_likes(sp, candidates)
            print(f"-> {n} canciones agregadas a Spotify Liked.")
            state["spotify"] = sorted(
                set(state.get("spotify", [])) | {c["track"].key for c in matched}
            )
        else:
            print(
                f"-> {len(matched)} candidatos escritos en {TO_SPOTIFY_REVIEW_PATH.name}.\n"
                "   Revisalos y corre de nuevo con --apply-spotify para aplicarlos."
            )
    elif not to_spotify:
        print("Spotify ya esta al dia (0 nuevas).")

    save_state(state)
    print("\nListo.")


if __name__ == "__main__":
    main()
