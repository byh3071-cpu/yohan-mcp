# -*- coding: utf-8 -*-
"""정책 엔진 (P5) — auto_approve / 일일한도 폴백 / always_gate 강제 / 감사로그."""
from adapters.memory_adapter import MemoryAdapter
from adapters.n8n_adapter import N8nAdapter
from adapters.notion_adapter import NotionAdapter
from adapters.qdrant_adapter import QdrantAdapter
from adapters.studio_adapter import StudioAdapter
from core.embeddings import HashEmbedder
from core.policy import PolicyEngine, PolicyLog, DEFAULT_POLICY, RECOMMENDED_AUTO_POLICY
from core.router import SmartRouter
from core.schema_validator import SchemaValidator
from core import tools as T
from core.tools import ToolContext


def _ctx(tmp_path, studio=None):
    adapters = {
        "notion": NotionAdapter(token=""),
        "memory": MemoryAdapter(base_dir=tmp_path),
        "qdrant": QdrantAdapter(url=None, embedder=HashEmbedder()),
        "studio": studio if studio is not None else StudioAdapter(url="", api_key=""),
        "n8n": N8nAdapter(url=""),
    }
    return ToolContext(adapters, SmartRouter(adapters), SchemaValidator())


async def _fake_fetch(url, client=None):
    return ("어텐션 메커니즘 정리", "본문 트랜스포머 어텐션")


# ── PolicyEngine.decide 단위 ────────────────────────────────────
def test_decide_auto_approve(tmp_path):
    eng = PolicyEngine(RECOMMENDED_AUTO_POLICY, log_path=tmp_path / "p.jsonl")
    d = eng.decide({"tool": "publish", "dry_run": True, "quality_score": 6, "step_index": 2})
    assert d["auto_approve"] is True and d["rule"] == "dry_run_high_quality"


def test_decide_default_is_conservative(tmp_path):
    """기본 정책은 자동승인 없음 — 드라이런 고품질이어도 사람 승인(P4 동등)."""
    eng = PolicyEngine(DEFAULT_POLICY, log_path=tmp_path / "p.jsonl")
    d = eng.decide({"tool": "publish", "dry_run": True, "quality_score": 6, "step_index": 2})
    assert d["auto_approve"] is False and d["rule"] is None


def test_decide_always_gate_wins_over_auto(tmp_path):
    """always_gate 는 auto_approve 보다 우선 — 외부 실발행은 무조건 게이트."""
    eng = PolicyEngine(RECOMMENDED_AUTO_POLICY, log_path=tmp_path / "p.jsonl")
    d = eng.decide({"tool": "publish", "dry_run": True, "external_publish": True,
                    "quality_score": 6, "step_index": 2})
    assert d["auto_approve"] is False and d["rule"] == "external_publish"


def test_decide_daily_limit_fallback(tmp_path):
    eng = PolicyEngine({**RECOMMENDED_AUTO_POLICY, "max_publishes_per_day": 0},
                       log_path=tmp_path / "p.jsonl")
    d = eng.decide({"tool": "publish", "dry_run": True, "quality_score": 6, "step_index": 2})
    assert d["auto_approve"] is False and d["rule"] == "max_publishes_per_day"


def test_decide_max_actions_per_run(tmp_path):
    eng = PolicyEngine({**RECOMMENDED_AUTO_POLICY, "max_actions_per_run": 1},
                       log_path=tmp_path / "p.jsonl")
    d = eng.decide({"tool": "publish", "dry_run": True, "quality_score": 6, "step_index": 2})
    assert d["auto_approve"] is False and d["rule"] == "max_actions_per_run"


def test_audit_log_and_daily_count(tmp_path):
    log = PolicyLog(tmp_path / "policy_log.jsonl")
    eng = PolicyEngine(RECOMMENDED_AUTO_POLICY, log=log)
    eng.decide({"tool": "publish", "dry_run": True, "quality_score": 6, "step_index": 2, "run_id": "r1"})
    eng.decide({"tool": "publish", "dry_run": True, "quality_score": 6, "step_index": 2, "run_id": "r2"})
    entries = log.all()
    assert len(entries) == 2
    assert entries[0]["rule"] == "dry_run_high_quality"
    assert entries[0]["facts"]["quality_score"] == 6
    assert log.count_auto_publishes_today() == 2          # 자동 발행만 카운트
    # 폴백(auto_approve=False)은 카운트되지 않음
    eng.decide({"tool": "publish", "dry_run": False, "external_publish": True, "step_index": 2})
    assert log.count_auto_publishes_today() == 2


# ── 통합: run_action 정책 경유 ──────────────────────────────────
async def test_run_action_auto_approve_completes(tmp_path, monkeypatch):
    """DoD1 — 드라이런 고품질 → 사람 승인 없이 auto_approve 완주(봉투에 policy_rule)."""
    monkeypatch.setattr(T, "_fetch_url", _fake_fetch)
    ctx = _ctx(tmp_path)
    pol = PolicyEngine(RECOMMENDED_AUTO_POLICY, log=ctx.policy.log)
    env = await T.tool_run_action(ctx, "ingest_summarize_publish",
                                  {"url": "https://example.com/a"}, policy=pol)
    assert env["data"]["status"] == "completed"
    assert env["data"]["auto_approved"] is True
    assert env["data"]["policy_rule"] == "dry_run_high_quality"
    assert env["dry_run"] is True
    # 사람 승인큐는 비어있다(무인 완주)
    assert ctx.approvals_store.list_pending() == []
    # 감사 로그 기록됨
    assert ctx.policy.log.count_auto_publishes_today() == 1


async def test_run_action_external_publish_always_gate(tmp_path, monkeypatch):
    """DoD2 — 외부 실발행 step → always_gate → 무조건 승인큐 폴백."""
    monkeypatch.setattr(T, "_fetch_url", _fake_fetch)
    # (P6) 외부 실발행 능력 = STUDIO_REPO_PATH + mode=file → can_publish True
    studio = StudioAdapter(repo_path=str(tmp_path / "studio_repo"), mode="file",
                           journal_path=tmp_path / "pub.jsonl")
    ctx = _ctx(tmp_path, studio=studio)
    pol = PolicyEngine(RECOMMENDED_AUTO_POLICY, log=ctx.policy.log)
    env = await T.tool_run_action(ctx, "ingest_summarize_publish",
                                  {"url": "https://example.com/b"}, policy=pol)
    assert env["data"]["status"] == "pending_approval"
    assert env["data"]["policy_decision"]["rule"] == "external_publish"
    assert len(ctx.approvals_store.list_pending()) == 1
    # 외부 실발행은 일어나지 않음(게이트에서 정지)
    assert ctx.links_store.find(relation="published_as") == []


async def test_run_action_daily_limit_fallback(tmp_path, monkeypatch):
    """DoD3 — 일일 발행 한도 초과 → 자동승인 거부 → 승인큐 폴백."""
    monkeypatch.setattr(T, "_fetch_url", _fake_fetch)
    ctx = _ctx(tmp_path)
    pol = PolicyEngine({**RECOMMENDED_AUTO_POLICY, "max_publishes_per_day": 0}, log=ctx.policy.log)
    env = await T.tool_run_action(ctx, "ingest_summarize_publish",
                                  {"url": "https://example.com/c"}, policy=pol)
    assert env["data"]["status"] == "pending_approval"
    assert env["data"]["policy_decision"]["rule"] == "max_publishes_per_day"


async def test_default_ctx_policy_keeps_human_gate(tmp_path, monkeypatch):
    """회귀 보증 — 기본 ctx 정책(보수적)은 P4 처럼 사람 게이트로 정지한다."""
    monkeypatch.setattr(T, "_fetch_url", _fake_fetch)
    ctx = _ctx(tmp_path)
    env = await T.tool_run_action(ctx, "ingest_summarize_publish", {"url": "https://example.com/d"})
    assert env["data"]["status"] == "pending_approval"   # 자동승인 없음(opt-in)


async def test_get_policy_tool(tmp_path):
    ctx = _ctx(tmp_path)
    env = await T.tool_get_policy(ctx)
    assert "external_publish" in env["data"]["policy"]["always_gate"]
    assert env["data"]["publishes_today"] == 0
    assert "dry_run_high_quality" in env["data"]["rules_available"]
