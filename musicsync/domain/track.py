"""Track model and cross-platform normalization key."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from unidecode import unidecode

KEY_SEP = "␟"  # separador no imprimible para la clave de match

_PAREN_RE = re.compile(r"[\(\[].*?[\)\]]")
_NOISE_RE = re.compile(
    r"\b(feat\.?|featuring|remaster(ed)?|remix|deluxe|version|edit|mono|stereo)\b.*",
    re.IGNORECASE,
)
_NONALNUM_RE = re.compile(r"[^a-z0-9 ]+")
_WS_RE = re.compile(r"\s+")


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


@dataclass
class Track:
    name: str
    artist: str
    key: str = field(init=False)
    platform_id: str | None = None
    added_at: str | None = None

    def __post_init__(self) -> None:
        self.key = normalize_key(self.name, self.artist)

    @property
    def line(self) -> str:
        """Linea de salida en el formato exacto 'Nombre - Artista'."""
        name = self.name.replace("\n", " ").replace("\r", " ").strip()
        artist = self.artist.replace("\n", " ").replace("\r", " ").strip()
        return f"{name} - {artist}"
