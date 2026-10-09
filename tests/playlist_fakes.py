"""In-memory PlaylistProvider fake shared by the playlist test modules."""

from __future__ import annotations

from dataclasses import dataclass, field

from musicsync.domain.playlist import Playlist, PlaylistAddResult, PlaylistSummary
from musicsync.domain.track import Track


@dataclass
class AddCall:
    name: str
    remote_id: str | None
    tracks: list[Track]


@dataclass
class FakePlaylistProvider:
    """Holds playlists by name; ``add_to_playlist`` appends like a real platform."""

    name: str
    playlists: dict[str, list[Track]] = field(default_factory=dict)
    can_playlist_read: bool = True
    can_playlist_write: bool = True
    reserved_playlist_names: frozenset[str] = frozenset()
    read_error: BaseException | None = None
    write_error: BaseException | None = None
    unresolvable: set[str] = field(default_factory=set)
    """Track names this platform cannot resolve (e.g. not in the Apple library)."""
    add_calls: list[AddCall] = field(default_factory=list)
    followed: set[str] = field(default_factory=set)
    """Playlist names that exist here but belong to another user (owned=False)."""

    def _remote_id(self, playlist_name: str) -> str:
        return f"{self.name}:{playlist_name}"

    def list_playlists(self) -> list[PlaylistSummary]:
        if self.read_error is not None:
            raise self.read_error
        return [
            PlaylistSummary(
                platform=self.name,
                name=name,
                remote_id=self._remote_id(name),
                track_count=len(tracks),
                owned=name not in self.followed,
            )
            for name, tracks in self.playlists.items()
        ]

    def read_playlist(self, summary: PlaylistSummary) -> Playlist:
        return Playlist(
            platform=self.name,
            name=summary.name,
            remote_id=summary.remote_id,
            tracks=tuple(self.playlists[summary.name]),
        )

    def add_to_playlist(
        self, name: str, remote_id: str | None, tracks: list[Track]
    ) -> PlaylistAddResult:
        self.add_calls.append(AddCall(name, remote_id, list(tracks)))
        if self.write_error is not None:
            raise self.write_error
        added = [t for t in tracks if t.name not in self.unresolvable]
        unresolved = [t for t in tracks if t.name in self.unresolvable]
        existing = (n for n in self.playlists if self._remote_id(n) == remote_id)
        name = next(existing, name)
        target = self.playlists.setdefault(name, [])
        present = {t.key for t in target}
        target.extend(t for t in added if t.key not in present)
        return PlaylistAddResult(
            remote_id=self._remote_id(name), added=added, unresolved=unresolved
        )


def track(name: str, artist: str = "Artist") -> Track:
    return Track(name=name, artist=artist)
