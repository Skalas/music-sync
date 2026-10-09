"""Spotify library provider (spotipy)."""

from __future__ import annotations

import json
import logging
import sys
import time
from collections.abc import Callable
from enum import IntEnum
from pathlib import Path
from typing import Any, TypeVar

import spotipy
from requests.exceptions import RequestException
from spotipy.exceptions import SpotifyException
from spotipy.oauth2 import SpotifyOAuth
from tqdm import tqdm

from musicsync.domain.errors import PlatformOperationError
from musicsync.domain.playlist import Playlist, PlaylistAddResult, PlaylistSummary
from musicsync.domain.track import Track, date_only, year_from_date
from musicsync.infrastructure._env import load_env_keys

logger = logging.getLogger(__name__)

SCOPE_READ = "user-library-read"
SCOPE_WRITE = "user-library-modify"
SCOPE_PLAYLIST_READ = "playlist-read-private"
# GET /me/playlists omits collaborative playlists without this scope.
SCOPE_PLAYLIST_READ_COLLABORATIVE = "playlist-read-collaborative"
SCOPE_PLAYLIST_WRITE = "playlist-modify-private playlist-modify-public"
TOKEN_CACHE_NAME = ".cache"
SPOTIFY_PLAYLIST_ADD_BATCH = 100
SPOTIFY_PAGE_LIMIT = 50
# PUT /me/library accepts at most 40 URIs (Feb 2026 library API; was 50 on /me/tracks).
SPOTIFY_ADD_BATCH = 40
MAX_RETRIES = 5
# Per-track search: stay under dev-mode rate limits and outwait 429 windows.
SPOTIFY_SEARCH_DELAY_SEC = 0.2
SPOTIFY_SEARCH_MAX_ATTEMPTS = 20
AUTH_REQUIRED_MESSAGE = (
    "Spotify: falta autorización (token ausente, expirado o sin el permiso pedido); "
    "reconecta Spotify en Conexiones o corre la CLI una vez."
)


class PlaylistScope(IntEnum):
    """Playlist OAuth scope requested on top of the liked-songs scopes."""

    NONE = 0
    READ = 1
    WRITE = 2


def playlist_scopes(scope: PlaylistScope) -> list[str]:
    if scope is PlaylistScope.NONE:
        return []
    read = [SCOPE_PLAYLIST_READ, SCOPE_PLAYLIST_READ_COLLABORATIVE]
    if scope is PlaylistScope.READ:
        return read
    return [*read, *SCOPE_PLAYLIST_WRITE.split()]


def cached_token_scopes(base_dir: Path) -> set[str]:
    """Scopes granted to the cached Spotify token (empty when absent/unreadable)."""
    try:
        cached = json.loads((base_dir / TOKEN_CACHE_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    scope = cached.get("scope") if isinstance(cached, dict) else None
    return set(str(scope).split()) if scope else set()


def requested_scopes(needed: list[str], granted: set[str]) -> str:
    """*needed* plus every scope already granted, so a run never narrows the grant.

    spotipy rewrites the cached token's scope with the requested one on refresh
    and re-prompts when requested is not a subset of cached; requesting the union
    keeps liked-songs and playlist runs from invalidating each other's token.
    Only scopes the user already granted are added — never a new write scope.
    """
    extra = sorted(granted - set(needed))
    return " ".join([*needed, *extra])


class _NonInteractiveSpotifyOAuth(SpotifyOAuth):
    """Never prompts or opens a browser: an unusable token raises instead (web)."""

    def get_auth_response(self, open_browser: bool | None = None) -> str:
        raise PlatformOperationError(AUTH_REQUIRED_MESSAGE)


class SpotifyProvider:
    name = "spotify"
    can_write = True
    graceful_on_error = False
    can_playlist_read = True
    can_playlist_write = True
    reserved_playlist_names: frozenset[str] = frozenset()

    def __init__(
        self,
        *,
        base_dir: Path,
        unmatched_log_path: Path,
        need_write: bool = False,
        playlist_scope: PlaylistScope = PlaylistScope.NONE,
        interactive: bool = True,
        client: spotipy.Spotify | None = None,
    ) -> None:
        self._base_dir = base_dir
        self._need_write = need_write
        self._playlist_scope = playlist_scope
        self._interactive = interactive
        self._client = client
        self._unmatched_log_path = unmatched_log_path

    def _get_client(self) -> spotipy.Spotify:
        if self._client is not None:
            return self._client
        config = self._load_config(self._need_write)
        cache_path = self._base_dir / TOKEN_CACHE_NAME
        oauth_class = SpotifyOAuth if self._interactive else _NonInteractiveSpotifyOAuth
        auth = oauth_class(
            client_id=config["SPOTIPY_CLIENT_ID"],
            client_secret=config["SPOTIPY_CLIENT_SECRET"],
            redirect_uri=config["SPOTIPY_REDIRECT_URI"],
            scope=config["scope"],
            cache_path=str(cache_path),
            open_browser=self._interactive,
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
        scopes = [SCOPE_READ, *([SCOPE_WRITE] if need_write else [])]
        config["scope"] = requested_scopes(
            scopes + playlist_scopes(self._playlist_scope),
            cached_token_scopes(self._base_dir),
        )
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
                            isrc=(track.get("external_ids") or {}).get("isrc") or None,
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

    # -- playlists -----------------------------------------------------------

    def list_playlists(self) -> list[PlaylistSummary]:
        """Every playlist in the library; ``owned=False`` marks followed/others' ones.

        The user's own playlists count whether or not they are collaborative.
        Non-owned ones are returned only so a same-named followed playlist is
        reported as ambiguous instead of a duplicate being created.
        """
        sp = self._get_client()
        user_id = str(_with_retries(sp.current_user).get("id") or "")
        return [
            PlaylistSummary(
                platform=self.name,
                name=str(item.get("name") or ""),
                remote_id=str(item["id"]),
                track_count=_playlist_total(item),
                owned=_is_own_playlist(item, user_id),
            )
            for item in _paged(sp.current_user_playlists)
            if item.get("id")
        ]

    def read_playlist(self, summary: PlaylistSummary) -> Playlist:
        sp = self._get_client()
        tracks: list[Track] = []
        for entry in _paged(
            sp.playlist_items, summary.remote_id, additional_types=("track",)
        ):
            track = _playlist_entry_track(entry)
            if track is not None:
                tracks.append(track)
        return Playlist(
            platform=self.name,
            name=summary.name,
            remote_id=summary.remote_id,
            tracks=tuple(tracks),
        )

    def add_to_playlist(
        self, name: str, remote_id: str | None, tracks: list[Track]
    ) -> PlaylistAddResult:
        """Resolve each track by Spotify search (never a foreign id) and append it."""
        if self._playlist_scope is not PlaylistScope.WRITE:
            raise PlatformOperationError(
                "Spotify: escribir playlists requiere --apply-spotify "
                "(scope playlist-modify)."
            )
        sp = self._get_client()
        existing = self._playlist_track_ids(sp, remote_id)
        to_post: list[str] = []
        added: list[Track] = []
        unresolved: list[Track] = []
        for track in tqdm(tracks, desc=f"Match en Spotify ({name})", unit="cancion"):
            # No artist → "track:X artist:" can match a different song: never search.
            spotify_id = self._search_track_id(sp, track) if track.artist.strip() else None
            if spotify_id is None:
                unresolved.append(track)
                continue
            if spotify_id not in existing:
                existing.add(spotify_id)
                to_post.append(spotify_id)
            added.append(track)
        if to_post:
            remote_id = remote_id or self._create_playlist(sp, name)
            self._post_playlist_items(sp, remote_id, to_post)
        return PlaylistAddResult(remote_id=remote_id, added=added, unresolved=unresolved)

    def _playlist_track_ids(self, sp: spotipy.Spotify, remote_id: str | None) -> set[str]:
        if remote_id is None:
            return set()
        ids: set[str] = set()
        for entry in _paged(sp.playlist_items, remote_id, additional_types=("track",)):
            track = _playlist_entry_track(entry)
            if track is not None and track.platform_id:
                ids.add(track.platform_id)
        return ids

    @staticmethod
    def _create_playlist(sp: spotipy.Spotify, name: str) -> str:
        created = _with_playlist_errors(
            sp.current_user_playlist_create, name, public=False
        )
        return str(created["id"])

    @staticmethod
    def _post_playlist_items(
        sp: spotipy.Spotify, remote_id: str, track_ids: list[str]
    ) -> None:
        for start in range(0, len(track_ids), SPOTIFY_PLAYLIST_ADD_BATCH):
            batch = track_ids[start : start + SPOTIFY_PLAYLIST_ADD_BATCH]
            _with_playlist_errors(sp.playlist_add_items, remote_id, batch)

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


def _paged(fn: Callable[..., dict[str, Any]], *args: Any, **kwargs: Any) -> list[dict]:
    """Every item of an offset-paged Spotify listing."""
    items: list[dict] = []
    offset = 0
    while True:
        page = _with_retries(fn, *args, limit=SPOTIFY_PAGE_LIMIT, offset=offset, **kwargs)
        batch = page.get("items") or []
        items.extend(item for item in batch if item)
        if not page.get("next") or not batch:
            return items
        offset += SPOTIFY_PAGE_LIMIT


def _is_own_playlist(item: dict[str, Any], user_id: str) -> bool:
    owner = (item.get("owner") or {}).get("id")
    return bool(user_id) and owner == user_id


def _playlist_total(item: dict[str, Any]) -> int:
    # Feb 2026 Web API renamed the playlist's "tracks" object to "items".
    ref = item.get("items") or item.get("tracks") or {}
    return int(ref.get("total") or 0) if isinstance(ref, dict) else 0


def _playlist_entry_track(entry: dict[str, Any]) -> Track | None:
    # Feb 2026 Web API renamed the entry's "track" field to "item".
    data = entry.get("item") or entry.get("track")
    if not data or data.get("type", "track") != "track" or not data.get("name"):
        return None
    return Track(
        name=data["name"],
        artist=", ".join(a["name"] for a in data.get("artists", []) if a.get("name")),
        platform_id=data.get("id") or None,
    )


def _with_playlist_errors(fn: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
    try:
        return _with_retries(fn, *args, **kwargs)
    except SpotifyException as exc:
        if exc.http_status == 403:
            raise PlatformOperationError(
                "Spotify rechazó escribir la playlist (HTTP 403 — scope insuficiente). "
                "Vuelve a autorizar con --apply-spotify y acepta playlist-modify."
            ) from exc
        raise


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
