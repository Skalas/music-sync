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
_PRIMARY_ARTIST_RE = re.compile(r"[,&;/]| feat")


def _normalize_field(value: str) -> str:
    value = unidecode(value or "").lower()
    value = _PAREN_RE.sub(" ", value)
    value = _NOISE_RE.sub(" ", value)
    value = _NONALNUM_RE.sub(" ", value)
    return _WS_RE.sub(" ", value).strip()


def normalize_text(value: str) -> str:
    """Normalize free text (titles, albums) the same way the match key does."""
    return _normalize_field(value)


# --- Recording title -------------------------------------------------------
# A title decoration is a bracketed segment ("(feat. X)", "[Remastered]") or a
# " - " suffix ("- From \"Shrek 2\" Soundtrack"). Decorations that do not change
# the recording are dropped; everything else (live, remix, acoustic...) is kept.

_BRACKET_SEGMENT_RE = re.compile(r"[\(\[]([^\(\)\[\]]*)[\)\]]")
_DASH_SEPARATOR = " - "

FEATURE_CREDIT_RE = re.compile(r"^(feat|ft|featuring|with)\b")
"""Featured-artist credit: same recording, credited differently per platform."""

SOURCE_TAG_RE = re.compile(r"^from\b|\bsoundtrack\b|\bmotion picture\b")
"""Soundtrack/source tag ("From \"Black Panther\"", "from the motion picture ...")."""

EDITION_TAG_RE = re.compile(
    r"(\d{4} )?(digital(ly)? )?remaster(ed)?( \d{4})?|deluxe( edition)?|mono|stereo"
)
"""Remaster/edition tag, matched against the WHOLE decoration."""

RECORDING_MARKER_RE = re.compile(
    r"\b(live|remix|mix|acoustic|edit|instrumental|demo|extended|version|cover|"
    r"karaoke|sped up|slowed)\b"
)
"""Markers of a different recording: a decoration carrying one is always kept."""


def _fold(value: str) -> str:
    value = _NONALNUM_RE.sub(" ", value)
    return _WS_RE.sub(" ", value).strip()


def _is_neutral_decoration(segment: str) -> bool:
    """A recording marker anywhere wins, even inside a credit ("feat. Sia - Alesso Remix").

    Trade-off: a credit naming an artist with a marker word ("feat. Mix Master
    Mike") is kept too — a false negative (stays unresolved), never a wrong link.
    """
    folded = _fold(segment)
    if RECORDING_MARKER_RE.search(folded):
        return False
    return bool(
        FEATURE_CREDIT_RE.match(folded)
        or SOURCE_TAG_RE.search(folded)
        or EDITION_TAG_RE.fullmatch(folded)
    )


def _strip_neutral_brackets(value: str) -> str:
    return _BRACKET_SEGMENT_RE.sub(
        lambda m: " " if _is_neutral_decoration(m.group(1)) else f" {m.group(1)} ", value
    )


def _strip_neutral_suffixes(value: str) -> str:
    head, *suffixes = value.split(_DASH_SEPARATOR)
    kept = [s for s in suffixes if not _is_neutral_decoration(s)]
    return " ".join([head, *kept])


def normalize_recording_title(value: str) -> str:
    """Title that identifies the *recording*: same on both platforms or a different song.

    Folds accents, case, punctuation and whitespace, and drops recording-neutral
    decorations (featured-artist credits, soundtrack/source tags, remaster/deluxe/
    mono/stereo tags). Recording-changing markers (live, remix, acoustic, edit,
    instrumental, demo, extended, version, cover, karaoke, sped up/slowed) are
    kept, so "Song (Live)" never equals "Song".
    """
    value = unidecode(value or "").lower()
    value = _strip_neutral_suffixes(_strip_neutral_brackets(value))
    return _fold(value)


def match_artist(artist: str) -> str:
    """Artist half of the match key: the normalized primary artist."""
    return _normalize_field(primary_artist(artist))


def year_from_date(raw: str | None) -> str | None:
    """Return the 4-digit year prefix of an ISO-style date string, or None."""
    return raw[:4] if raw else None


def date_only(raw: str | None) -> str | None:
    """Normalize a date/datetime string to YYYY-MM-DD (10 chars).

    Providers may return full ISO-8601 datetimes; this trims to the date part so
    added_at is consistent across Spotify, Tidal, and Apple in the database.
    """
    if not raw:
        return None
    return raw[:10] if len(raw) >= 10 else raw


def primary_artist(artist: str) -> str:
    """Primer artista acreditado: lo que precede a ',', '&', ';', '/' o ' feat'.

    Las plataformas acreditan colaboraciones de formas distintas ("Azealia Banks,
    Lazy Jay" vs "Azealia Banks"), asi que el artista principal es la unica parte
    comparable entre servicios.
    """
    return _PRIMARY_ARTIST_RE.split(artist or "", maxsplit=1)[0].strip()


def normalize_key(name: str, artist: str) -> str:
    """Clave heuristica para emparejar canciones entre servicios."""
    return f"{_normalize_field(name)}{KEY_SEP}{match_artist(artist)}"


@dataclass
class Track:
    name: str
    artist: str
    key: str = field(init=False)
    platform_id: str | None = None
    added_at: str | None = None
    album: str | None = None
    artwork_url: str | None = None
    duration_sec: int | None = None
    year: str | None = None
    isrc: str | None = None

    def __post_init__(self) -> None:
        self.key = normalize_key(self.name, self.artist)

    @property
    def line(self) -> str:
        """Linea de salida en el formato exacto 'Nombre - Artista'."""
        name = self.name.replace("\n", " ").replace("\r", " ").strip()
        artist = self.artist.replace("\n", " ").replace("\r", " ").strip()
        return f"{name} - {artist}"
