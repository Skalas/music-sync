"""T2 — Backward compatibility with pre-sprint Spotify⇄Apple reconcile."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from musicsync.domain.track import Track
from musicsync.domain.union import compute_to_sync


def reconcile_legacy(
    spotify: list[Track],
    apple: list[Track],
    state: dict[str, list[str]],
) -> tuple[list[Track], list[Track]]:
    """Pre-sprint 2-way reconcile — local shim for backward-compat golden tests."""
    spotify_keys = {t.key for t in spotify}
    apple_keys = {t.key for t in apple}
    done_apple = set(state.get("apple", []))
    done_spotify = set(state.get("spotify", []))

    to_apple = [
        t for t in spotify if t.key not in apple_keys and t.key not in done_apple
    ]
    to_spotify = [
        t for t in apple if t.key not in spotify_keys and t.key not in done_spotify
    ]
    return to_apple, to_spotify

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def golden_case() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads((FIXTURES / "spotify_apple_golden.json").read_text()))


def test_backward_compat_matches_legacy(golden_case: dict) -> None:
    spotify = [Track(**t) for t in golden_case["spotify"]]
    apple = [Track(**t) for t in golden_case["apple"]]
    state = golden_case["state"]

    legacy_apple, legacy_spotify = reconcile_legacy(spotify, apple, state)

    presence = {
        "spotify": {t.key: t for t in spotify},
        "apple": {t.key: t for t in apple},
    }
    synced = {
        "apple": set(state.get("apple", [])),
        "spotify": set(state.get("spotify", [])),
    }
    nway = compute_to_sync(presence, synced)

    assert sorted(t.key for t in nway["apple"]) == sorted(t.key for t in legacy_apple)
    assert sorted(t.key for t in nway["spotify"]) == sorted(t.key for t in legacy_spotify)
