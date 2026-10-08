"""Apple Music library provider (AppleScript, plus a Shortcut only for imports).

Apple's scripting dictionary can favorite tracks that are ALREADY in the
library, but its `add` command takes local files only and its `search` command
is scoped to `library.read` — neither can pull a track out of the Apple Music
catalog. Importing therefore still needs the SyncToAppleMusic Shortcut, so
``apply_likes`` splits the work in two: favorite everything already present via
AppleScript, then fall through to the Shortcut only for what is genuinely
missing. A library that already overlaps with Spotify/Tidal skips the Shortcut
entirely.
"""

from __future__ import annotations

import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from musicsync.domain.errors import PlatformOperationError
from musicsync.domain.track import Track, primary_artist, year_from_date

SHORTCUT_NAME = "SyncToAppleMusic"
_TAB = "\t"

# Per-line statuses reported by mark_loved.applescript.
_STATUS_FAVORITED = "OK"
_STATUS_MISSING = "MISSING"


def _parse_apple_line(line: str) -> Track | None:
    """Parse one line of AppleScript output.

    New format (6 tab-separated fields):
        name TAB artist TAB album TAB year TAB duration_sec TAB date_added

    Legacy fallback (old "Name - Artist" format): parse just name and artist.
    Any line that doesn't match either format is skipped.
    """
    if _TAB in line:
        # 6 fields; split at most 5 times so a stray tab in the last field (date)
        # doesn't shift positions.
        parts = line.split(_TAB, 5)
        if len(parts) < 2:
            return None
        name = parts[0].strip()
        artist = parts[1].strip()
        if not name and not artist:
            return None
        album = parts[2].strip() if len(parts) > 2 else None
        year_raw = parts[3].strip() if len(parts) > 3 else ""
        dur_raw = parts[4].strip() if len(parts) > 4 else ""
        added_at = parts[5].strip() if len(parts) > 5 else None
        return Track(
            name=name,
            artist=artist,
            album=album or None,
            year=year_from_date(year_raw or None),
            duration_sec=int(dur_raw) if dur_raw.isdigit() else None,
            added_at=added_at or None,
        )
    # Legacy format: "Name - Artist"
    if " - " not in line:
        return None
    name, _, artist = line.rpartition(" - ")
    return Track(name=name.strip(), artist=artist.strip())


def _tsv_field(value: str) -> str:
    """Flatten a field so it cannot break the TAB-delimited line contract."""
    return value.replace(_TAB, " ").replace("\n", " ").replace("\r", " ").strip()


def _search_line(track: Track) -> str:
    """Store-search text for the Shortcut: 'Name - Primary Artist'.

    The Shortcut feeds this straight into an Apple Music catalog search, and the
    catalog credits a single primary artist. A comma-joined credit list matches
    nothing there: "212 - Azealia Banks, Lazy Jay" finds no results because the
    catalog entry is "212 (feat. Lazy Jay)" by "Azealia Banks". Review files keep
    the full credits (see write_review) — those are read by humans, not searched.
    """
    artist = primary_artist(track.artist) or track.artist
    return f"{_tsv_field(track.name)} - {_tsv_field(artist)}"


@dataclass(frozen=True)
class _FavoriteOutcome:
    """How one favorite pass resolved, partitioned by what to do next."""

    favorited: list[Track] = field(default_factory=list)
    """Confirmed marked in the library — safe to checkpoint as synced."""

    missing: list[Track] = field(default_factory=list)
    """Absent from the library; only the Shortcut can bring these in."""

    failed: list[Track] = field(default_factory=list)
    """Present but not markable, or unreported. Left unsynced so they retry."""


def _partition_by_status(tracks: list[Track], stdout: str) -> _FavoriteOutcome:
    """Pair mark_loved's status lines with the tracks that were sent to it.

    The script contracts to emit exactly one status line per input line, in
    order, so pairing is positional — that keeps duplicate "Name - Artist"
    entries distinct, which a lookup keyed by text could not. A short or garbled
    report leaves the tail unaccounted for; those tracks land in *failed* rather
    than being optimistically reported as synced.
    """
    statuses = [
        line.split(_TAB, 1)[0].strip()
        for line in stdout.splitlines()
        if line.strip()
    ]
    outcome = _FavoriteOutcome()
    for index, track in enumerate(tracks):
        status = statuses[index] if index < len(statuses) else ""
        if status == _STATUS_FAVORITED:
            outcome.favorited.append(track)
        elif status == _STATUS_MISSING:
            outcome.missing.append(track)
        else:
            outcome.failed.append(track)
    return outcome


class AppleProvider:
    name = "apple"
    can_write = True
    graceful_on_error = False

    def __init__(
        self,
        *,
        applescript_dir: Path,
        output_path: Path,
    ) -> None:
        self._applescript_dir = applescript_dir
        self._output_path = output_path

    def read_liked(self) -> list[Track]:
        script = self._applescript_dir / "read_loved.applescript"
        try:
            result = subprocess.run(
                ["osascript", str(script)],
                capture_output=True,
                text=True,
                check=True,
            )
        except FileNotFoundError as exc:
            raise PlatformOperationError(
                "'osascript' no está disponible (¿estás en macOS?)."
            ) from exc
        except subprocess.CalledProcessError as exc:
            raise PlatformOperationError(
                f"error leyendo Apple Music:\n{exc.stderr.strip()}"
            ) from exc

        tracks: list[Track] = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            track = _parse_apple_line(line)
            if track is not None:
                tracks.append(track)
        return tracks

    def apply_likes(
        self,
        tracks: list[Track],
        *,
        on_batch: Callable[[list[Track]], None] | None = None,
        reorder: bool = False,
    ) -> list[Track]:
        """Favorite what is already in the library, import only the rest.

        *reorder* is accepted for port compatibility and ignored: Apple Music
        exposes no ordered favorites list to rewrite.
        """
        if not tracks:
            return []

        def checkpoint(batch: list[Track]) -> None:
            if batch and on_batch is not None:
                on_batch(batch)

        print(f"\n-> Marcando Favorita en la biblioteca ({len(tracks)} candidatas)...")
        present = self._favorite_in_library(tracks)
        print(
            f"   marcadas: {len(present.favorited)}"
            f" | faltan importar: {len(present.missing)}"
        )
        applied = list(present.favorited)
        checkpoint(present.favorited)
        unresolved = list(present.failed)

        if not present.missing:
            print(f"   nada que importar: se omite el atajo '{SHORTCUT_NAME}'.")
        else:
            applied.extend(self._import_missing(present.missing, checkpoint, unresolved))

        if unresolved:
            print(
                f"   aviso: {len(unresolved)} sin aplicar"
                " (quedan pendientes y se reintentan en la próxima corrida):"
            )
            for track in unresolved:
                print(f"     - {track.line}")
        return applied

    def _import_missing(
        self,
        missing: list[Track],
        checkpoint: Callable[[list[Track]], None],
        unresolved: list[Track],
    ) -> list[Track]:
        """Run the Shortcut for catalog imports, then favorite what arrived."""
        # Sin newline final a proposito: el Atajo hace "Split by New Lines" y un
        # salto al final produce un elemento vacio extra, es decir una iteracion
        # que busca "" en la tienda y puede agregar una cancion arbitraria.
        self._output_path.write_text(
            "\n".join(_search_line(t) for t in missing), encoding="utf-8"
        )
        print(f"   Exportadas {len(missing)} canciones a {self._output_path.name}")
        self._trigger_shortcut(self._output_path)

        imported = self._favorite_in_library(missing)
        print(f"   importadas y marcadas: {len(imported.favorited)}")
        checkpoint(imported.favorited)
        unresolved.extend(imported.missing)
        unresolved.extend(imported.failed)
        return imported.favorited

    def _favorite_in_library(self, tracks: list[Track]) -> _FavoriteOutcome:
        """Mark *tracks* as favorites, reporting which ones actually landed."""
        script = self._applescript_dir / "mark_loved.applescript"
        payload = "".join(
            f"{_tsv_field(t.name)}{_TAB}{_tsv_field(t.artist)}\n" for t in tracks
        )
        with tempfile.NamedTemporaryFile(
            "w", suffix=".tsv", encoding="utf-8", delete=False
        ) as handle:
            handle.write(payload)
            tsv_path = Path(handle.name)

        try:
            result = subprocess.run(
                ["osascript", str(script), str(tsv_path)],
                capture_output=True,
                text=True,
            )
        except FileNotFoundError as exc:
            raise PlatformOperationError(
                "'osascript' no está disponible (¿estás en macOS?)."
            ) from exc
        finally:
            tsv_path.unlink(missing_ok=True)

        if result.returncode != 0:
            raise PlatformOperationError(
                "no se pudo marcar Favorita en Apple Music:\n"
                f"{result.stderr.strip()}"
            )
        return _partition_by_status(tracks, result.stdout)

    def _trigger_shortcut(self, input_path: Path) -> None:
        print(f"-> Ejecutando atajo '{SHORTCUT_NAME}'...")
        try:
            subprocess.run(
                ["shortcuts", "run", SHORTCUT_NAME, "-i", str(input_path)],
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            raise PlatformOperationError(
                f"ERROR: falló el atajo '{SHORTCUT_NAME}'. "
                "Verifica que exista en Atajos.app (ver README)."
            ) from exc

    def write_review(self, tracks: list[Track], path: Path) -> None:
        """Write the candidate list as plain 'Name - Artist' lines for human review."""
        path.write_text(
            "\n".join(t.line for t in tracks) + "\n", encoding="utf-8"
        )
