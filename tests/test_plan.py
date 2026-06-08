# -*- coding: utf-8 -*-
"""plan 도구 (P4) — 프로토콜 추천 + step 미리보기(dry plan, 실행 안 함)."""
from adapters.memory_adapter import MemoryAdapter
from adapters.n8n_adapter import N8nAdapter
from adapters.notion_adapter import NotionAdapter
from adapters.qdrant_adapter import QdrantAdapter
from adapters.studio_adapter import StudioAdapter
from core.router import SmartRouter
from core.schema_validator import SchemaValidator
from core import tools as T
from core.tools import ToolContext


def _ctx(tmp_path):
    adapters = {
        "notion": NotionAdapter(token=""),
        "memory": MemoryAdapter(base_dir=tmp_path),
        "qdrant": QdrantAdapter(url=""),
        "studio": StudioAdapter(url="", api_key=""),
        "n8n": N8nAdapter(url=""),
    }
    return ToolContext(adapters, SmartRouter(adapters), SchemaValidator())


async def test_plan_recommends_publish_protocol(tmp_path):
    ctx = _ctx(tmp_path)
    env = await T.tool_plan(ctx, "수집한 URL 을 요약해서 블로그에 발행하고 싶다")
    assert env["data"]["recommended"] == "ingest_summarize_publish"
    # step 미리보기(실행 안 함) — tool/gate 명시
    steps = env["data"]["steps"]
    assert [s["tool"] for s in steps] == ["ingest", "create", "publish"]
    assert env["data"]["gates"] == [2]            # publish 직전 게이트
    assert env["data"]["dry_run"] is True


async def test_plan_recommends_decision_protocol(tmp_path):
    ctx = _ctx(tmp_path)
    env = await T.tool_plan(ctx, "이 자료들로 의사결정을 내려야 한다")
    assert env["data"]["recommended"] == "resource_to_decision"
    assert any(c["protocol"] == "resource_to_decision" for c in env["data"]["candidates"])


async def test_plan_is_dry_no_execution(tmp_path):
    """plan 은 어떤 도구도 실행하지 않는다 — 승인큐/실행저널 무변화."""
    ctx = _ctx(tmp_path)
    await T.tool_plan(ctx, "블로그 발행")
    assert ctx.approvals_store.list_pending() == []
    assert ctx.runs_store.all() == []             # 실행 저널 비어있음
    assert ctx.links_store.find(relation="published_as") == []


async def test_plan_no_match_lists_candidates(tmp_path):
    ctx = _ctx(tmp_path)
    env = await T.tool_plan(ctx, "오늘 점심 뭐 먹지")
    assert env["data"]["recommended"] is None     # 명확한 매칭 없음
    assert len(env["data"]["candidates"]) == 2    # 그래도 후보 전체 제공
    assert env["data"]["steps"] == []
