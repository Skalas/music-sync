"""Spotify library provider (spotipy)."""

from __future__ import annotations

import logging
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

import spotipy
from requests.exceptions import RequestException
from spotipy.exceptions import SpotifyException
from spotipy.oauth2 import SpotifyOAuth
from tqdm import tqdm

from musicsync.domain.errors import PlatformOperationError
from musicsync.domain.track import Track, date_only, year_from_date
from musicsync.infrastructure._env import load_env_keys

logger = logging.getLogger(__name__)

SCOPE_READ = "user-library-read"
SCOPE_WRITE = "user-library-modify"
SPOTIFY_PAGE_LIMIT = 50
# PUT /me/library accepts at most 40 URIs (Feb 2026 library API; was 50 on /me/tracks).
SPOTIFY_ADD_BATCH = 40
MAX_RETRIES = 5
# Per-track search: stay under dev-mode rate limits and outwait 429 windows.
SPOTIFY_SEARCH_DELAY_SEC = 0.2
SPOTIFY_SEARCH_MAX_ATTEMPTS = 20


class SpotifyProvider:
    name = "spotify"
    can_write = True
    graceful_on_error = False

    def __init__(
        self,
        *,
        base_dir: Path,
        unmatched_log_path: Path,
        need_write: bool = False,
        client: spotipy.Spotify | None = None,
    ) -> None:
        self._base_dir = base_dir
        self._need_write = need_write
        self._client = client
        self._unmatched_log_path = unmatched_log_path

    def _get_client(self) -> spotipy.Spotify:
        if self._client is not None:
            return self._client
        config = self._load_config(self._need_write)
        cache_path = self._base_dir / ".cache"
        auth = SpotifyOAuth(
            client_id=config["SPOTIPY_CLIENT_ID"],
            client_secret=config["SPOTIPY_CLIENT_SECRET"],
            redirect_uri=config["SPOTIPY_REDIRECT_URI"],
            scope=config["scope"],
            cache_path=str(cache_path),
            open_browser=True,
        )
        self._client = spotipy.Spotify(auth_manager=auth, retries=0)
        return self._client

    def _load_config(self, need_write: bool) -> dict[str, str]:
        required = ["SPOTIPY_CLIENT_ID", "SPOTIPY_CLIENT_SECRET", "SPOTIPY_REDIRECT_URI"]
        config, missing = load_env_keys(self._base_dir, required)
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

    def read_liked(self) -> list[Track]:
        sp = self._get_client()
        first = _with_retries(
            sp.current_user_saved_tracks, limit=SPOTIFY_PAGE_LIMIT, offset=0
        )
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
                    album_data = track.get("album") or {}
                    images = album_data.get("images") or []
                    duration_ms = track.get("duration_ms")
                    tracks.append(
                        Track(
                            name=track["name"],
                            artist=", ".join(a["name"] for a in track.get("artists", [])),
                            platform_id=track["id"],
                            added_at=date_only(item.get("added_at")),
                            album=album_data.get("name") or None,
                            artwork_url=images[0].get("url") if images else None,
                            duration_sec=(
                                round(duration_ms / 1000) if duration_ms is not None else None
                            ),
                            year=year_from_date(album_data.get("release_date")),
                        )
                    )
                    bar.update(1)
                if not page.get("next"):
                    break
                offset += SPOTIFY_PAGE_LIMIT
                page = _with_retries(
                    sp.current_user_saved_tracks,
                    limit=SPOTIFY_PAGE_LIMIT,
                    offset=offset,
                )

        tracks.sort(key=lambda t: t.added_at or "")
        return tracks

    def apply_likes(
        self,
        tracks: list[Track],
        *,
        on_batch: Callable[[list[Track]], None] | None = None,
        reorder: bool = False,
    ) -> list[Track]:
        if not self._need_write:
            raise PlatformOperationError(
                "Spotify apply requiere el scope user-library-modify; "
                "el proveedor se construyó solo con lectura."
            )
        sp = self._get_client()
        applied: list[Track] = []

        for batch_start in range(0, len(tracks), SPOTIFY_ADD_BATCH):
            batch = tracks[batch_start : batch_start + SPOTIFY_ADD_BATCH]
            matched_ids: list[str] = []
            matched_tracks: list[Track] = []
            for track in tqdm(batch, desc="Match en Spotify", unit="cancion"):
                spotify_id = self._search_track_id(sp, track)
                if spotify_id:
                    matched_ids.append(spotify_id)
                    matched_tracks.append(track)

            if not matched_ids:
                continue

            try:
                _add_track_ids(sp, matched_ids)
            except SpotifyException as exc:
                if exc.http_status == 403:
                    raise PlatformOperationError(
                        "Spotify rechazó agregar likes (HTTP 403 — scope insuficiente). "
                        "Vuelve a conectar Spotify y acepta el permiso para modificar "
                        "tu biblioteca (user-library-modify), luego reintenta Apply."
                    ) from exc
                if exc.http_status == 400 and "too many uris" in str(exc).lower():
                    raise PlatformOperationError(
                        "Spotify rechazó el lote (HTTP 400 — demasiadas URIs). "
                        "Reintenta Apply; si persiste, reporta el bug."
                    ) from exc
                raise

            applied.extend(matched_tracks)
            if on_batch is not None:
                on_batch(matched_tracks)

        return applied

    def write_review(self, tracks: list[Track], path: Path) -> None:
        sp = self._get_client()
        candidates = self._match_on_spotify(sp, tracks)
        self._write_spotify_review(path, candidates)
        self._log_unmatched(candidates, self._unmatched_log_path)

    def _search_track_id(self, sp: spotipy.Spotify, track: Track) -> str | None:
        query = f"track:{track.name} artist:{track.artist}"
        for attempt in range(1, SPOTIFY_SEARCH_MAX_ATTEMPTS + 1):
            try:
                res = sp.search(q=query, type="track", limit=1)
                items = res.get("tracks", {}).get("items", [])
                if SPOTIFY_SEARCH_DELAY_SEC:
                    time.sleep(SPOTIFY_SEARCH_DELAY_SEC)
                if not items:
                    return None
                track_id = items[0].get("id")
                return str(track_id) if track_id else None
            except SpotifyException as exc:
                if exc.http_status == 429 and attempt < SPOTIFY_SEARCH_MAX_ATTEMPTS:
                    wait = _retry_after_seconds(exc, attempt)
                    logger.warning(
                        "Spotify search 429 para %r; esperando %.0fs (%d/%d)",
                        track.line,
                        wait,
                        attempt,
                        SPOTIFY_SEARCH_MAX_ATTEMPTS,
                    )
                    time.sleep(wait)
                    continue
                logger.warning(
                    "Spotify search falló para %r: %s",
                    track.line,
                    exc,
                )
                return None
            except RequestException as exc:
                if attempt < SPOTIFY_SEARCH_MAX_ATTEMPTS:
                    time.sleep(min(2**attempt, 30))
                    continue
                logger.warning(
                    "Spotify search red falló para %r: %s",
                    track.line,
                    exc,
                )
                return None
        return None

    def _match_on_spotify(
        self, sp: spotipy.Spotify, tracks: list[Track]
    ) -> list[dict]:
        candidates: list[dict] = []
        for track in tqdm(tracks, desc="Match en Spotify", unit="cancion"):
            spotify_id = self._search_track_id(sp, track)
            if not spotify_id:
                candidates.append({"track": track, "match": None})
                continue
            candidates.append(
                {
                    "track": track,
                    "match": {
                        "id": spotify_id,
                        "name": track.name,
                        "artist": track.artist,
                        "url": f"https://open.spotify.com/track/{spotify_id}",
                    },
                }
            )
        return candidates

    @staticmethod
    def _write_spotify_review(path: Path, candidates: list[dict]) -> None:
        lines = ["# Revisa estos matches antes de correr con --apply-spotify", ""]
        for cand in candidates:
            src = cand["track"]
            match = cand["match"]
            if match:
                lines.append(f"[OK]   {src.line}")
                lines.append(
                    f"        -> {match['name']} - {match['artist']}  {match['url']}"
                )
            else:
                lines.append(f"[MISS] {src.line}  (sin match en Spotify)")
            lines.append("")
        path.write_text("\n".join(lines), encoding="utf-8")

    @staticmethod
    def _log_unmatched(candidates: list[dict], path: Path) -> None:
        misses = [c["track"].line for c in candidates if not c["match"]]
        if misses:
            path.write_text(
                "Canciones sin match en Spotify:\n" + "\n".join(misses) + "\n",
                encoding="utf-8",
            )


_T = TypeVar("_T")


def _retry_after_seconds(exc: SpotifyException, attempt: int) -> float:
    if exc.headers and exc.headers.get("Retry-After") is not None:
        return float(exc.headers["Retry-After"])
    return float(min(2**attempt, 60))


def _add_track_ids(sp: spotipy.Spotify, track_ids: list[str]) -> None:
    _with_retries(sp.current_user_saved_tracks_add, tracks=track_ids)


def _with_retries(fn: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return fn(*args, **kwargs)
        except SpotifyException as exc:
            if exc.http_status == 429 and attempt < MAX_RETRIES:
                time.sleep(_retry_after_seconds(exc, attempt))
                continue
            raise
        except RequestException:
            if attempt < MAX_RETRIES:
                time.sleep(2**attempt)
                continue
            raise
    raise RuntimeError("agotados los reintentos de red")  # pragma: no cover
