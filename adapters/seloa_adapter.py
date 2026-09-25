"""PC-local proxy for SELOA's remote streamable HTTP MCP server.

SELOA's task/event data stays in its own model. This module returns the remote
CallToolResult JSON unchanged and never routes it through yohan-mcp search.
"""
from __future__ import annotations

import asyncio
import os
from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult, TextContent


TOOL_NAMES = frozenset(
    {
        "seloa_today", "seloa_between", "seloa_tasks", "seloa_search",
        "seloa_overview", "seloa_create", "seloa_update", "seloa_delete",
        "seloa_changes", "seloa_undo",
    }
)
_TIMEOUT_SECONDS = 30.0


def _local_error(code: str, message: str) -> CallToolResult:
    return CallToolResult(
        isError=True,
        content=[TextContent(type="text", text=message)],
        structuredContent={"error": {"code": code}},
    )


class SeloaAdapter:
    """One bounded MCP session per call; configuration is read only from env."""

    async def call(self, name: str, arguments: dict[str, Any]) -> CallToolResult:
        if name not in TOOL_NAMES:
            return _local_error("unknown_tool", "Unknown SELOA tool.")

        token = os.getenv("SELOA_MCP_TOKEN", "").strip()
        if not token:
            return _local_error("not_configured", "SELOA is disabled: SELOA_MCP_TOKEN is missing.")
        url = os.getenv("SELOA_MCP_URL", "").strip()
        try:
            parsed = urlsplit(url)
        except ValueError:
            return _local_error("invalid_config", "SELOA_MCP_URL must be a valid HTTPS endpoint.")
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            return _local_error("invalid_config", "SELOA_MCP_URL must be an HTTPS endpoint without credentials or query parameters.")

        try:
            async with asyncio.timeout(_TIMEOUT_SECONDS):
                async with httpx.AsyncClient(
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=httpx.Timeout(20.0, connect=10.0),
                ) as client:
                    async with streamable_http_client(url, http_client=client) as (read, write, _):
                        async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=20)) as session:
                            await session.initialize()
                            return await session.call_tool(
                                name, arguments=arguments, read_timeout_seconds=timedelta(seconds=20)
                            )
        except (TimeoutError, httpx.TimeoutException):
            return _local_error("timeout", "SELOA request timed out.")
        except Exception:
            # SDK/httpx exceptions can include Authorization headers or URL details.
            return _local_error("remote_unavailable", "SELOA request failed; check local configuration and connectivity.")
