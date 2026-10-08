"""Apple Music catalog API: ES256 developer token + read-only catalog client.

Credentials come only from .env (APPLE_TEAM_ID, APPLE_KEY_ID,
APPLE_PRIVATE_KEY_PATH, optional APPLE_STOREFRONT). The developer JWT is a
bearer secret: it is never logged, printed, or placed in exception messages.
"""

from __future__ import annotations

import logging
import math
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jwt
import requests
from requests.exceptions import RequestException

from musicsync.domain.apple_catalog import CatalogSong
from musicsync.domain.errors import PlatformOperationError
from musicsync.infrastructure._env import load_env_keys

logger = logging.getLogger(__name__)

API_BASE = "https://api.music.apple.com/v1"
REQUIRED_KEYS = ["APPLE_TEAM_ID", "APPLE_KEY_ID", "APPLE_PRIVATE_KEY_PATH"]
STOREFRONT_KEY = "APPLE_STOREFRONT"
DEFAULT_STOREFRONT = "us"

TOKEN_ALGORITHM = "ES256"
TOKEN_TTL_SEC = 12 * 60 * 60  # Apple permite hasta 6 meses; un batch dura minutos.
TOKEN_REFRESH_MARGIN_SEC = 5 * 60

SEARCH_LIMIT = 10
REQUEST_TIMEOUT_SEC = 30
MAX_ATTEMPTS = 4
MAX_BACKOFF_SEC = 60
HTTP_TOO_MANY_REQUESTS = 429
HTTP_SERVER_ERROR = 500
HTTP_CLIENT_ERROR = 400
MILLIS_PER_SEC = 1000

# Fallback cuando Apple no trae el album relacionado: nombres típicos de recopilatorio.
_COMPILATION_ALBUM_RE = re.compile(
    r"\b(greatest hits|best of|hits of|now that s what|the essential|essentials|"
    r"anthology|compilation|collection|various artists)\b",
    re.IGNORECASE,
)
_VARIOUS_ARTISTS = "various artists"


class AppleMusicError(PlatformOperationError):
    """Apple Music catalog request failed (auth, quota, network, bad key)."""


@dataclass(frozen=True)
class AppleMusicCredentials:
    team_id: str
    key_id: str
    private_key_path: Path
    storefront: str


def load_apple_credentials(
    base_dir: Path,
) -> tuple[AppleMusicCredentials | None, list[str]]:
    """Return ``(credentials, missing_keys)``; credentials is None when any key is missing."""
    config, missing = load_env_keys(base_dir, REQUIRED_KEYS)
    if missing:
        return None, missing
    storefront = os.environ.get(STOREFRONT_KEY, "").strip().lower() or DEFAULT_STOREFRONT
    return (
        AppleMusicCredentials(
            team_id=config["APPLE_TEAM_ID"],
            key_id=config["APPLE_KEY_ID"],
            private_key_path=Path(config["APPLE_PRIVATE_KEY_PATH"]).expanduser(),
            storefront=storefront,
        ),
        [],
    )


class DeveloperTokenProvider:
    """Mint and cache the ES256 developer token, refreshing it near expiry."""

    def __init__(
        self,
        *,
        team_id: str,
        key_id: str,
        private_key: str,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._team_id = team_id
        self._key_id = key_id
        self._private_key = private_key
        self._clock = clock
        self._token: str | None = None
        self._expires_at = 0.0

    def __repr__(self) -> str:
        return f"DeveloperTokenProvider(team_id={self._team_id!r}, key_id={self._key_id!r})"

    @classmethod
    def from_credentials(cls, creds: AppleMusicCredentials) -> DeveloperTokenProvider:
        try:
            private_key = creds.private_key_path.read_text(encoding="utf-8")
        except (OSError, ValueError) as exc:
            reason = exc.strerror if isinstance(exc, OSError) else type(exc).__name__
            raise AppleMusicError(
                f"no se pudo leer APPLE_PRIVATE_KEY_PATH ({creds.private_key_path}): {reason}"
            ) from None
        provider = cls(team_id=creds.team_id, key_id=creds.key_id, private_key=private_key)
        provider.token()  # valida la clave al arrancar: una clave rota desactiva, no falla a medias
        return provider

    def token(self) -> str:
        now = self._clock()
        if self._token is None or now >= self._expires_at - TOKEN_REFRESH_MARGIN_SEC:
            self._token, self._expires_at = self._mint(now)
        return self._token

    def _mint(self, now: float) -> tuple[str, float]:
        issued_at = int(now)
        expires_at = issued_at + TOKEN_TTL_SEC
        try:
            token = jwt.encode(
                {"iss": self._team_id, "iat": issued_at, "exp": expires_at},
                self._private_key,
                algorithm=TOKEN_ALGORITHM,
                headers={"kid": self._key_id},
            )
        except (ValueError, TypeError, jwt.PyJWTError) as exc:
            raise AppleMusicError(
                f"clave privada de Apple Music inválida: {type(exc).__name__}"
            ) from None
        return token, float(expires_at)


class AppleCatalogClient:
    """Read-only Apple Music catalog lookups for one storefront."""

    def __init__(
        self,
        token_provider: DeveloperTokenProvider,
        storefront: str,
        *,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._tokens = token_provider
        self.storefront = storefront
        self._session = session or requests.Session()
        self._sleep = sleep

    def songs_by_isrc(self, isrc: str) -> list[CatalogSong]:
        body = self._get("songs", {"filter[isrc]": isrc, "include": "albums"})
        return _parse_songs(body.get("data"))

    def search_songs(self, term: str) -> list[CatalogSong]:
        body = self._get(
            "search", {"types": "songs", "limit": str(SEARCH_LIMIT), "term": term}
        )
        songs = _as_dict(_as_dict(body.get("results")).get("songs")).get("data")
        return _parse_songs(songs)

    def _get(self, resource: str, params: dict[str, str]) -> dict[str, Any]:
        path = f"/catalog/{self.storefront}/{resource}"
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                resp = self._session.get(
                    f"{API_BASE}{path}",
                    headers={"Authorization": f"Bearer {self._tokens.token()}"},
                    params=params,
                    timeout=REQUEST_TIMEOUT_SEC,
                )
            except RequestException as exc:
                if attempt == MAX_ATTEMPTS:
                    raise AppleMusicError(
                        f"GET {path} falló: {type(exc).__name__}"
                    ) from None
                self._sleep(_backoff(attempt))
                continue

            if _is_retryable(resp.status_code) and attempt < MAX_ATTEMPTS:
                self._sleep(_retry_after(resp, attempt))
                continue
            if resp.status_code >= HTTP_CLIENT_ERROR:
                raise AppleMusicError(f"GET {path} → HTTP {resp.status_code}")
            return _json_object(resp, path)

        raise AppleMusicError(f"GET {path} agotados los reintentos")


def build_apple_catalog_client(
    base_dir: Path,
) -> tuple[AppleCatalogClient | None, str | None]:
    """Return ``(client, None)`` or ``(None, reason)`` when Apple links are unavailable."""
    creds, missing = load_apple_credentials(base_dir)
    if creds is None:
        return None, f"faltan {', '.join(missing)} en .env"
    try:
        tokens = DeveloperTokenProvider.from_credentials(creds)
    except AppleMusicError as exc:
        return None, str(exc)
    return AppleCatalogClient(tokens, creds.storefront), None


def _json_object(resp: requests.Response, path: str) -> dict[str, Any]:
    try:
        body = resp.json()
    except ValueError:
        raise AppleMusicError(f"GET {path} → respuesta no JSON") from None
    if not isinstance(body, dict):
        raise AppleMusicError(f"GET {path} → respuesta JSON inesperada")
    return body


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _is_retryable(status: int) -> bool:
    return status == HTTP_TOO_MANY_REQUESTS or status >= HTTP_SERVER_ERROR


def _backoff(attempt: int) -> float:
    return float(min(2**attempt, MAX_BACKOFF_SEC))


def _retry_after(resp: requests.Response, attempt: int) -> float:
    raw = resp.headers.get("Retry-After")
    try:
        seconds = float(raw) if raw is not None else math.nan
    except ValueError:
        seconds = math.nan
    if not math.isfinite(seconds) or seconds < 0:
        return _backoff(attempt)
    return min(seconds, MAX_BACKOFF_SEC)


def _parse_songs(resources: Any) -> list[CatalogSong]:
    if not isinstance(resources, list):
        return []
    songs = [_parse_song(r) for r in resources if isinstance(r, dict)]
    return [s for s in songs if s is not None]


def _parse_song(resource: dict[str, Any]) -> CatalogSong | None:
    """Return a CatalogSong, or None when the resource is malformed."""
    attrs = _as_dict(resource.get("attributes"))
    catalog_id = resource.get("id")
    name = attrs.get("name")
    artist = attrs.get("artistName")
    if not isinstance(catalog_id, (str, int)) or isinstance(catalog_id, bool):
        return None
    if not catalog_id or not isinstance(name, str) or not name:
        return None
    if not isinstance(artist, str):
        return None
    album = _str_or_none(attrs.get("albumName"))
    return CatalogSong(
        catalog_id=str(catalog_id),
        name=name,
        artist=artist,
        album=album,
        duration_sec=_duration_sec(attrs.get("durationInMillis")),
        is_compilation=_is_compilation(resource, album),
        url=_str_or_none(attrs.get("url")),
    )


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _duration_sec(millis: Any) -> int | None:
    if isinstance(millis, bool) or not isinstance(millis, (int, float)):
        return None
    if not math.isfinite(millis) or millis < 0:
        return None
    return round(millis / MILLIS_PER_SEC)


def _first_album_attributes(resource: dict[str, Any]) -> dict[str, Any]:
    """Attributes of the first related album, or {} when absent or malformed."""
    albums = _as_dict(_as_dict(resource.get("relationships")).get("albums")).get("data")
    if not isinstance(albums, list) or not albums:
        return {}
    return _as_dict(_as_dict(albums[0]).get("attributes"))


def _is_compilation(resource: dict[str, Any], album_name: str | None) -> bool:
    """Prefer the related album's ``isCompilation``; fall back to an album-name heuristic.

    Malformed relationship data counts as unknown, never as a compilation.
    """
    album_attrs = _first_album_attributes(resource)
    flag = album_attrs.get("isCompilation")
    if isinstance(flag, bool):
        return flag
    album_artist = album_attrs.get("artistName")
    if isinstance(album_artist, str) and album_artist.strip().lower() == _VARIOUS_ARTISTS:
        return True
    return bool(album_name and _COMPILATION_ALBUM_RE.search(album_name.replace("'", " ")))
