"""Output-file path constants for the sync operation.

All paths are anchored to the project root so they resolve to the same
location regardless of the working directory.
"""

from __future__ import annotations

from pathlib import Path

# Assumes the in-tree/editable layout: this file lives at
# <project_root>/musicsync/application/output_paths.py, so the project root is
# three parents up.
BASE_DIR = Path(__file__).resolve().parent.parent.parent

# Project-anchored locations shared by the CLI and the web composition root.
APPLESCRIPT_DIR = BASE_DIR / "applescript"
DEFAULT_DB = BASE_DIR / "library.db"
STATE_PATH = BASE_DIR / "state.json"

TO_APPLE_PATH = BASE_DIR / "canciones_to_apple.txt"
TO_SPOTIFY_REVIEW_PATH = BASE_DIR / "to_spotify_review.txt"
UNMATCHED_LOG_PATH = BASE_DIR / "unmatched.log"
