"""Spotify library provider (spotipy)."""

from __future__ import annotations

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

from musicsync.domain.track import Track
from musicsync.infrastructure._env import load_env_keys

SCOPE_READ = "user-library-read"
SCOPE_WRITE = "user-library-modify"
SPOTIFY_PAGE_LIMIT = 50
SPOTIFY_ADD_BATCH = 50
MAX_RETRIES = 3


class SpotifyProvider:
    name = "spotify"
    can_write = True
    graceful_on_apply_error = False

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
                    tracks.append(
                        Track(
                            name=track["name"],
                            artist=", ".join(a["name"] for a in track["artists"]),
                            platform_id=track["id"],
                            added_at=item.get("added_at"),
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

    def apply_likes(self, tracks: list[Track]) -> list[Track]:
        sp = self._get_client()
        candidates = self._match_on_spotify(sp, tracks)
        matched = [c for c in candidates if c["match"]]
        ids = [c["match"]["id"] for c in matched]
        for start in range(0, len(ids), SPOTIFY_ADD_BATCH):
            batch = ids[start : start + SPOTIFY_ADD_BATCH]
            _with_retries(sp.current_user_saved_tracks_add, tracks=batch)
        return [c["track"] for c in matched]

    def write_review(self, tracks: list[Track], path: Path) -> None:
        sp = self._get_client()
        candidates = self._match_on_spotify(sp, tracks)
        self._write_spotify_review(path, candidates)
        self._log_unmatched(candidates, self._unmatched_log_path)

    def _match_on_spotify(
        self, sp: spotipy.Spotify, tracks: list[Track]
    ) -> list[dict]:
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


def _with_retries(fn: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
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
