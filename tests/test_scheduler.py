# -*- coding: utf-8 -*-
"""스케줄러 추상화 (P5) — 트리거 등록·조회 / run_trigger 정책 경유."""
import json

from adapters.memory_adapter import MemoryAdapter
from adapters.n8n_adapter import N8nAdapter
from adapters.notion_adapter import NotionAdapter
from adapters.qdrant_adapter import QdrantAdapter
from adapters.studio_adapter import StudioAdapter
from core.embeddings import HashEmbedder
from core.router import SmartRouter
from core.scheduler import Scheduler
from core.schema_validator import SchemaValidator
from core import tools as T
from core.tools import ToolContext

# 격리된 트리거 정의(테스트 전용) — auto/strict/manual 3종
_TRIGGERS = {
    "triggers": [
        {
            "id": "auto_demo", "kind": "manual", "protocol": "ingest_summarize_publish",
            "params": {"url": "https://example.com/auto"},
            "policy": {"auto_approve_when": ["dry_run_high_quality"],
                       "always_gate": ["external_publish"], "max_publishes_per_day": 20},
        },
        {
            "id": "strict_demo", "kind": "webhook", "protocol": "ingest_summarize_publish",
            "params": {"url": "https://example.com/strict"},
            "policy": {"auto_approve_when": [], "always_gate": ["is_publish"]},
        },
        {
            "id": "decision_demo", "kind": "manual", "protocol": "resource_to_decision",
            "params": {"query": "테스트 결정"},
        },
    ]
}


def _ctx(tmp_path):
    tpath = tmp_path / "triggers.json"
    tpath.write_text(json.dumps(_TRIGGERS, ensure_ascii=False), encoding="utf-8")
    adapters = {
        "notion": NotionAdapter(token=""),
        "memory": MemoryAdapter(base_dir=tmp_path),
        "qdrant": QdrantAdapter(url=None, embedder=HashEmbedder()),
        "studio": StudioAdapter(url="", api_key=""),
        "n8n": N8nAdapter(url=""),
    }
    sched = Scheduler(triggers_path=tpath, runlog_path=tmp_path / "trigger_runs.jsonl")
    return ToolContext(adapters, SmartRouter(adapters), SchemaValidator(), scheduler=sched)


async def _fake_fetch(url, client=None):
    return ("어텐션 메커니즘 정리", "본문 트랜스포머 어텐션")


# ── 등록/조회 ───────────────────────────────────────────────────
async def test_list_triggers(tmp_path):
    ctx = _ctx(tmp_path)
    env = await T.tool_list_triggers(ctx)
    ids = [t["id"] for t in env["data"]["triggers"]]
    assert set(ids) == {"auto_demo", "strict_demo", "decision_demo"}
    auto = next(t for t in env["data"]["triggers"] if t["id"] == "auto_demo")
    assert auto["kind"] == "manual" and auto["has_policy"] is True


async def test_run_trigger_unknown(tmp_path):
    ctx = _ctx(tmp_path)
    env = await T.tool_run_trigger(ctx, "no_such_trigger")
    assert env["data"]["status"] == "unknown_trigger"
    assert "auto_demo" in env["data"]["known_triggers"]
    assert env["errors"]


# ── run_trigger 정책 경유 ───────────────────────────────────────
async def test_run_trigger_auto_approve(tmp_path, monkeypatch):
    """DoD4 — run_trigger(manual)가 정책 경유해 프로토콜 실행(자동승인 완주)."""
    monkeypatch.setattr(T, "_fetch_url", _fake_fetch)
    ctx = _ctx(tmp_path)
    env = await T.tool_run_trigger(ctx, "auto_demo")
    assert env["data"]["status"] == "completed"
    assert env["data"]["auto_approved"] is True
    assert env["trigger"] == {"id": "auto_demo", "kind": "manual"}
    # 실행 이력 기록됨
    runs = ctx.scheduler.runlog.all()
    assert runs and runs[-1]["trigger_id"] == "auto_demo" and runs[-1]["status"] == "completed"
    # 정책 감사 로그도 남음(트리거 정책이 ctx.policy 로그 공유)
    assert ctx.policy.log.count_auto_publishes_today() == 1


async def test_run_trigger_strict_gate(tmp_path, monkeypatch):
    """엄격 트리거(always_gate=is_publish) → 정책상 무조건 승인큐 폴백."""
    monkeypatch.setattr(T, "_fetch_url", _fake_fetch)
    ctx = _ctx(tmp_path)
    env = await T.tool_run_trigger(ctx, "strict_demo")
    assert env["data"]["status"] == "pending_approval"
    assert env["data"]["policy_decision"]["rule"] == "is_publish"
    assert len(ctx.approvals_store.list_pending()) == 1


async def test_run_trigger_params_override(tmp_path):
    """게이트 없는 트리거 + params override 병합 → 완주."""
    ctx = _ctx(tmp_path)
    env = await T.tool_run_trigger(ctx, "decision_demo", {"query": "오버라이드 결정"})
    assert env["data"]["status"] == "completed"
    # override 가 트리거 기본 params 를 덮어 결정 초안에 반영
    assert "오버라이드 결정" in env["data"]["result"]["data"]["title"]


# ── Scheduler 단위 ──────────────────────────────────────────────
def test_scheduler_get_and_missing_file(tmp_path):
    ctx = _ctx(tmp_path)
    assert ctx.scheduler.get("auto_demo")["protocol"] == "ingest_summarize_publish"
    assert ctx.scheduler.get("nope") is None
    # 파일 없으면 빈 목록(예외 없음)
    empty = Scheduler(triggers_path=tmp_path / "missing.json")
    assert empty.load() == [] and empty.list() == []


def test_repo_triggers_json_loads():
    """리포 커밋된 triggers.json 이 유효하게 로드된다."""
    sched = Scheduler()  # ROOT/triggers.json
    ids = [t["id"] for t in sched.list()]
    assert "auto_publish_demo" in ids and "strict_publish" in ids
