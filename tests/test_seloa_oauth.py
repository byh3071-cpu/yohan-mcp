"""Persistent SELOA OAuth storage and refresh endpoint contracts."""
from __future__ import annotations

import pytest
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl

import adapters.seloa_oauth as oauth


def _storage(tmp_path, url: str) -> oauth.WindowsTokenStorage:
    prefix = b"encrypted:"
    return oauth.WindowsTokenStorage(
        url,
        path=tmp_path / "oauth.bin",
        protect=lambda data: prefix + data[::-1],
        unprotect=lambda data: data[len(prefix):][::-1],
    )


async def test_tokens_and_registration_are_encrypted_and_survive_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(oauth.time, "time", lambda: 1000.0)
    url = "https://example.test/mcp"
    storage = _storage(tmp_path, url)
    token = OAuthToken(access_token="access-secret", refresh_token="refresh-secret", expires_in=120)
    client = OAuthClientInformationFull(client_id="client-id", redirect_uris=[AnyUrl(oauth.REDIRECT_URI)])

    await storage.set_client_info(client)
    await storage.set_tokens(token)
    encrypted = (tmp_path / "oauth.bin").read_bytes()
    assert b"access-secret" not in encrypted
    assert b"refresh-secret" not in encrypted
    assert b"client-id" not in encrypted

    restarted = _storage(tmp_path, url)
    assert (await restarted.get_tokens()).refresh_token == "refresh-secret"
    assert (await restarted.get_client_info()).client_id == "client-id"
    assert restarted.expires_at() == 1060.0

    provider = oauth.make_oauth_provider(url, restarted)
    assert str(provider.context.oauth_metadata.token_endpoint) == "https://example.test/oauth/token"
    assert provider.context.token_expiry_time == 1060.0
    provider.context.current_tokens = await restarted.get_tokens()
    provider.context.client_info = await restarted.get_client_info()
    refresh_request = await provider._refresh_token()
    assert str(refresh_request.url) == "https://example.test/oauth/token"
    assert b"refresh-secret" in refresh_request.content


async def test_state_is_bound_to_the_remote_resource(tmp_path):
    original = _storage(tmp_path, "https://example.test/mcp")
    await original.set_tokens(OAuthToken(access_token="a", refresh_token="b"))
    changed = _storage(tmp_path, "https://different.test/mcp")
    with pytest.raises(ValueError, match="another endpoint"):
        await changed.get_tokens()


async def test_missing_expiry_hint_forces_refresh_after_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(oauth.time, "time", lambda: 1000.0)
    storage = _storage(tmp_path, "https://example.test/mcp")
    await storage.set_tokens(OAuthToken(access_token="a", refresh_token="b"))
    assert storage.expires_at() < 1000.0
    assert oauth.make_oauth_provider("https://example.test/mcp", storage).context.token_expiry_time < 1000.0
