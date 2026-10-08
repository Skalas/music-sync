"""T2 — Apple Music developer token (ES256 JWT) and credential loading."""

from __future__ import annotations

from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from musicsync.infrastructure.apple_music_api import (
    TOKEN_REFRESH_MARGIN_SEC,
    TOKEN_TTL_SEC,
    AppleMusicError,
    DeveloperTokenProvider,
    build_apple_catalog_client,
    load_apple_credentials,
)

APPLE_ENV_KEYS = ("APPLE_TEAM_ID", "APPLE_KEY_ID", "APPLE_PRIVATE_KEY_PATH", "APPLE_STOREFRONT")
MAX_TTL_SEC = 15_777_000  # 6 months: Apple's documented ceiling


def _throwaway_key() -> tuple[str, str]:
    """Return (private PEM, public PEM) for a fresh P-256 key."""
    private = ec.generate_private_key(ec.SECP256R1())
    private_pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public_pem = private.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return private_pem, public_pem


class _Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def _clean_apple_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in APPLE_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_token_is_es256_with_kid_and_team_claims() -> None:
    private_pem, public_pem = _throwaway_key()
    provider = DeveloperTokenProvider(
        team_id="TEAM123", key_id="KEY456", private_key=private_pem, clock=_Clock(1_000_000)
    )

    token = provider.token()

    header = jwt.get_unverified_header(token)
    assert header["alg"] == "ES256"
    assert header["kid"] == "KEY456"
    claims = jwt.decode(
        token, public_pem, algorithms=["ES256"], options={"verify_exp": False}
    )
    assert claims["iss"] == "TEAM123"
    assert claims["iat"] == 1_000_000
    assert claims["exp"] - claims["iat"] == TOKEN_TTL_SEC
    assert TOKEN_TTL_SEC <= MAX_TTL_SEC


def test_token_is_cached_then_refreshed_near_expiry() -> None:
    private_pem, _public = _throwaway_key()
    clock = _Clock(1_000_000)
    provider = DeveloperTokenProvider(
        team_id="T", key_id="K", private_key=private_pem, clock=clock
    )

    first = provider.token()
    clock.now += 60
    assert provider.token() == first

    clock.now = 1_000_000 + TOKEN_TTL_SEC - TOKEN_REFRESH_MARGIN_SEC
    refreshed = provider.token()
    assert refreshed != first
    assert jwt.get_unverified_header(refreshed)["kid"] == "K"


def test_invalid_key_raises_without_leaking_material() -> None:
    provider = DeveloperTokenProvider(team_id="T", key_id="K", private_key="not-a-pem-secret")
    with pytest.raises(AppleMusicError) as exc:
        provider.token()
    assert "not-a-pem-secret" not in str(exc.value)


def test_repr_does_not_expose_key_or_token() -> None:
    private_pem, _public = _throwaway_key()
    provider = DeveloperTokenProvider(team_id="T", key_id="K", private_key=private_pem)
    token = provider.token()
    assert private_pem not in repr(provider)
    assert token not in repr(provider)


def _write_env(base: Path, key_path: str, storefront: str | None = None) -> None:
    lines = ["APPLE_TEAM_ID=TEAM123", "APPLE_KEY_ID=KEY456", f"APPLE_PRIVATE_KEY_PATH={key_path}"]
    if storefront:
        lines.append(f"APPLE_STOREFRONT={storefront}")
    (base / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_credentials_expand_tilde_and_default_storefront(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    _write_env(tmp_path, "~/keys/AuthKey.p8")

    creds, missing = load_apple_credentials(tmp_path)

    assert missing == []
    assert creds is not None
    assert creds.private_key_path == tmp_path / "keys" / "AuthKey.p8"
    assert creds.storefront == "us"


def test_missing_credentials_disable_client(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("", encoding="utf-8")

    client, reason = build_apple_catalog_client(tmp_path)

    assert client is None
    assert reason is not None
    assert "APPLE_TEAM_ID" in reason


def test_unreadable_key_file_disables_client(tmp_path: Path) -> None:
    _write_env(tmp_path, str(tmp_path / "missing.p8"))

    client, reason = build_apple_catalog_client(tmp_path)

    assert client is None
    assert reason is not None
    assert "APPLE_PRIVATE_KEY_PATH" in reason


def test_invalid_key_file_disables_client_without_leaking(tmp_path: Path) -> None:
    key_file = tmp_path / "AuthKey.p8"
    key_file.write_text("-----BEGIN PRIVATE KEY-----\nSECRETGARBAGE\n", encoding="utf-8")
    _write_env(tmp_path, str(key_file))

    client, reason = build_apple_catalog_client(tmp_path)

    assert client is None
    assert reason is not None
    assert "SECRETGARBAGE" not in reason
    assert "\n" not in reason


def test_non_utf8_key_file_disables_client(tmp_path: Path) -> None:
    key_file = tmp_path / "AuthKey.p8"
    key_file.write_bytes(b"\xff\xfe\x00binary")
    _write_env(tmp_path, str(key_file))

    client, reason = build_apple_catalog_client(tmp_path)

    assert client is None
    assert reason is not None
    assert "APPLE_PRIVATE_KEY_PATH" in reason


def test_configured_client_uses_storefront(tmp_path: Path) -> None:
    private_pem, _public = _throwaway_key()
    key_file = tmp_path / "AuthKey.p8"
    key_file.write_text(private_pem, encoding="utf-8")
    _write_env(tmp_path, str(key_file), storefront="SV")

    client, reason = build_apple_catalog_client(tmp_path)

    assert reason is None
    assert client is not None
    assert client.storefront == "sv"
