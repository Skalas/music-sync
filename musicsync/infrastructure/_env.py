"""Shared environment-key loader for infrastructure providers."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


def load_env_keys(
    base_dir: Path, keys: list[str]
) -> tuple[dict[str, str], list[str]]:
    """Load dotenv from *base_dir*/.env and return (config, missing).

    The caller is responsible for deciding what to do with missing keys —
    Spotify exits the process; Tidal raises TidalError for graceful skip.
    """
    assert base_dir.is_absolute(), "base_dir must be an absolute path"
    load_dotenv(base_dir / ".env")
    config = {key: os.environ.get(key, "").strip() for key in keys}
    missing = [key for key, value in config.items() if not value]
    return config, missing
