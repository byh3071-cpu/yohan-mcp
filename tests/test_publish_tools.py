# -*- coding: utf-8 -*-
"""publish/run_action 도구 + 인스턴스 링크 기록 (P3)."""
from adapters.memory_adapter import MemoryAdapter
from adapters.n8n_adapter import N8nAdapter
from adapters.notion_adapter import NotionAdapter
from adapters.qdrant_adapter import QdrantAdapter
from adapters.studio_adapter import StudioAdapter
from core.router import SmartRouter
from core.schema_validator import SchemaValidator
from core import tools as T
from core.tools import ToolContext

SUMMARY = {
    "summary_id": "sum_1", "resource_id": "res_1", "title": "어텐션 정리",
    "summary": "어텐션은 토큰 관련도를 계산한다.", "key_insights": ["멀티헤드 표현 다양성"],
    "domain": "AI", "status": "완료", "created_at": "2026-06-08T09:30:00+09:00",
}


def _ctx(tmp_path):
    adapters = {
        "notion": NotionAdapter(token=""),
        "memory": MemoryAdapter(base_dir=tmp_path),
        "qdrant": QdrantAdapter(url=""),
        "studio": StudioAdapter(url="", api_key=""),  # 드라이런
        "n8n": N8nAdapter(url=""),
    }
    return ToolContext(adapters, SmartRouter(adapters), SchemaValidator())


def _envelope_shape(env):
    assert set(env) >= {"data", "verification", "provenance"}
    assert "schema_valid" in env["verification"]
    assert "sources_used" in env["provenance"]


async def test_publish_dry_run_records_link(tmp_path):
    ctx = _ctx(tmp_path)
    env = await T.tool_publish(ctx, SUMMARY)
    _envelope_shape(env)
    assert env["dry_run"] is True and env["published"] is False
    assert env["data"]["status"] == "초안"          # 드라이런 POST
    assert env["data"]["summary_id"] == "sum_1"     # published_as FK
    # 풀 verification 블록(6항목 점수) 포함
    ver = env["verification"]
    assert isinstance(ver["quality_score"], int)
    assert ver["cross_links_intact"] is True
    # published_as 인스턴스 링크가 런타임 저장소에 기록됨 (schema _links.json 불오염)
    assert env["published_as"]["relation"] == "published_as"
    found = ctx.links_store.find(relation="published_as")
    assert found and found[0]["target"] == "studio:post:post_sum_1"


async def test_publish_does_not_pollute_schema_links(tmp_path):
    ctx = _ctx(tmp_path)
    before = len(ctx._links())
    await T.tool_publish(ctx, SUMMARY)
    after = len(ctx._links())
    assert before == after  # schema 타입수준 _links.json 은 그대로


async def test_publish_unresolvable_summary_errors(tmp_path):
    ctx = _ctx(tmp_path)
    env = await T.tool_publish(ctx, "없는_요약_id")  # notion 토큰 없음 → 로드 실패
    assert env["data"] is None
    assert env["errors"] and "요약 로드 실패" in env["errors"][0]


async def test_run_action_publish_summary_protocol(tmp_path):
    ctx = _ctx(tmp_path)
    env = await T.tool_run_action(ctx, "publish_summary", {"summary": SUMMARY})
    _envelope_shape(env)
    assert env["data"]["protocol"] == "publish_summary"
    assert env["data"]["post"]["summary_id"] == "sum_1"
    assert env["data"]["dry_run"] is True
    assert "reasoning_steps" in env["provenance"]
    # 프로토콜 실행도 링크를 남긴다
    assert ctx.links_store.find(relation="published_as")


async def test_run_action_unknown_is_stub(tmp_path):
    """미등록 프로토콜만 stub — P4 에서 ingest_summarize_publish 는 실동작(엔진 위임)."""
    ctx = _ctx(tmp_path)
    env = await T.tool_run_action(ctx, "definitely_unknown_proto")
    assert env["data"]["status"] == "stub"
    # 등록 목록에 P3 단발 + P4 멀티스텝 프로토콜이 함께 안내됨
    known = env["data"]["known_protocols"]
    assert "publish_summary" in known and "ingest_summarize_publish" in known
