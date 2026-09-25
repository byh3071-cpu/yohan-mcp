"""SELOA MCP proxy contracts, with every remote call replaced by a local fake."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest
from mcp.types import CallToolResult, TextContent

import adapters.seloa_adapter as adapter_module
from core.core_rules import available_tools
import server


@pytest.fixture
def remote(monkeypatch):
    calls: list[tuple[str, dict]] = []
    seen_headers: list[str] = []
    monkeypatch.setenv("SELOA_MCP_URL", "https://example.test/mcp")
    monkeypatch.setenv("SELOA_MCP_TOKEN", "fake-private-token")

    @asynccontextmanager
    async def fake_transport(url, *, http_client):
        assert url == "https://example.test/mcp"
        seen_headers.append(http_client.headers["Authorization"])
        yield "read", "write", lambda: None

    class FakeSession:
        def __init__(self, read, write, read_timeout_seconds):
            assert (read, write) == ("read", "write")
            assert read_timeout_seconds.total_seconds() == 20

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def initialize(self):
            return None

        async def call_tool(self, name, arguments, read_timeout_seconds):
            assert read_timeout_seconds.total_seconds() == 20
            calls.append((name, arguments))
            return CallToolResult(
                content=[TextContent(type="text", text='{"remote":"unchanged"}')],
                structuredContent={"remote": "unchanged"},
                isError=name == "seloa_search",
            )

    monkeypatch.setattr(adapter_module, "streamable_http_client", fake_transport)
    monkeypatch.setattr(adapter_module, "ClientSession", FakeSession)
    return calls, seen_headers


async def test_exact_ten_tools_are_listed():
    names = {tool.name for tool in await server.mcp.list_tools() if tool.name.startswith("seloa_")}
    assert names == adapter_module.TOOL_NAMES


def test_core_catalog_marks_seloa_writes_as_gated():
    catalog = {tool["name"]: tool for tool in available_tools()}
    writes = {"seloa_create", "seloa_update", "seloa_delete", "seloa_undo"}
    for name in adapter_module.TOOL_NAMES:
        assert catalog[name]["gated"] is (name in writes)
        assert catalog[name]["locked"] is (name in writes)


async def test_read_proxy_preserves_remote_result_and_error(remote):
    calls, headers = remote
    result = await server.seloa_today("2026-09-25")
    assert result.model_dump(mode="json", by_alias=True, exclude_none=True) == {
        "content": [{"type": "text", "text": '{"remote":"unchanged"}'}],
        "structuredContent": {"remote": "unchanged"},
        "isError": False,
    }
    error = await server.seloa_search("meeting")
    assert error.isError is True
    assert error.content[0].text == '{"remote":"unchanged"}'
    outer = await server.mcp.call_tool("seloa_search", {"query": "meeting"})
    assert outer.isError is True
    assert calls == [
        ("seloa_today", {"date": "2026-09-25"}),
        ("seloa_search", {"query": "meeting"}),
        ("seloa_search", {"query": "meeting"}),
    ]
    assert headers == ["Bearer fake-private-token"] * 3


async def test_other_read_argument_mapping(remote):
    calls, _ = remote
    await server.seloa_between("2026-09-01", "2026-09-25")
    await server.seloa_tasks(status="open", limit=10)
    await server.seloa_overview()
    await server.seloa_changes(limit=5)
    assert calls == [
        ("seloa_between", {"from": "2026-09-01", "to": "2026-09-25"}),
        ("seloa_tasks", {"status": "open", "limit": 10}),
        ("seloa_overview", {}),
        ("seloa_changes", {"limit": 5}),
    ]


async def test_create_requires_direct_request_or_confirmation(remote):
    calls, _ = remote
    blocked = await server.seloa_create("Codex", "task", "draft")
    assert blocked.structuredContent["error"]["code"] == "confirmation_required"
    assert calls == []
    await server.seloa_create("Codex", "task", "direct", user_directed=True)
    await server.seloa_create("Codex", "event", "approved", date="2026-09-25", user_confirmed=True)
    assert calls == [
        ("seloa_create", {"agent": "Codex", "kind": "task", "title": "direct"}),
        ("seloa_create", {"agent": "Codex", "kind": "event", "title": "approved", "date": "2026-09-25"}),
    ]


async def test_update_delete_and_undo_confirmation_boundaries(remote):
    calls, _ = remote
    changes = server.SeloaUpdateFields(notes=None, status="completed")
    assert (await server.seloa_update("Codex", "task-1", changes)).structuredContent["error"]["code"] == "confirmation_required"
    assert (await server.seloa_delete("Codex", "task-1")).structuredContent["error"]["code"] == "confirmation_required"
    assert (await server.seloa_undo("action-1")).structuredContent["error"]["code"] == "explicit_request_required"
    assert calls == []
    await server.seloa_update("Codex", "task-1", changes, user_confirmed=True)
    await server.seloa_delete("Codex", "task-1", user_confirmed=True)
    await server.seloa_undo("action-1", user_directed=True)
    assert calls == [
        ("seloa_update", {"agent": "Codex", "id": "task-1", "notes": None, "status": "completed"}),
        ("seloa_delete", {"agent": "Codex", "id": "task-1"}),
        ("seloa_undo", {"actionId": "action-1"}),
    ]


async def test_fastmcp_parses_typed_update_and_preserves_null(remote):
    calls, _ = remote
    await server.mcp.call_tool(
        "seloa_update",
        {"agent": "Codex", "id": "task-1", "changes": {"notes": None}, "user_confirmed": True},
    )
    assert calls == [("seloa_update", {"agent": "Codex", "id": "task-1", "notes": None})]


async def test_missing_credential_disables_without_network(monkeypatch, remote):
    calls, _ = remote
    monkeypatch.delenv("SELOA_MCP_TOKEN")
    monkeypatch.setattr(adapter_module, "WindowsTokenStorage", lambda url: type("MissingStore", (), {"has_tokens": lambda self: False})())
    result = await server.seloa_overview()
    assert result.structuredContent["error"]["code"] == "not_connected"
    assert calls == []


async def test_transport_errors_do_not_expose_credentials(monkeypatch, remote):
    calls, _ = remote

    @asynccontextmanager
    async def failing_transport(url, *, http_client):
        raise RuntimeError("fake-private-token appeared in an HTTP exception")
        yield  # pragma: no cover

    monkeypatch.setattr(adapter_module, "streamable_http_client", failing_transport)
    result = await server.seloa_overview()
    assert result.structuredContent["error"]["code"] == "remote_unavailable"
    assert "fake-private-token" not in result.model_dump_json()
    assert calls == []


async def test_total_timeout_is_bounded(monkeypatch, remote):
    calls, _ = remote

    @asynccontextmanager
    async def stalled_transport(url, *, http_client):
        await asyncio.sleep(1)
        yield  # pragma: no cover

    monkeypatch.setattr(adapter_module, "streamable_http_client", stalled_transport)
    monkeypatch.setattr(adapter_module, "_TIMEOUT_SECONDS", 0.01)
    result = await server.seloa_overview()
    assert result.structuredContent["error"]["code"] == "timeout"
    assert calls == []


async def test_total_timeout_includes_waiting_for_auth_lock(monkeypatch, remote):
    calls, _ = remote
    monkeypatch.setattr(adapter_module, "_TIMEOUT_SECONDS", 0.01)
    async with server.seloa._auth_lock:
        result = await server.seloa_overview()
    assert result.structuredContent["error"]["code"] == "timeout"
    assert calls == []


async def test_invalid_url_never_sends_token(monkeypatch, remote):
    calls, headers = remote
    monkeypatch.setenv("SELOA_MCP_URL", "http://example.test/mcp")
    result = await server.seloa_overview()
    assert result.structuredContent["error"]["code"] == "invalid_config"
    assert calls == headers == []
