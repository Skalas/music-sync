"""Apple Music library provider (Shortcut + AppleScript)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from musicsync.domain.track import Track

SHORTCUT_NAME = "SyncToAppleMusic"


class AppleProvider:
    name = "apple"
    can_write = True
    graceful_on_apply_error = False

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
        except FileNotFoundError:
            sys.exit("ERROR: 'osascript' no está disponible (¿estás en macOS?).")
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

    def apply_likes(self, tracks: list[Track]) -> list[Track]:
        if not tracks:
            return []

        self._output_path.write_text(
            "\n".join(t.line for t in tracks) + "\n", encoding="utf-8"
        )
        print(f"\nExportadas {len(tracks)} canciones a {self._output_path.name}")
        self._trigger_shortcut(self._output_path)
        self._mark_loved(self._output_path)
        return tracks

    def _trigger_shortcut(self, input_path: Path) -> None:
        print(f"-> Ejecutando atajo '{SHORTCUT_NAME}'...")
        try:
            subprocess.run(
                ["shortcuts", "run", SHORTCUT_NAME, "-i", str(input_path)],
                check=True,
            )
        except subprocess.CalledProcessError:
            sys.exit(
                f"ERROR: falló el atajo '{SHORTCUT_NAME}'. "
                "Verifica que exista en Atajos.app (ver README)."
            )

    def write_review(self, tracks: list[Track], path: Path) -> None:
        """Write the candidate list as plain 'Name - Artist' lines for human review."""
        path.write_text(
            "\n".join(t.line for t in tracks) + "\n", encoding="utf-8"
        )

    def _mark_loved(self, input_path: Path) -> None:
        print("-> Marcando como Favorita/Love en Apple Music...")
        script = self._applescript_dir / "mark_loved.applescript"
        result = subprocess.run(
            ["osascript", str(script), str(input_path)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"   aviso: no se pudo marcar Love ({result.stderr.strip()})")
        else:
            print(f"   {result.stdout.strip()}")
