# -*- coding: utf-8 -*-
"""run_action 게이트 (P4) — 되돌리기 어려운 step 직전 pending 봉투 반환 검증."""
from adapters.memory_adapter import MemoryAdapter
from adapters.n8n_adapter import N8nAdapter
from adapters.notion_adapter import NotionAdapter
from adapters.qdrant_adapter import QdrantAdapter
from adapters.studio_adapter import StudioAdapter
from core.embeddings import HashEmbedder
from core.router import SmartRouter
from core.schema_validator import SchemaValidator
from core import tools as T
from core.tools import ToolContext


def _ctx(tmp_path):
    adapters = {
        "notion": NotionAdapter(token=""),
        "memory": MemoryAdapter(base_dir=tmp_path),
        "qdrant": QdrantAdapter(url=None, embedder=HashEmbedder()),
        "studio": StudioAdapter(url="", api_key=""),
        "n8n": N8nAdapter(url=""),
    }
    return ToolContext(adapters, SmartRouter(adapters), SchemaValidator())


async def _fake_fetch(url, client=None):
    return ("어텐션 메커니즘 정리", "본문 텍스트")


async def test_run_action_stops_at_gate(tmp_path, monkeypatch):
    monkeypatch.setattr(T, "_fetch_url", _fake_fetch)
    ctx = _ctx(tmp_path)
    env = await T.tool_run_action(ctx, "ingest_summarize_publish", {"url": "https://example.com/x"})

    # pending 봉투 형태
    assert set(env) >= {"data", "verification", "provenance"}
    d = env["data"]
    assert d["status"] == "pending_approval"
    assert d["run_id"].startswith("run_")
    assert d["awaiting_tool"] == "publish"        # 발행 직전에서 정지
    assert env.get("pending") is True
    assert env["verification"]["schema_valid"] is None   # 미실행 → 미검증
    assert any("GATE" in s for s in env["provenance"]["reasoning_steps"])

    # 승인큐에 적재됨 + 아직 발행 안 됨
    pending = ctx.approvals_store.list_pending()
    assert len(pending) == 1 and pending[0]["run_id"] == d["run_id"]
    assert ctx.links_store.find(relation="published_as") == []   # publish 미실행


async def test_run_action_gate_idempotent_no_dup_queue(tmp_path, monkeypatch):
    """게이트 도달 run 을 재호출해도 승인큐가 중복 적재되지 않는다."""
    monkeypatch.setattr(T, "_fetch_url", _fake_fetch)
    ctx = _ctx(tmp_path)
    e1 = await T.tool_run_action(ctx, "ingest_summarize_publish", {"url": "https://example.com/y"})
    e2 = await T.tool_run_action(ctx, "ingest_summarize_publish", {"url": "https://example.com/y"})
    assert e1["data"]["run_id"] == e2["data"]["run_id"]
    assert e2.get("idempotent_replay") is True
    assert len(ctx.approvals_store.list_pending()) == 1
