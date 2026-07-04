# -*- coding: utf-8 -*-
"""Protocol Engine (P4) — 체인 성공 / 중단(부분결과) / 멱등."""
from adapters.memory_adapter import MemoryAdapter
from adapters.n8n_adapter import N8nAdapter
from adapters.notion_adapter import NotionAdapter
from adapters.qdrant_adapter import QdrantAdapter
from adapters.studio_adapter import StudioAdapter
from core.embeddings import HashEmbedder
from core.router import SmartRouter
from core.schema_validator import SchemaValidator
from core import protocols as P
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


def _envelope_shape(env):
    assert set(env) >= {"data", "verification", "provenance"}
    assert "schema_valid" in env["verification"]
    assert "sources_used" in env["provenance"]


async def _fake_fetch(url, client=None):
    return ("어텐션 메커니즘 정리", "본문 텍스트 트랜스포머 어텐션")


# ── 등록/미리보기 ───────────────────────────────────────────────
def test_registry_and_preview():
    assert "ingest_summarize_publish" in P.list_protocols()
    assert "resource_to_decision" in P.list_protocols()
    steps = P.preview("ingest_summarize_publish")
    assert [s["tool"] for s in steps] == ["ingest", "create", "publish"]
    assert steps[2]["gate"] is True and steps[0]["gate"] is False
    assert P.preview("nope") == []


# ── 체인 성공 (게이트 없는 resource_to_decision) ────────────────
async def test_chain_success_resource_to_decision(tmp_path):
    ctx = _ctx(tmp_path)
    env = await T.tool_run_action(ctx, "resource_to_decision", {"query": "RTK 도입 결정"})
    _envelope_shape(env)
    assert env["data"]["status"] == "completed"
    labels = [s["step"] for s in env["data"]["steps"]]
    assert labels == ["search", "context", "decision"]
    assert env.get("completed") is True
    # reasoning_steps 누적(각 step 로그)
    assert any("step0 search" in s for s in env["provenance"]["reasoning_steps"])
    # DECISION 이 memory 에 실제로 기록됨
    found = await ctx.adapters["memory"].search("결정", {"type": "decision"})
    assert found and found[0]["data"]["status"] == "제안"


# ── 중단 + 부분결과 (ingest fetch 실패 → step0 에서 멈춤) ────────
async def test_chain_abort_partial(tmp_path, monkeypatch):
    async def boom(url, client=None):
        raise ValueError("fetch 강제 실패")
    monkeypatch.setattr(T, "_fetch_url", boom)

    ctx = _ctx(tmp_path)
    env = await T.tool_run_action(ctx, "ingest_summarize_publish", {"url": "https://example.com/x"})
    _envelope_shape(env)
    assert env["data"]["status"] == "aborted"
    assert env["data"]["failed_step_index"] == 0       # 중단 지점 명시
    assert env["data"]["failed_tool"] == "ingest"
    assert env.get("aborted") is True
    assert env["verification"]["schema_valid"] is False
    assert env["errors"]
    # 게이트 도달 전에 멈췄으므로 승인큐는 비어있다
    assert ctx.approvals_store.list_pending() == []


# ── 중단: 게이트 이후 step 실패는 부분결과 (publish 실패 가정) ───
async def test_chain_abort_at_publish(tmp_path, monkeypatch):
    monkeypatch.setattr(T, "_fetch_url", _fake_fetch)
    ctx = _ctx(tmp_path)
    pend = await T.tool_run_action(ctx, "ingest_summarize_publish", {"url": "https://example.com/p"})
    rid = pend["data"]["run_id"]

    # studio.publish 가 None 산출(=하드 실패) 하도록 강제 → 승인 후 publish step 에서 중단
    async def fail_publish(ctx_, summary):
        from core import verify as V
        return V.make_envelope(None, sources_used=[], schema_valid=None, errors=["studio 강제 실패"])
    monkeypatch.setattr(T, "tool_publish", fail_publish)

    env = await T.tool_approve(ctx, rid, "approve")
    assert env["data"]["status"] == "aborted"
    assert env["data"]["failed_tool"] == "publish"
    assert env["data"]["failed_step_index"] == 2


# ── 멱등: 같은 run_id 재실행 → 완료 step 건너뛰고 저장 봉투 replay ─
async def test_idempotent_replay(tmp_path):
    ctx = _ctx(tmp_path)
    first = await T.tool_run_action(ctx, "resource_to_decision", {"query": "멱등 테스트"})
    second = await T.tool_run_action(ctx, "resource_to_decision", {"query": "멱등 테스트"})
    assert first["data"]["status"] == "completed"
    assert second.get("idempotent_replay") is True
    assert second["data"]["run_id"] == first["data"]["run_id"]
    # 저널은 같은 run_id 로 누적되지만 결과 봉투는 동일
    assert second["data"]["result"]["id"] == first["data"]["result"]["id"]


# ── 재시도: aborted run 재호출 = replay 가 아니라 실패 step 부터 재개 ─
async def test_aborted_run_resumes_from_failed_step(tmp_path, monkeypatch):
    """aborted 저널은 종결(replay 대상)이 아니라 재시도 가능 상태다.

    회귀 가드: aborted 를 idempotent replay 하면 트리거 재발화의 '실패 prior 는
    체인 재실행' 계약(core/triggers.py fire)이 무효화되어 일시 장애가 영구 장애로
    굳는다. 재시도는 완료 step 을 건너뛰고(멱등) 실패 step 부터 재개한다.
    """
    search_calls = {"n": 0}
    context_calls = {"n": 0}
    real_search = T.tool_search
    real_get_context = T.tool_get_context

    async def counting_search(ctx_, query, opts=None):
        search_calls["n"] += 1
        return await real_search(ctx_, query, opts)

    async def flaky_get_context(ctx_, query, opts=None):
        context_calls["n"] += 1
        if context_calls["n"] == 1:
            raise RuntimeError("일시 장애")
        return await real_get_context(ctx_, query, opts)

    monkeypatch.setattr(T, "tool_search", counting_search)
    monkeypatch.setattr(T, "tool_get_context", flaky_get_context)

    ctx = _ctx(tmp_path)
    first = await T.tool_run_action(ctx, "resource_to_decision", {"query": "재시도 검증"})
    assert first["data"]["status"] == "aborted"
    assert first["data"]["failed_step_index"] == 1

    second = await T.tool_run_action(ctx, "resource_to_decision", {"query": "재시도 검증"})
    assert second.get("idempotent_replay") is None    # aborted 봉투 replay 금지
    assert second["data"]["status"] == "completed"    # 실패 step 부터 재개 → 완주
    assert context_calls["n"] == 2                    # 실패 step 은 재실행됨
    assert search_calls["n"] == 1                     # 완료 step 은 재실행 안 함(멱등)

    third = await T.tool_run_action(ctx, "resource_to_decision", {"query": "재시도 검증"})
    assert third.get("idempotent_replay") is True     # 완주 후엔 done 봉투 replay


# ── 명시 run_id 멱등 키 ─────────────────────────────────────────
async def test_explicit_run_id(tmp_path):
    ctx = _ctx(tmp_path)
    env = await T.tool_run_action(ctx, "resource_to_decision", {"query": "q", "run_id": "myrun"})
    assert env["data"]["run_id"] == "myrun"


# ── 미등록 프로토콜은 run_protocol 에서 안내 봉투 ───────────────
async def test_unknown_protocol_envelope(tmp_path):
    ctx = _ctx(tmp_path)
    env = await P.run_protocol(ctx, "no_such_proto", {})
    assert env["data"]["status"] == "unknown_protocol"
    assert "ingest_summarize_publish" in env["data"]["known_protocols"]
    assert env["errors"]
