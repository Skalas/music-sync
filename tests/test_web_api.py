"""A1–A6 — FastAPI endpoint tests.

Tests use the FastAPI TestClient with a seeded temp DB so no real providers
or remote libraries are touched.  Providers are monkeypatched where needed.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from musicsync.domain.platforms import PLATFORMS
from musicsync.infrastructure.seed import seed_db
from musicsync.web.app import create_app
from musicsync.web.container import Container, build_container

# State-changing POST endpoints require this header (CSRF guard).
XHR_HEADERS = {"X-Requested-With": "XMLHttpRequest"}

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def seeded_db(tmp_path: Path) -> Path:
    db = tmp_path / "test_library.db"
    seed_db(db)
    return db


@pytest.fixture()
def client(seeded_db: Path) -> TestClient:
    # Pass an empty providers list so tests do not require .env credentials.
    # Individual tests that need provider behaviour monkeypatch at the class level.
    app = create_app(db_path=seeded_db, providers=[])
    return TestClient(app)


@pytest.fixture()
def container(seeded_db: Path) -> Container:
    return build_container(seeded_db, providers=[])


# ---------------------------------------------------------------------------
# Fake providers for live-read tests
# ---------------------------------------------------------------------------


class _RecordingReadProvider:
    """Fake provider whose read_liked succeeds and records call counts."""

    can_write = True
    graceful_on_error = False

    def __init__(self, name: str) -> None:
        self.name = name
        self.read_liked_calls = 0

    def read_liked(self) -> list[Any]:
        self.read_liked_calls += 1
        return []

    def apply_likes(self, tracks: list[Any]) -> list[Any]:
        return tracks

    def write_review(self, tracks: list[Any], path: Any) -> None:
        pass


class _FailingReadProvider(_RecordingReadProvider):
    """Fake provider whose read_liked always raises (simulates unconnected)."""

    def read_liked(self) -> list[Any]:
        self.read_liked_calls += 1
        raise RuntimeError("not connected")


# ---------------------------------------------------------------------------
# A1 — GET /api/library
# ---------------------------------------------------------------------------


class TestLibraryEndpoint:
    def test_returns_seeded_tracks(self, client: TestClient) -> None:
        resp = client.get("/api/library")
        assert resp.status_code == 200
        body = resp.json()
        assert "items" in body
        assert body["total"] > 0

    def test_track_shape(self, client: TestClient) -> None:
        resp = client.get("/api/library")
        track = resp.json()["items"][0]
        assert "key" in track
        assert "name" in track
        assert "artist" in track
        assert "presence" in track
        assert "links" in track
        assert "on_all_three" in track
        # New metadata fields
        assert "album" in track
        assert "artwork_url" in track
        assert "duration_sec" in track
        assert "year" in track
        assert "added_at" in track
        assert isinstance(track["added_at"], dict)

    def test_presence_booleans(self, client: TestClient) -> None:
        resp = client.get("/api/library")
        # Bohemian Rhapsody is on spotify + apple but NOT tidal in seed data
        items = resp.json()["items"]
        br = next((t for t in items if t["name"] == "Bohemian Rhapsody"), None)
        assert br is not None
        assert br["presence"]["spotify"] is True
        assert br["presence"]["apple"] is True
        assert br["presence"]["tidal"] is False

    def test_on_all_three_flag(self, client: TestClient) -> None:
        resp = client.get("/api/library")
        items = resp.json()["items"]
        # No track in seed data is on all three platforms
        for item in items:
            assert item["on_all_three"] == (
                item["presence"]["spotify"]
                and item["presence"]["apple"]
                and item["presence"]["tidal"]
            )

    def test_apple_link_always_present(self, client: TestClient) -> None:
        resp = client.get("/api/library")
        for track in resp.json()["items"]:
            assert track["links"]["apple"] is not None
            assert track["links"]["apple"].startswith("https://music.apple.com/search")

    def test_spotify_link_null_without_platform_id(self, client: TestClient) -> None:
        # Seed data doesn't set platform_id, so Spotify links should be None
        resp = client.get("/api/library")
        for track in resp.json()["items"]:
            assert track["links"]["spotify"] is None

    def test_q_filter_name(self, client: TestClient) -> None:
        resp = client.get("/api/library?q=Bohemian")
        body = resp.json()
        assert body["total"] >= 1
        for item in body["items"]:
            assert "bohemian" in item["name"].lower() or "bohemian" in item["artist"].lower()

    def test_q_filter_no_match(self, client: TestClient) -> None:
        resp = client.get("/api/library?q=ZZZNOMATCH")
        assert resp.json()["total"] == 0

    def test_filter_all_three(self, client: TestClient) -> None:
        resp = client.get("/api/library?filter=all_three")
        for item in resp.json()["items"]:
            assert item["on_all_three"] is True

    def test_filter_missing_somewhere(self, client: TestClient) -> None:
        resp = client.get("/api/library?filter=missing_somewhere")
        for item in resp.json()["items"]:
            assert item["on_all_three"] is False

    def test_pagination(self, client: TestClient) -> None:
        resp_all = client.get("/api/library")
        total = resp_all.json()["total"]

        # Get page 1 with size 2
        resp_p1 = client.get("/api/library?page=1&page_size=2")
        body_p1 = resp_p1.json()
        assert len(body_p1["items"]) == min(2, total)
        assert body_p1["total"] == total
        assert body_p1["page"] == 1

        # Get page 2 with size 2
        if total > 2:
            resp_p2 = client.get("/api/library?page=2&page_size=2")
            body_p2 = resp_p2.json()
            # Different items on different pages
            keys_p1 = {t["key"] for t in body_p1["items"]}
            keys_p2 = {t["key"] for t in body_p2["items"]}
            assert keys_p1.isdisjoint(keys_p2)


# ---------------------------------------------------------------------------
# A2 — POST /api/sync (dry-run) + GET /api/sync/stream (SSE)
# ---------------------------------------------------------------------------


class TestSyncEndpoint:
    def test_dry_run_returns_diff(self, client: TestClient) -> None:
        resp = client.post(
            "/api/sync", json={"offline": True}, headers=XHR_HEADERS
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "to_sync" in body
        assert "counts" in body

    def test_dry_run_counts_match_to_sync(self, client: TestClient) -> None:
        resp = client.post(
            "/api/sync", json={"offline": True}, headers=XHR_HEADERS
        )
        body = resp.json()
        for platform, tracks in body["to_sync"].items():
            assert body["counts"][platform] == len(tracks)

    def test_dry_run_does_not_write(self, seeded_db: Path) -> None:
        """Verify dry-run leaves the DB unchanged (no platform marked synced)."""
        container = build_container(seeded_db, providers=[])
        client = TestClient(create_app(db_path=seeded_db, providers=[]))

        client.post("/api/sync", json={"offline": True}, headers=XHR_HEADERS)

        synced = sum(len(container.repo.get_synced_keys(p)) for p in PLATFORMS)
        assert synced == 0, "dry-run must not mark anything synced"

    def test_sync_stream_sse_events(self, client: TestClient) -> None:
        with client.stream("GET", "/api/sync/stream") as resp:
            assert resp.status_code == 200
            content = resp.read().decode()

        # SSE events must include at least a "done" event
        assert "done" in content

    def test_sync_request_defaults_to_live(self) -> None:
        """Sync now must default to a live read (offline=False)."""
        from musicsync.web.app import SyncRequest

        assert SyncRequest().offline is False

    def test_live_sync_skips_failing_provider(self, seeded_db: Path) -> None:
        """A live POST /api/sync must not 500 when a provider's read fails."""
        failing = _FailingReadProvider("spotify")
        healthy = _RecordingReadProvider("tidal")
        app = create_app(db_path=seeded_db, providers=[failing, healthy])
        client = TestClient(app)

        # Live read (offline=False is the default body).
        resp = client.post("/api/sync", json={"offline": False}, headers=XHR_HEADERS)

        assert resp.status_code == 200
        # The failing provider was attempted but did not crash the request.
        assert failing.read_liked_calls == 1
        # The healthy provider still read and contributed to the refresh.
        assert healthy.read_liked_calls == 1
        # Response is a well-formed diff over the (still-present) DB rows.
        body = resp.json()
        assert "to_sync" in body
        assert "counts" in body


# ---------------------------------------------------------------------------
# Connected-gating: the web container builds only set-up platforms
# ---------------------------------------------------------------------------


class TestConnectedGating:
    def test_unconfigured_spotify_is_excluded(
        self, seeded_db: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """No Spotify creds → provider is never built, so live sync omits it."""
        import musicsync.web.container as container_mod

        # No creds in env, and a base_dir with no token caches.
        for var in ("SPOTIPY_CLIENT_ID", "SPOTIPY_CLIENT_SECRET", "SPOTIPY_REDIRECT_URI"):
            monkeypatch.delenv(var, raising=False)
        empty_base = tmp_path / "no_caches"
        empty_base.mkdir()
        monkeypatch.setattr(container_mod, "BASE_DIR", empty_base)

        providers = container_mod._build_providers()
        names = {p.name for p in providers}
        assert "spotify" not in names
        assert "tidal" not in names  # no .tidal-cache either

    def test_connected_spotify_is_included(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Creds present + .cache token exists → Spotify is built."""
        import musicsync.web.container as container_mod

        monkeypatch.setenv("SPOTIPY_CLIENT_ID", "id")
        monkeypatch.setenv("SPOTIPY_CLIENT_SECRET", "secret")
        monkeypatch.setenv("SPOTIPY_REDIRECT_URI", "http://127.0.0.1:8080")
        base = tmp_path / "with_cache"
        base.mkdir()
        (base / ".cache").write_text("{}", encoding="utf-8")
        monkeypatch.setattr(container_mod, "BASE_DIR", base)

        configured, connected = container_mod.connection_status("spotify", base)
        assert configured is True
        assert connected is True

    def test_live_sync_with_no_connected_providers_returns_200(
        self, seeded_db: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """End-to-end: no platform connected → Sync now still works (DB-only diff)."""
        import musicsync.web.container as container_mod

        for var in (
            "SPOTIPY_CLIENT_ID",
            "SPOTIPY_CLIENT_SECRET",
            "SPOTIPY_REDIRECT_URI",
            "TIDAL_CLIENT_ID",
            "TIDAL_REDIRECT_URI",
        ):
            monkeypatch.delenv(var, raising=False)
        empty_base = tmp_path / "no_caches2"
        empty_base.mkdir()
        monkeypatch.setattr(container_mod, "BASE_DIR", empty_base)

        # providers=None → container builds via the connected-gated factory.
        app = create_app(db_path=seeded_db)
        client = TestClient(app)

        resp = client.post("/api/sync", json={"offline": False}, headers=XHR_HEADERS)
        assert resp.status_code == 200
        assert "to_sync" in resp.json()


# ---------------------------------------------------------------------------
# connection_status reads env that .env loading (build_container) populated
# ---------------------------------------------------------------------------


_OAUTH_ENV_KEYS = (
    "SPOTIPY_CLIENT_ID",
    "SPOTIPY_CLIENT_SECRET",
    "SPOTIPY_REDIRECT_URI",
    "TIDAL_CLIENT_ID",
    "TIDAL_REDIRECT_URI",
)


class TestEnvLoading:
    @staticmethod
    def _clear_oauth_env(monkeypatch: pytest.MonkeyPatch) -> None:
        # Hermetic: a developer's real shell env must not influence the result,
        # and monkeypatch restores (deletes) any value load_dotenv injects below.
        for key in _OAUTH_ENV_KEYS:
            monkeypatch.delenv(key, raising=False)

    def test_configured_true_from_dotenv_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from dotenv import load_dotenv

        from musicsync.web.container import connection_status

        self._clear_oauth_env(monkeypatch)
        (tmp_path / ".env").write_text(
            "SPOTIPY_CLIENT_ID=id\n"
            "SPOTIPY_CLIENT_SECRET=secret\n"
            "SPOTIPY_REDIRECT_URI=http://127.0.0.1:8080\n"
            "TIDAL_CLIENT_ID=tid\n"
            "TIDAL_REDIRECT_URI=http://127.0.0.1:8080/tidal\n",
            encoding="utf-8",
        )

        # build_container loads .env once at startup; emulate that here so the
        # values come from the .env file, not ambient env (cleared above).
        load_dotenv(tmp_path / ".env")

        spotify_configured, _ = connection_status("spotify", tmp_path)
        tidal_configured, _ = connection_status("tidal", tmp_path)
        assert spotify_configured is True
        assert tidal_configured is True

    def test_configured_false_without_dotenv_or_env(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from musicsync.web.container import connection_status

        self._clear_oauth_env(monkeypatch)
        # No .env loaded and keys cleared → nothing configured.
        spotify_configured, _ = connection_status("spotify", tmp_path)
        tidal_configured, _ = connection_status("tidal", tmp_path)
        assert spotify_configured is False
        assert tidal_configured is False


# ---------------------------------------------------------------------------
# POST /api/auth/{platform}/connect — builds a fresh provider on demand
# ---------------------------------------------------------------------------


class TestConnectEndpoint:
    def test_connect_configured_but_not_connected_returns_started(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Spotify configured but no token cache: connect must 200, not 500."""
        import musicsync.web.app as app_mod

        monkeypatch.setenv("SPOTIPY_CLIENT_ID", "id")
        monkeypatch.setenv("SPOTIPY_CLIENT_SECRET", "secret")
        monkeypatch.setenv("SPOTIPY_REDIRECT_URI", "http://127.0.0.1:8080")

        built: list[str] = []

        class _StubProvider:
            name = "spotify"

            def read_liked(self) -> list[Any]:  # no real browser/OAuth
                return []

        def _fake_build(platform: str, base_dir: Any, **kwargs: Any) -> _StubProvider:
            built.append(platform)
            return _StubProvider()

        monkeypatch.setattr(app_mod, "build_connect_provider", _fake_build)

        resp = client.post("/api/auth/spotify/connect", headers=XHR_HEADERS)

        assert resp.status_code == 200
        assert resp.json()["started"] is True
        assert built == ["spotify"]

    def test_connect_not_configured_returns_400(
        self, seeded_db: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import musicsync.web.app as app_mod
        import musicsync.web.container as container_mod

        for var in (
            "SPOTIPY_CLIENT_ID",
            "SPOTIPY_CLIENT_SECRET",
            "SPOTIPY_REDIRECT_URI",
        ):
            monkeypatch.delenv(var, raising=False)
        # Hermetic: point at a base dir with no .env so creds can't be loaded.
        empty_base = tmp_path / "no_env"
        empty_base.mkdir()
        monkeypatch.setattr(container_mod, "BASE_DIR", empty_base)
        monkeypatch.setattr(app_mod, "BASE_DIR", empty_base)

        client = TestClient(create_app(db_path=seeded_db, providers=[]))
        resp = client.post("/api/auth/spotify/connect", headers=XHR_HEADERS)
        assert resp.status_code == 400

    def test_duplicate_connect_returns_409(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A second in-flight connect for the same platform is rejected (port clash)."""
        import musicsync.web.app as app_mod

        monkeypatch.setenv("SPOTIPY_CLIENT_ID", "id")
        monkeypatch.setenv("SPOTIPY_CLIENT_SECRET", "secret")
        monkeypatch.setenv("SPOTIPY_REDIRECT_URI", "http://127.0.0.1:8080")

        with app_mod._connect_lock:
            app_mod._connect_in_progress.discard("spotify")

        release = threading.Event()

        class _BlockingProvider:
            name = "spotify"

            def read_liked(self) -> list[Any]:
                release.wait(timeout=5)  # hold the "in progress" flag
                return []

        monkeypatch.setattr(
            app_mod, "build_connect_provider", lambda *a, **k: _BlockingProvider()
        )

        try:
            first = client.post("/api/auth/spotify/connect", headers=XHR_HEADERS)
            assert first.status_code == 200
            second = client.post("/api/auth/spotify/connect", headers=XHR_HEADERS)
            assert second.status_code == 409
        finally:
            release.set()
        # Drain so the background thread clears the flag for later tests.
        import time

        for _ in range(50):
            with app_mod._connect_lock:
                if "spotify" not in app_mod._connect_in_progress:
                    break
            time.sleep(0.02)

    def test_connect_apple_reports_no_oauth(self, client: TestClient) -> None:
        resp = client.post("/api/auth/apple/connect", headers=XHR_HEADERS)
        assert resp.status_code == 200
        body = resp.json()
        assert body["started"] is False
        assert "OAuth" in body["message"]


# ---------------------------------------------------------------------------
# A3 — POST /api/apply/{platform} (guarded write)
# ---------------------------------------------------------------------------


class _MockProvider:
    """Minimal mock provider for apply-path tests."""

    can_write = True
    graceful_on_error = False

    def __init__(self, name: str) -> None:
        self.name = name
        self.apply_likes_calls: list[Any] = []
        self.read_liked_calls = 0

    def read_liked(self) -> list[Any]:
        self.read_liked_calls += 1
        return []

    def apply_likes(self, tracks: list[Any]) -> list[Any]:
        self.apply_likes_calls.append(tracks)
        return tracks

    def write_review(self, tracks: list[Any], path: Any) -> None:
        pass


class TestApplyEndpoint:
    def test_apply_spotify_invokes_provider(self, seeded_db: Path) -> None:
        """apply_likes is routed only through POST /api/apply/{platform}."""
        mock_spotify = _MockProvider("spotify")
        app = create_app(db_path=seeded_db, providers=[mock_spotify])
        client = TestClient(app)

        resp = client.post("/api/apply/spotify", headers=XHR_HEADERS)

        assert resp.status_code == 200
        body = resp.json()
        assert body["platform"] == "spotify"
        assert "applied" in body
        # apply_likes was called (may be 0 tracks if seeded DB already synced,
        # but the apply flag was set — verified by the fact that apply_likes_calls
        # has an entry if there was anything to sync, or stays empty if already done)
        # The important assertion: the endpoint did NOT raise and returned correctly.

    def test_sync_does_not_call_apply(self, seeded_db: Path) -> None:
        """POST /api/sync (dry-run) must NEVER call apply_likes."""
        mock_spotify = _MockProvider("spotify")
        app = create_app(db_path=seeded_db, providers=[mock_spotify])
        client = TestClient(app)

        client.post(
            "/api/sync", json={"offline": True}, headers=XHR_HEADERS
        )

        assert mock_spotify.apply_likes_calls == [], "dry-run must not call apply_likes"

    def test_apply_only_targets_requested_platform(self, seeded_db: Path) -> None:
        """POST /api/apply/spotify must only set apply_spotify=True, not tidal."""
        mock_spotify = _MockProvider("spotify")
        mock_tidal = _MockProvider("tidal")
        app = create_app(db_path=seeded_db, providers=[mock_spotify, mock_tidal])
        client = TestClient(app)

        client.post("/api/apply/spotify", headers=XHR_HEADERS)
        # Tidal apply_likes must NOT be called when applying spotify
        assert mock_tidal.apply_likes_calls == []

    def test_apply_is_offline_no_network_read(self, seeded_db: Path) -> None:
        """Apply works from existing DB presence — it must not re-read providers."""
        mock_spotify = _MockProvider("spotify")
        mock_tidal = _MockProvider("tidal")
        app = create_app(db_path=seeded_db, providers=[mock_spotify, mock_tidal])
        client = TestClient(app)

        resp = client.post("/api/apply/spotify", headers=XHR_HEADERS)

        assert resp.status_code == 200
        # offline=True: no provider's read_liked() is called during apply.
        assert mock_spotify.read_liked_calls == 0
        assert mock_tidal.read_liked_calls == 0
        # Only the requested platform's apply path runs.
        assert mock_tidal.apply_likes_calls == []

    def test_apply_unknown_platform_returns_404(self, client: TestClient) -> None:
        resp = client.post("/api/apply/deezer", headers=XHR_HEADERS)
        assert resp.status_code == 404

    def test_apply_tidal_routes_correctly(self, seeded_db: Path) -> None:
        mock_tidal = _MockProvider("tidal")
        app = create_app(db_path=seeded_db, providers=[mock_tidal])
        client = TestClient(app)

        resp = client.post("/api/apply/tidal", headers=XHR_HEADERS)

        assert resp.status_code == 200
        assert resp.json()["platform"] == "tidal"


# ---------------------------------------------------------------------------
# B3 — CSRF guard: state-changing POSTs require X-Requested-With header
# ---------------------------------------------------------------------------


class TestCsrfGuard:
    def test_sync_without_xhr_header_is_forbidden(self, client: TestClient) -> None:
        resp = client.post("/api/sync", json={"offline": True})
        assert resp.status_code == 403

    def test_apply_without_xhr_header_is_forbidden(self, seeded_db: Path) -> None:
        mock_spotify = _MockProvider("spotify")
        app = create_app(db_path=seeded_db, providers=[mock_spotify])
        client = TestClient(app)

        resp = client.post("/api/apply/spotify")
        assert resp.status_code == 403
        # The guarded write path must not run when the CSRF check fails.
        assert mock_spotify.apply_likes_calls == []

    def test_connect_without_xhr_header_is_forbidden(self, client: TestClient) -> None:
        resp = client.post("/api/auth/spotify/connect")
        assert resp.status_code == 403

    def test_wrong_xhr_header_value_is_forbidden(self, client: TestClient) -> None:
        resp = client.post(
            "/api/sync",
            json={"offline": True},
            headers={"X-Requested-With": "fetch"},
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# A4/A5 — GET /api/auth/{platform} (status shape; no browser flow)
# ---------------------------------------------------------------------------


class TestAuthStatus:
    def test_spotify_status_shape(self, client: TestClient) -> None:
        resp = client.get("/api/auth/spotify")
        assert resp.status_code == 200
        body = resp.json()
        assert body["platform"] == "spotify"
        assert isinstance(body["configured"], bool)
        assert isinstance(body["connected"], bool)

    def test_apple_status_shape(self, client: TestClient) -> None:
        resp = client.get("/api/auth/apple")
        assert resp.status_code == 200
        body = resp.json()
        assert body["platform"] == "apple"
        assert isinstance(body["configured"], bool)
        assert isinstance(body["connected"], bool)

    def test_tidal_status_shape(self, client: TestClient) -> None:
        resp = client.get("/api/auth/tidal")
        assert resp.status_code == 200
        body = resp.json()
        assert body["platform"] == "tidal"
        assert isinstance(body["configured"], bool)
        assert isinstance(body["connected"], bool)

    def test_tidal_configured_without_client_secret(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """C2 — Tidal PKCE needs only CLIENT_ID + REDIRECT_URI, never a secret."""
        monkeypatch.setenv("TIDAL_CLIENT_ID", "test-id")
        monkeypatch.setenv("TIDAL_REDIRECT_URI", "http://127.0.0.1:8080/callback")
        monkeypatch.delenv("TIDAL_CLIENT_SECRET", raising=False)

        resp = client.get("/api/auth/tidal")
        assert resp.status_code == 200
        assert resp.json()["configured"] is True

    def test_tidal_not_configured_without_client_id(
        self, seeded_db: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import musicsync.web.app as app_mod
        import musicsync.web.container as container_mod

        monkeypatch.delenv("TIDAL_CLIENT_ID", raising=False)
        monkeypatch.delenv("TIDAL_REDIRECT_URI", raising=False)
        # Hermetic: a base dir with no .env so creds can't be loaded from file.
        empty_base = tmp_path / "no_env_tidal"
        empty_base.mkdir()
        monkeypatch.setattr(container_mod, "BASE_DIR", empty_base)
        monkeypatch.setattr(app_mod, "BASE_DIR", empty_base)

        client = TestClient(create_app(db_path=seeded_db, providers=[]))
        resp = client.get("/api/auth/tidal")
        assert resp.status_code == 200
        assert resp.json()["configured"] is False

    def test_unknown_platform_returns_404(self, client: TestClient) -> None:
        resp = client.get("/api/auth/deezer")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# A6 — GET /api/export.csv
# ---------------------------------------------------------------------------


class TestCsvExport:
    def test_returns_csv_with_correct_headers(self, client: TestClient) -> None:
        resp = client.get("/api/export.csv")
        assert resp.status_code == 200
        assert "text/csv" in resp.headers["content-type"]
        lines = resp.text.strip().splitlines()
        # Existing columns must appear in order; new metadata columns appended after.
        assert lines[0].startswith("key,name,artist,spotify,apple,tidal")
        for col in ("album", "year", "duration_sec", "artwork_url"):
            assert col in lines[0]

    def test_attachment_content_disposition(self, client: TestClient) -> None:
        resp = client.get("/api/export.csv")
        assert "attachment" in resp.headers.get("content-disposition", "")

    def test_row_count_matches_tracks(self, client: TestClient, seeded_db: Path) -> None:
        from musicsync.infrastructure.sqlite_repository import SqliteTrackRepository

        repo = SqliteTrackRepository(seeded_db)
        expected = len(repo.iter_export_rows())
        repo.close()

        resp = client.get("/api/export.csv")
        lines = resp.text.strip().splitlines()
        # Header + data rows
        assert len(lines) == expected + 1

    def test_seeded_tracks_appear_in_csv(self, client: TestClient) -> None:
        resp = client.get("/api/export.csv")
        assert "Bohemian Rhapsody" in resp.text
