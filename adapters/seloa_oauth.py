"""Windows-local OAuth state for the SELOA MCP client.

The installed MCP SDK persists tokens through TokenStorage, but not their
absolute expiry or the discovered token endpoint. Keep both across processes
so a restarted server refreshes instead of starting an interactive login.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from mcp.client.auth import OAuthClientProvider
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthMetadata, OAuthToken
from pydantic import AnyHttpUrl, AnyUrl

REDIRECT_HOST = "127.0.0.1"
REDIRECT_PORT = 8765
REDIRECT_PATH = "/callback"
REDIRECT_URI = f"http://{REDIRECT_HOST}:{REDIRECT_PORT}{REDIRECT_PATH}"
DEFAULT_STATE_FILE = (
    Path(os.environ["LOCALAPPDATA"]) / "yohan-mcp" / "seloa-oauth.bin"
    if os.getenv("LOCALAPPDATA")
    else Path(__file__).resolve().parents[1] / "data" / "seloa-oauth.bin"
)


def _protect(data: bytes) -> bytes:
    import win32crypt

    return win32crypt.CryptProtectData(data, "SELOA OAuth", None, None, None, 0)


def _unprotect(data: bytes) -> bytes:
    import win32crypt

    return win32crypt.CryptUnprotectData(data, None, None, None, 0)[1]


class WindowsTokenStorage:
    """Persist OAuth tokens and client registration encrypted for this user."""

    def __init__(
        self,
        resource_url: str,
        path: Path | None = None,
        protect: Callable[[bytes], bytes] = _protect,
        unprotect: Callable[[bytes], bytes] = _unprotect,
    ) -> None:
        self.resource_url = resource_url
        configured_path = os.getenv("SELOA_OAUTH_STATE_FILE", "").strip()
        self.path = path or (Path(configured_path) if configured_path else DEFAULT_STATE_FILE)
        self.protect = protect
        self.unprotect = unprotect

    def _read(self) -> dict:
        if not self.path.exists():
            return {"version": 1, "resource": self.resource_url}
        raw = self.unprotect(self.path.read_bytes())
        state = json.loads(raw.decode("utf-8"))
        if not isinstance(state, dict) or state.get("version") != 1 or state.get("resource") != self.resource_url:
            raise ValueError("SELOA OAuth state is invalid or belongs to another endpoint")
        return state

    def _write(self, state: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        encrypted = self.protect(json.dumps(state, ensure_ascii=False).encode("utf-8"))
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self.path.parent, prefix=self.path.name + ".", suffix=".tmp"
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(encrypted)
            os.replace(temporary, self.path)
        finally:
            if temporary.exists():
                temporary.unlink()

    def has_tokens(self) -> bool:
        return bool(self._read().get("tokens"))

    def expires_at(self) -> float | None:
        value = self._read().get("expires_at")
        return float(value) if isinstance(value, (int, float)) else None

    async def get_tokens(self) -> OAuthToken | None:
        value = self._read().get("tokens")
        return OAuthToken.model_validate(value) if value else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        state = self._read()
        state["tokens"] = tokens.model_dump(mode="json")
        # No expiry hint must not turn into an indefinitely valid cached token.
        state["expires_at"] = time.time() + max(0, tokens.expires_in - 60) if tokens.expires_in else time.time() - 1
        self._write(state)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        value = self._read().get("client_info")
        return OAuthClientInformationFull.model_validate(value) if value else None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        state = self._read()
        state["client_info"] = client_info.model_dump(mode="json")
        self._write(state)


def make_oauth_provider(
    url: str,
    storage: WindowsTokenStorage,
    redirect_handler=None,
    callback_handler=None,
) -> OAuthClientProvider:
    parsed = urlsplit(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    provider = OAuthClientProvider(
        server_url=url,
        client_metadata=OAuthClientMetadata(
            client_name="Yohan MCP SELOA",
            redirect_uris=[AnyUrl(REDIRECT_URI)],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            scope="seloa:access",
            token_endpoint_auth_method="none",
        ),
        storage=storage,
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )
    # MCP SDK 1.27.2 forgets discovery and expiry on restart. Without these,
    # refresh goes to /token instead of SELOA's /oauth/token and expired tokens
    # cause a browser login instead of refresh.
    provider.context.oauth_metadata = OAuthMetadata(
        issuer=AnyHttpUrl(origin),
        authorization_endpoint=AnyHttpUrl(origin + "/authorize"),
        token_endpoint=AnyHttpUrl(origin + "/oauth/token"),
        registration_endpoint=AnyHttpUrl(origin + "/oauth/register"),
    )
    provider.context.token_expiry_time = storage.expires_at()
    return provider
