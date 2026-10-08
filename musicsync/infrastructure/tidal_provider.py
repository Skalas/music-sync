"""Tidal library provider (official developer.tidal.com OAuth2 + PKCE)."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import secrets
import tempfile
import time
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, quote, urlencode, urlparse

import requests
from requests.exceptions import RequestException
from tqdm import tqdm

from musicsync.domain.track import Track, date_only, year_from_date
from musicsync.domain.union import sort_tracks_chronologically
from musicsync.infrastructure._env import load_env_keys

logger = logging.getLogger(__name__)

API_BASE = "https://openapi.tidal.com/v2"
AUTH_URL = "https://login.tidal.com/authorize"
TOKEN_URL = "https://auth.tidal.com/v1/oauth2/token"
SCOPE_READ = "collection.read"
SCOPE_WRITE = "collection.write"
ADD_BATCH = 20
MAX_RETRIES = 3


@dataclass
class _ApiContext:
    """Mutable bearer token for long runs (refresh on 401 mid-loop)."""

    token: str
    need_write: bool


class TidalError(Exception):
    """Tidal API or auth failure — caught by sync orchestration for graceful skip."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class TidalProvider:
    name = "tidal"
    can_write = True
    graceful_on_error = True

    def __init__(
        self,
        *,
        base_dir: Path,
        need_write: bool = False,
        session: requests.Session | None = None,
    ) -> None:
        self._base_dir = base_dir
        self._need_write = need_write
        self._session = session or requests.Session()
        self._cache_path = base_dir / ".tidal-cache"
        self._config = self._load_config()

    def _load_config(self) -> dict[str, str]:
        keys = ["TIDAL_CLIENT_ID", "TIDAL_REDIRECT_URI"]
        config, missing = load_env_keys(self._base_dir, keys)
        if missing:
            raise TidalError(
                "faltan credenciales Tidal en .env: "
                + ", ".join(missing)
                + " (copia .env.example)"
            )
        return config

    def read_liked(self) -> list[Track]:
        ctx = _ApiContext(
            token=self._ensure_token(need_write=False),
            need_write=False,
        )
        tracks: list[Track] = []
        cursor: str | None = None
        artist_cache: dict[str, str] = {}

        while True:
            params: dict[str, str] = {
                "include": "items,artists",
                "countryCode": "US",
            }
            if cursor:
                params["page[cursor]"] = cursor

            data = self._api_get(
                "/userCollectionTracks/me/relationships/items",
                ctx,
                params=params,
            )
            included = _index_included(data.get("included", []))
            for item in data.get("data", []):
                track = _parse_collection_item(item, included)
                if track is not None:
                    track = self._enrich_track_artist(
                        ctx, item, included, track, artist_cache
                    )
                    tracks.append(track)

            cursor = _next_cursor(data.get("links", {}))
            if not cursor:
                break

        tracks.sort(key=lambda t: t.added_at or "")
        return tracks

    def write_review(self, tracks: list[Track], path: Path) -> None:
        """No-op: Tidal does not have a review-file flow."""

    def apply_likes(
        self,
        tracks: list[Track],
        *,
        on_batch: Callable[[list[Track]], None] | None = None,
        reorder: bool = False,
    ) -> list[Track]:
        tracks = sort_tracks_chronologically(tracks)
        if reorder:
            return self._reorder_likes(tracks, on_batch=on_batch)

        ctx = _ApiContext(
            token=self._ensure_token(need_write=True),
            need_write=True,
        )
        applied: list[Track] = []

        for batch_start in range(0, len(tracks), ADD_BATCH):
            self._maybe_refresh_ctx(ctx)
            batch = tracks[batch_start : batch_start + ADD_BATCH]
            matched: list[tuple[Track, str]] = []
            for track in tqdm(batch, desc="Match en Tidal", unit="cancion"):
                tidal_id = self._search_track_id(track, ctx)
                if tidal_id:
                    matched.append((track, tidal_id))

            if matched:
                applied.extend(self._add_favorites(ctx, matched, on_batch=on_batch))

        return applied

    def _reorder_likes(
        self,
        tracks: list[Track],
        *,
        on_batch: Callable[[list[Track]], None] | None = None,
    ) -> list[Track]:
        """Remove then re-add favorites in chronological order (oldest first)."""
        ctx = _ApiContext(
            token=self._ensure_token(need_write=True),
            need_write=True,
        )
        matched: list[tuple[Track, str]] = []
        for track in tqdm(tracks, desc="Match en Tidal", unit="cancion"):
            tidal_id = self._resolve_tidal_id(track, ctx)
            if tidal_id:
                matched.append((track, tidal_id))

        skipped = len(tracks) - len(matched)
        if skipped:
            logger.warning(
                "Tidal reorder: %d canción(es) sin match en catálogo; se omiten",
                skipped,
            )
        if not matched:
            return []

        tidal_ids = [tid for _, tid in matched]
        logger.info(
            "Tidal reorder: quitando %d favorito(s) antes de reinsertar en orden",
            len(tidal_ids),
        )
        self._remove_favorites(ctx, tidal_ids)

        applied: list[Track] = []
        for batch_start in range(0, len(matched), ADD_BATCH):
            self._maybe_refresh_ctx(ctx)
            batch = matched[batch_start : batch_start + ADD_BATCH]
            batch_applied = self._add_favorites(ctx, batch, on_batch=on_batch)
            applied.extend(batch_applied)

        return applied

    def _resolve_tidal_id(self, track: Track, ctx: _ApiContext) -> str | None:
        if track.platform_id:
            return track.platform_id
        return self._search_track_id(track, ctx)

    def _enrich_track_artist(
        self,
        ctx: _ApiContext,
        item: dict[str, Any],
        included: dict[str, dict[str, Any]],
        track: Track,
        artist_cache: dict[str, str],
    ) -> Track:
        if track.artist.strip():
            return track
        artist_id = _first_artist_id(_collection_resource(item, included))
        if not artist_id:
            return track
        name = self._resolve_artist_name(ctx, artist_id, artist_cache)
        if not name:
            return track
        return Track(
            name=track.name,
            artist=name,
            platform_id=track.platform_id,
            added_at=track.added_at,
            album=track.album,
            artwork_url=track.artwork_url,
            duration_sec=track.duration_sec,
            year=track.year,
        )

    def _resolve_artist_name(
        self,
        ctx: _ApiContext,
        artist_id: str,
        cache: dict[str, str],
    ) -> str:
        if artist_id in cache:
            return cache[artist_id]
        try:
            data = self._api_get(
                f"/artists/{artist_id}",
                ctx,
                params={"countryCode": "US"},
            )
        except TidalError:
            cache[artist_id] = ""
            return ""
        attrs = (data.get("data") or {}).get("attributes") or {}
        name = str(attrs.get("name") or "")
        cache[artist_id] = name
        return name

    def _remove_favorites(self, ctx: _ApiContext, tidal_ids: list[str]) -> None:
        path = "/userCollectionTracks/me/relationships/items"
        params = {"countryCode": "US"}
        for batch_start in range(0, len(tidal_ids), ADD_BATCH):
            self._maybe_refresh_ctx(ctx)
            batch_ids = tidal_ids[batch_start : batch_start + ADD_BATCH]
            payload_items = [{"type": "tracks", "id": tid} for tid in batch_ids]
            try:
                self._api_delete(
                    path,
                    ctx,
                    json_body={"data": payload_items},
                    params=params,
                )
            except TidalError as exc:
                if exc.status_code in (404, 409):
                    self._remove_favorites_one_by_one(ctx, batch_ids, params=params)
                    continue
                raise

    def _remove_favorites_one_by_one(
        self,
        ctx: _ApiContext,
        tidal_ids: list[str],
        *,
        params: dict[str, str],
    ) -> None:
        path = "/userCollectionTracks/me/relationships/items"
        for tidal_id in tidal_ids:
            try:
                self._api_delete(
                    path,
                    ctx,
                    json_body={"data": [{"type": "tracks", "id": tidal_id}]},
                    params=params,
                )
            except TidalError as exc:
                if exc.status_code in (404, 409):
                    continue
                raise

    def _add_favorites(
        self,
        ctx: _ApiContext,
        matched: list[tuple[Track, str]],
        *,
        on_batch: Callable[[list[Track]], None] | None = None,
    ) -> list[Track]:
        """POST favorites; on batch 409 (duplicate), retry track-by-track."""
        path = "/userCollectionTracks/me/relationships/items"
        params = {"countryCode": "US"}
        payload_items = [{"type": "tracks", "id": tidal_id} for _, tidal_id in matched]

        try:
            self._api_post(
                path,
                ctx,
                json_body={"data": payload_items},
                params=params,
            )
        except TidalError as exc:
            if exc.status_code != 409:
                raise
            logger.info(
                "Tidal: lote rechazado (409, ya en favoritos); reintentando canción a canción"
            )
            return self._add_favorites_one_by_one(ctx, matched, on_batch=on_batch)

        tracks = [track for track, _ in matched]
        if on_batch is not None:
            on_batch(tracks)
        return tracks

    def _add_favorites_one_by_one(
        self,
        ctx: _ApiContext,
        matched: list[tuple[Track, str]],
        *,
        on_batch: Callable[[list[Track]], None] | None = None,
    ) -> list[Track]:
        path = "/userCollectionTracks/me/relationships/items"
        params = {"countryCode": "US"}
        applied: list[Track] = []

        for track, tidal_id in matched:
            try:
                self._api_post(
                    path,
                    ctx,
                    json_body={"data": [{"type": "tracks", "id": tidal_id}]},
                    params=params,
                )
            except TidalError as exc:
                if exc.status_code != 409:
                    raise
            applied.append(track)

        if applied and on_batch is not None:
            on_batch(applied)
        return applied

    def _search_track_id(self, track: Track, ctx: _ApiContext) -> str | None:
        query = f"{track.name} {track.artist}".strip()
        encoded = quote(query, safe="")
        try:
            data = self._api_get(
                f"/searchResults/{encoded}/relationships/tracks",
                ctx,
                params={"countryCode": "US", "page[limit]": "1"},
            )
        except TidalError:
            return None

        for item in data.get("data", []):
            if item.get("type") == "tracks" and item.get("id"):
                return str(item["id"])
        return None

    def _maybe_refresh_ctx(self, ctx: _ApiContext) -> None:
        cached = self._read_cache()
        if cached and _token_expired(cached):
            ctx.token = self._refresh_access_token(need_write=ctx.need_write)

    def _ensure_token(self, *, need_write: bool) -> str:
        cached = self._read_cache()
        if cached and not _token_expired(cached):
            return str(cached["access_token"])

        if cached and cached.get("refresh_token"):
            try:
                refreshed = self._refresh_token(str(cached["refresh_token"]))
                self._write_cache(refreshed)
                return str(refreshed["access_token"])
            except TidalError:
                pass

        scopes = SCOPE_READ if not need_write else f"{SCOPE_READ} {SCOPE_WRITE}"
        tokens = self._authorize_pkce(scopes)
        self._write_cache(tokens)
        return str(tokens["access_token"])

    def _refresh_access_token(self, *, need_write: bool) -> str:
        """Refresh from cache when possible; fall back to full OAuth."""
        cached = self._read_cache()
        if cached and cached.get("refresh_token"):
            try:
                refreshed = self._refresh_token(str(cached["refresh_token"]))
                self._write_cache(refreshed)
                return str(refreshed["access_token"])
            except TidalError:
                pass
        return self._ensure_token(need_write=need_write)

    def _authorize_pkce(self, scopes: str) -> dict[str, Any]:
        verifier = secrets.token_urlsafe(64)
        challenge = _pkce_challenge(verifier)
        state = secrets.token_urlsafe(16)
        redirect_uri = self._config["TIDAL_REDIRECT_URI"]

        params = {
            "response_type": "code",
            "client_id": self._config["TIDAL_CLIENT_ID"],
            "redirect_uri": redirect_uri,
            "scope": scopes,
            "code_challenge_method": "S256",
            "code_challenge": challenge,
            "state": state,
        }
        url = f"{AUTH_URL}?{urlencode(params)}"
        print("Abre el navegador para autorizar Tidal...")
        webbrowser.open(url)

        code, returned_state = _wait_for_redirect(redirect_uri)
        if returned_state != state:
            raise TidalError("state OAuth no coincide (posible CSRF)")

        return self._exchange_code(code, verifier)

    def _exchange_code(self, code: str, verifier: str) -> dict[str, Any]:
        data = {
            "grant_type": "authorization_code",
            "client_id": self._config["TIDAL_CLIENT_ID"],
            "code": code,
            "redirect_uri": self._config["TIDAL_REDIRECT_URI"],
            "code_verifier": verifier,
        }
        return self._token_request(data)

    def _refresh_token(self, refresh_token: str) -> dict[str, Any]:
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": self._config["TIDAL_CLIENT_ID"],
        }
        return self._token_request(data)

    def _token_request(self, data: dict[str, str]) -> dict[str, Any]:
        try:
            resp = self._session.post(
                TOKEN_URL,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=30,
            )
        except RequestException as exc:
            raise TidalError(f"token endpoint inalcanzable: {exc}") from exc

        if resp.status_code >= 400:
            raise TidalError(f"token HTTP {resp.status_code}")

        payload: dict[str, Any] = resp.json()
        payload["obtained_at"] = _now_epoch()
        return payload

    def _api_get(
        self,
        path: str,
        ctx: _ApiContext,
        *,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = self._session.get(
                    f"{API_BASE}{path}",
                    headers=_api_headers(ctx.token),
                    params=params,
                    timeout=30,
                )
            except RequestException as exc:
                if attempt == MAX_RETRIES:
                    raise TidalError(f"GET {path} falló: {exc}") from exc
                time.sleep(2**attempt)
                continue

            if resp.status_code == 401 and attempt < MAX_RETRIES:
                ctx.token = self._refresh_access_token(need_write=ctx.need_write)
                continue
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", min(2**attempt, 60)))
                time.sleep(wait)
                if attempt < MAX_RETRIES:
                    continue
            if resp.status_code >= 400:
                raise _http_error("GET", path, resp.status_code)

            body: dict[str, Any] = resp.json()
            return body

        raise TidalError(f"GET {path} agotados los reintentos")

    def _api_post(
        self,
        path: str,
        ctx: _ApiContext,
        *,
        json_body: dict[str, Any],
        params: dict[str, str] | None = None,
    ) -> None:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = self._session.post(
                    f"{API_BASE}{path}",
                    headers=_api_headers(ctx.token),
                    json=json_body,
                    params=params,
                    timeout=30,
                )
            except RequestException as exc:
                if attempt == MAX_RETRIES:
                    raise TidalError(f"POST {path} falló: {exc}") from exc
                time.sleep(2**attempt)
                continue

            if resp.status_code == 401 and attempt < MAX_RETRIES:
                ctx.token = self._refresh_access_token(need_write=ctx.need_write)
                continue
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", min(2**attempt, 60)))
                time.sleep(wait)
                if attempt < MAX_RETRIES:
                    continue
            if resp.status_code >= 400:
                raise _http_error("POST", path, resp.status_code)

            return

        raise TidalError(f"POST {path} agotados los reintentos")

    def _api_delete(
        self,
        path: str,
        ctx: _ApiContext,
        *,
        json_body: dict[str, Any],
        params: dict[str, str] | None = None,
    ) -> None:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = self._session.delete(
                    f"{API_BASE}{path}",
                    headers=_api_headers(ctx.token),
                    json=json_body,
                    params=params,
                    timeout=30,
                )
            except RequestException as exc:
                if attempt == MAX_RETRIES:
                    raise TidalError(f"DELETE {path} falló: {exc}") from exc
                time.sleep(2**attempt)
                continue

            if resp.status_code == 401 and attempt < MAX_RETRIES:
                ctx.token = self._refresh_access_token(need_write=ctx.need_write)
                continue
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", min(2**attempt, 60)))
                time.sleep(wait)
                if attempt < MAX_RETRIES:
                    continue
            if resp.status_code >= 400:
                raise _http_error("DELETE", path, resp.status_code)

            return

        raise TidalError(f"DELETE {path} agotados los reintentos")

    def _read_cache(self) -> dict[str, Any] | None:
        if not self._cache_path.exists():
            return None
        return cast(dict[str, Any], json.loads(self._cache_path.read_text(encoding="utf-8")))

    def _write_cache(self, data: dict[str, Any]) -> None:
        fd, tmp_name = tempfile.mkstemp(dir=str(self._cache_path.parent))
        tmp_path = Path(tmp_name)
        try:
            os.chmod(tmp_path, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(data, indent=2))
            os.replace(tmp_path, self._cache_path)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise


def _api_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.api+json",
        "Content-Type": "application/vnd.api+json",
    }


_HTTP_STATUS_HINTS: dict[int, str] = {
    401: "token inválido o expirado; reconecta Tidal en la web",
    403: "sin permiso de escritura en favoritos",
    404: "no está en favoritos de Tidal",
    409: "ya está en favoritos de Tidal",
    429: "límite de peticiones; reintenta más tarde",
}


def _http_error(method: str, path: str, status: int) -> TidalError:
    hint = _HTTP_STATUS_HINTS.get(status)
    message = f"{method} {path} HTTP {status}"
    if hint:
        message = f"{message} — {hint}"
    return TidalError(message, status_code=status)


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _token_expired(cached: dict[str, Any]) -> bool:
    obtained = float(cached.get("obtained_at", 0))
    expires_in = int(cached.get("expires_in", 0))
    return _now_epoch() >= obtained + max(expires_in - 60, 0)


def _now_epoch() -> float:
    return time.time()


def _next_cursor(links: dict[str, Any]) -> str | None:
    nxt = links.get("next")
    if not nxt:
        return None
    parsed = urlparse(str(nxt))
    qs = parse_qs(parsed.query)
    cursors = qs.get("page[cursor]", [])
    return cursors[0] if cursors else None


def _index_included(included: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for item in included:
        key = f"{item.get('type')}:{item.get('id')}"
        index[key] = item
    return index


def _parse_iso8601_duration(value: str) -> int | None:
    """Parse ISO-8601 duration like PT3M20S to seconds. Returns None if unparseable
    or if no time components are present (e.g. bare 'PT')."""
    m = re.fullmatch(
        r"P(?:(\d+)D)?T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?",
        value.strip(),
    )
    if not m:
        return None
    # If no group captured any digits, the input had no meaningful duration.
    if not any([m.group(1), m.group(2), m.group(3), m.group(4)]):
        return None
    days = int(m.group(1) or 0)
    hours = int(m.group(2) or 0)
    minutes = int(m.group(3) or 0)
    seconds = float(m.group(4) or 0)
    return days * 86400 + hours * 3600 + minutes * 60 + round(seconds)


def _album_metadata(
    resource: dict[str, Any], included: dict[str, dict[str, Any]]
) -> tuple[str | None, str | None, str | None]:
    """Return (album_name, year, artwork_url) from the track's album relationship."""
    rel = resource.get("relationships", {}).get("albums", {})
    for ref in rel.get("data", []):
        album = included.get(f"{ref.get('type')}:{ref.get('id')}")
        if album is None:
            continue
        attrs = album.get("attributes", {})
        name = attrs.get("title") or attrs.get("name") or None
        year = year_from_date(attrs.get("releaseDate"))
        image_links = attrs.get("imageLinks") or []
        artwork_url = image_links[0].get("href") if image_links else None
        # Also try cover attribute (some endpoints return it differently)
        if not artwork_url:
            cover = attrs.get("cover") or ""
            artwork_url = cover if cover.startswith("https://") else None
        return name, year, artwork_url
    return None, None, None


def _collection_resource(
    item: dict[str, Any], included: dict[str, dict[str, Any]]
) -> dict[str, Any] | None:
    ref_type = item.get("type")
    ref_id = item.get("id")
    if not ref_type or not ref_id:
        return None
    resource = included.get(f"{ref_type}:{ref_id}")
    if resource is None and ref_type == "tracks":
        return item
    return resource


def _first_artist_id(resource: dict[str, Any] | None) -> str | None:
    if resource is None:
        return None
    rel = resource.get("relationships", {}).get("artists", {})
    for ref in rel.get("data", []):
        artist_id = ref.get("id")
        if artist_id:
            return str(artist_id)
    return None


def _parse_collection_item(
    item: dict[str, Any], included: dict[str, dict[str, Any]]
) -> Track | None:
    resource = _collection_resource(item, included)
    if resource is None:
        return None

    ref_id = item.get("id")
    attrs = resource.get("attributes", {})
    title = attrs.get("title") or attrs.get("name") or ""
    artist = _artist_name(resource, included)
    added_at = date_only((item.get("meta") or {}).get("addedAt"))

    if not title:
        return None

    # Duration: may be integer seconds or ISO-8601 string
    raw_duration = attrs.get("duration")
    duration_sec: int | None = None
    if isinstance(raw_duration, (int, float)):
        duration_sec = round(raw_duration)
    elif isinstance(raw_duration, str):
        duration_sec = _parse_iso8601_duration(raw_duration)

    album_name, year, artwork_url = _album_metadata(resource, included)

    return Track(
        name=title,
        artist=artist,
        platform_id=str(ref_id),
        added_at=added_at,
        album=album_name,
        artwork_url=artwork_url,
        duration_sec=duration_sec,
        year=year,
    )


def _artist_name(
    resource: dict[str, Any], included: dict[str, dict[str, Any]]
) -> str:
    rel = resource.get("relationships", {}).get("artists", {})
    for ref in rel.get("data", []):
        artist = included.get(f"{ref.get('type')}:{ref.get('id')}")
        if artist:
            name = artist.get("attributes", {}).get("name")
            if name:
                return str(name)
    return ""


def _make_redirect_handler(
    holder: dict[str, str | None],
) -> type[BaseHTTPRequestHandler]:
    class _RedirectHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            qs = parse_qs(urlparse(self.path).query)
            holder["code"] = qs.get("code", [None])[0]
            holder["state"] = qs.get("state", [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                b"<html><body>Autorizado. Puedes cerrar esta ventana.</body></html>"
            )

        def log_message(self, format: str, *args: Any) -> None:
            return

    return _RedirectHandler


def _wait_for_redirect(redirect_uri: str) -> tuple[str, str | None]:
    parsed = urlparse(redirect_uri)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if port in (80, 443):
        port = 8080

    holder: dict[str, str | None] = {"code": None, "state": None}
    server = HTTPServer(("127.0.0.1", port), _make_redirect_handler(holder))
    server.timeout = 120
    try:
        server.handle_request()
        if not holder["code"]:
            # handle_request returns on timeout with nothing captured — free the
            # port instead of leaving an abandoned flow holding :8080.
            raise TidalError("OAuth flow timed out or was abandoned")
    finally:
        server.server_close()

    return holder["code"], holder["state"]
