"""Per-platform track URL resolver.

Spotify and Tidal use deep links keyed by platform_id.
Apple Music uses the catalog id resolved by the batch step (``apple_catalog``)
when present, and falls back to a search URL otherwise.
"""

from __future__ import annotations

from urllib.parse import quote, quote_plus


def track_url(
    platform: str,
    *,
    platform_id: str | None,
    name: str,
    artist: str,
    storefront: str | None = None,
) -> str | None:
    """Return a playable URL for the track on *platform*, or None when unavailable.

    - spotify: deep link from platform_id; None if platform_id is missing.
    - tidal:   deep link from platform_id; None if platform_id is missing.
    - apple:   song link from catalog id + storefront; search URL when either is missing.
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
        if platform_id and storefront:
            return (
                f"https://music.apple.com/{quote(storefront, safe='')}"
                f"/song/{quote(platform_id, safe='')}"
            )
        term = quote_plus(f"{name} {artist}")
        return f"https://music.apple.com/search?term={term}"

    return None
