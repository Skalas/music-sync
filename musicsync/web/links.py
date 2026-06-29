"""Per-platform track URL resolver.

Spotify and Tidal use deep links keyed by platform_id.
Apple Music has no stored catalog id, so we return a search URL instead.
"""

from __future__ import annotations

from urllib.parse import quote_plus


def track_url(
    platform: str,
    *,
    platform_id: str | None,
    name: str,
    artist: str,
) -> str | None:
    """Return a playable URL for the track on *platform*, or None when unavailable.

    - spotify: deep link from platform_id; None if platform_id is missing.
    - tidal:   deep link from platform_id; None if platform_id is missing.
    - apple:   always a search URL (Apple catalog id is not yet stored).
    """
    if platform == "spotify":
        if not platform_id:
            return None
        return f"https://open.spotify.com/track/{platform_id}"

    if platform == "tidal":
        if not platform_id:
            return None
        return f"https://tidal.com/browse/track/{platform_id}"

    if platform == "apple":
        term = quote_plus(f"{name} {artist}")
        return f"https://music.apple.com/search?term={term}"

    return None
