"""Tidal library provider (official developer.tidal.com OAuth2 + PKCE)."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, quote, urlencode, urlparse

import requests
from dotenv import load_dotenv
from requests.exceptions import RequestException
from tqdm import tqdm

from musicsync.domain.track import Track

logger = logging.getLogger(__name__)

API_BASE = "https://openapi.tidal.com/v2"
AUTH_URL = "https://login.tidal.com/authorize"
TOKEN_URL = "https://auth.tidal.com/v1/oauth2/token"
SCOPE_READ = "collection.read"
SCOPE_WRITE = "collection.write"
ADD_BATCH = 20


class TidalError(Exception):
    """Tidal API or auth failure — caught by sync orchestration for graceful skip."""


class TidalProvider:
    name = "tidal"
    can_write = True

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
        load_dotenv(self._base_dir / ".env")
        keys = ["TIDAL_CLIENT_ID", "TIDAL_CLIENT_SECRET", "TIDAL_REDIRECT_URI"]
        config = {k: os.environ.get(k, "").strip() for k in keys}
        missing = [k for k, v in config.items() if not v]
        if missing:
            raise TidalError(
                "faltan credenciales Tidal en .env: "
                + ", ".join(missing)
                + " (copia .env.example)"
            )
        return config

    def read_liked(self) -> list[Track]:
        token = self._ensure_token(need_write=False)
        tracks: list[Track] = []
        cursor: str | None = None

        while True:
            params: dict[str, str] = {
                "include": "items",
                "countryCode": "US",
            }
            if cursor:
                params["page[cursor]"] = cursor

            data = self._api_get(
                "/userCollectionTracks/me/relationships/items",
                token,
                params=params,
            )
            included = _index_included(data.get("included", []))
            for item in data.get("data", []):
                track = _parse_collection_item(item, included)
                if track is not None:
                    tracks.append(track)

            cursor = _next_cursor(data.get("links", {}))
            if not cursor:
                break

        tracks.sort(key=lambda t: t.added_at or "")
        return tracks

    def apply_likes(self, tracks: list[Track]) -> list[Track]:
        token = self._ensure_token(need_write=True)
        applied: list[Track] = []

        for batch_start in range(0, len(tracks), ADD_BATCH):
            batch = tracks[batch_start : batch_start + ADD_BATCH]
            payload_items: list[dict[str, str]] = []
            for track in tqdm(batch, desc="Match en Tidal", unit="cancion"):
                tidal_id = self._search_track_id(track, token)
                if tidal_id:
                    payload_items.append({"type": "tracks", "id": tidal_id})
                    applied.append(track)

            if payload_items:
                self._api_post(
                    "/userCollectionTracks/me/relationships/items",
                    token,
                    json_body={"data": payload_items},
                    params={"countryCode": "US"},
                )

        return applied

    def _search_track_id(self, track: Track, token: str) -> str | None:
        query = f"{track.name} {track.artist}".strip()
        encoded = quote(query, safe="")
        try:
            data = self._api_get(
                f"/searchResults/{encoded}/relationships/tracks",
                token,
                params={"countryCode": "US", "page[limit]": "1"},
            )
        except TidalError:
            return None

        for item in data.get("data", []):
            if item.get("type") == "tracks" and item.get("id"):
                return str(item["id"])
        return None

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
        self, path: str, token: str, *, params: dict[str, str] | None = None
    ) -> dict[str, Any]:
        try:
            resp = self._session.get(
                f"{API_BASE}{path}",
                headers=_api_headers(token),
                params=params,
                timeout=30,
            )
        except RequestException as exc:
            raise TidalError(f"GET {path} falló: {exc}") from exc

        if resp.status_code >= 400:
            raise TidalError(f"GET {path} HTTP {resp.status_code}")

        body: dict[str, Any] = resp.json()
        return body

    def _api_post(
        self,
        path: str,
        token: str,
        *,
        json_body: dict[str, Any],
        params: dict[str, str] | None = None,
    ) -> None:
        try:
            resp = self._session.post(
                f"{API_BASE}{path}",
                headers=_api_headers(token),
                json=json_body,
                params=params,
                timeout=30,
            )
        except RequestException as exc:
            raise TidalError(f"POST {path} falló: {exc}") from exc

        if resp.status_code >= 400:
            raise TidalError(f"POST {path} HTTP {resp.status_code}")

    def _read_cache(self) -> dict[str, Any] | None:
        if not self._cache_path.exists():
            return None
        return cast(dict[str, Any], json.loads(self._cache_path.read_text(encoding="utf-8")))

    def _write_cache(self, data: dict[str, Any]) -> None:
        self._cache_path.write_text(
            json.dumps(data, indent=2), encoding="utf-8"
        )
        self._cache_path.chmod(0o600)


def _api_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.api+json",
        "Content-Type": "application/vnd.api+json",
    }


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _token_expired(cached: dict[str, Any]) -> bool:
    obtained = float(cached.get("obtained_at", 0))
    expires_in = int(cached.get("expires_in", 0))
    return _now_epoch() >= obtained + max(expires_in - 60, 0)


def _now_epoch() -> float:
    import time

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


def _parse_collection_item(
    item: dict[str, Any], included: dict[str, dict[str, Any]]
) -> Track | None:
    ref_type = item.get("type")
    ref_id = item.get("id")
    if not ref_type or not ref_id:
        return None

    resource = included.get(f"{ref_type}:{ref_id}")
    if resource is None and ref_type == "tracks":
        resource = item

    if resource is None:
        return None

    attrs = resource.get("attributes", {})
    title = attrs.get("title") or attrs.get("name") or ""
    artist = _artist_name(resource, included)
    added_at = (item.get("meta") or {}).get("addedAt")

    if not title:
        return None

    return Track(
        name=title,
        artist=artist,
        platform_id=str(ref_id),
        added_at=added_at,
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


class _RedirectHandler(BaseHTTPRequestHandler):
    code: str | None = None
    state: str | None = None

    def do_GET(self) -> None:  # noqa: N802
        qs = parse_qs(urlparse(self.path).query)
        _RedirectHandler.code = qs.get("code", [None])[0]
        _RedirectHandler.state = qs.get("state", [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"<html><body>Autorizado. Puedes cerrar esta ventana.</body></html>")

    def log_message(self, format: str, *args: Any) -> None:
        return


def _wait_for_redirect(redirect_uri: str) -> tuple[str, str | None]:
    parsed = urlparse(redirect_uri)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if port in (80, 443):
        port = 8080

    server = HTTPServer(("127.0.0.1", port), _RedirectHandler)
    server.handle_request()
    server.server_close()

    if not _RedirectHandler.code:
        raise TidalError("no se recibió código de autorización Tidal")
    return _RedirectHandler.code, _RedirectHandler.state
