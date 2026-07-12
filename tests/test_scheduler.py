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


def _ctx(tmp_path, triggers=None, studio=None):
    tpath = tmp_path / "triggers.json"
    tpath.write_text(json.dumps(triggers or _TRIGGERS, ensure_ascii=False), encoding="utf-8")
    adapters = {
        "notion": NotionAdapter(token=""),
        "memory": MemoryAdapter(base_dir=tmp_path),
        "qdrant": QdrantAdapter(url=None, embedder=HashEmbedder()),
        "studio": studio if studio is not None else StudioAdapter(url="", api_key=""),
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


# ── run_trigger P7 형식(target.chain, protocol 키 없음) ─────────
# 리포 triggers.json 의 hourly_ingest/nightly_full_loop 와 동형 — 과거 run_trigger 가
# trig["protocol"] 직접 인덱싱으로 KeyError 'protocol' 즉사하던 회귀 가드.
_P7_TRIGGERS = {
    "triggers": [
        {
            "id": "p7_ingest", "type": "interval", "enabled": True,
            "schedule": {"every_sec": 3600},
            "target": {"chain": "ingest_summarize"},
            "params": {"url": "https://example.com/feed"},
        },
        {
            "id": "p7_full_loop", "type": "daily", "enabled": True,
            "schedule": {"at": "02:30", "tz": "Asia/Seoul"},
            "target": {"chain": "full_loop"},
            "params": {"url": "https://example.com/nightly"},
            "policy": {"publish_mode": "dry_run", "require_approval": True},
        },
    ]
}


async def test_run_trigger_p7_inline_chain(tmp_path, monkeypatch):
    """P7 target.chain=ingest_summarize → 인라인 체인 완주(표준 봉투, 예외 없음)."""
    monkeypatch.setattr(T, "_fetch_url", _fake_fetch)
    ctx = _ctx(tmp_path, triggers=_P7_TRIGGERS)
    env = await T.tool_run_trigger(ctx, "p7_ingest")
    assert env["data"]["status"] == "completed"
    assert env["data"]["chain"] == "ingest_summarize"
    assert env["trigger"]["id"] == "p7_ingest"
    # 실행 이력에 해소된 체인이 기록됨(protocol 키 없어도 None 아님)
    runs = ctx.scheduler.runlog.all()
    assert runs and runs[-1]["trigger_id"] == "p7_ingest"
    assert runs[-1]["status"] == "completed" and runs[-1]["protocol"] == "ingest_summarize"


async def test_run_trigger_p7_full_loop_gate(tmp_path, monkeypatch):
    """P7 target.chain=full_loop → 프로토콜 해소 실행 + require_approval 이라 승인큐 폴백."""
    monkeypatch.setattr(T, "_fetch_url", _fake_fetch)
    ctx = _ctx(tmp_path, triggers=_P7_TRIGGERS)
    env = await T.tool_run_trigger(ctx, "p7_full_loop")
    assert env["data"]["status"] == "pending_approval"
    # require_approval=True 는 TriggerEngine 과 같은 번역(always_gate=is_publish) — 진입점 정합
    assert env["data"]["policy_decision"]["rule"] == "is_publish"
    assert len(ctx.approvals_store.list_pending()) == 1
    runs = ctx.scheduler.runlog.all()
    assert runs and runs[-1]["protocol"] == "ingest_summarize_publish"


# ── run_trigger 선언 publish_mode 정합(YOHA-5 회귀 가드) ────────
async def test_run_trigger_honors_declared_publish_mode_dry_run(tmp_path, monkeypatch):
    """publish_mode=dry_run 선언 트리거는 run_trigger 진입점에서도 드라이런 완주 — 실파일 0.

    회귀(YOHA-5): run_trigger 가 선언 publish_mode·policy 번역을 TriggerEngine 과
    공유하지 않아, 같은 트리거가 엔진 발화(fire)로는 드라이런 완주하는데 run_trigger
    로는 env 모드(file)로 게이트 pending → 승인 시 실파일이 써졌다(승인자는 dry_run
    트리거로 알고 승인). 이제 선언 해석은 dispatch_declared_trigger 단일 지점 공유.
    """
    monkeypatch.setattr(T, "_fetch_url", _fake_fetch)
    repo = tmp_path / "repo"
    # 위험 구성 재현: studio 는 env(file) 모드 + 실제 repo_path — 실발행 가능 상태
    studio = StudioAdapter(repo_path=str(repo), mode="file", journal_path=tmp_path / "pub.jsonl")
    trig = {"id": "dry_declared", "type": "daily", "enabled": True, "schedule": {"at": "00:00"},
            "target": {"chain": "full_loop"}, "params": {"url": "https://e.com/dry"},
            "policy": {"publish_mode": "dry_run", "require_approval": False}}
    ctx = _ctx(tmp_path, triggers={"triggers": [trig]}, studio=studio)

    env = await T.tool_run_trigger(ctx, "dry_declared")
    # TriggerEngine.fire(test_full_loop_unmanned_dry_run)와 동일 결과 — 무인 드라이런 완주
    assert env["data"]["status"] == "completed"
    assert env["data"]["auto_approved"] is True
    assert env["dry_run"] is True
    assert ctx.approvals_store.list_pending() == []          # 승인 큐 폴백 없음
    blog = repo / "src" / "content" / "blog"
    assert not blog.exists() or not list(blog.glob("*.mdx"))  # 실파일 0 (선언 dry_run 준수)
    assert studio.mode == "file"                              # 실행 후 env 모드 복원(누수 없음)


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


def test_repo_triggers_json_all_new_form():
    """리포 triggers.json 은 전부 신형(type/target.chain) — legacy 필드(kind/protocol) 0.

    legacy 4건 마이그레이션의 의미 보존 단언: 변환 전 해석(chain/type/스케줄/정책)과 동일.
    Scheduler.list() 는 type/target/policy 를 표면화하지 않으므로 원본 파일을 직접 검사한다.
    """
    from pathlib import Path

    from core.triggers import _chain_of, _daily_at, _protocol_of, _trigger_type, validate_trigger

    raw = json.loads((Path(__file__).resolve().parents[1] / "triggers.json").read_text("utf-8"))
    by = {t["id"]: t for t in raw["triggers"]}
    for tid, t in by.items():
        assert "kind" not in t and "protocol" not in t, f"{tid}: legacy 필드 잔존"
        assert t.get("type") in {"interval", "daily", "webhook", "manual"}, tid
        assert (t.get("target") or {}).get("chain"), f"{tid}: target.chain 없음"
        ok, reason = validate_trigger(t)
        assert ok, f"{tid}: {reason}"
    # morning_publish — legacy cron "57 8 * * *" 과 동일 해석: daily 08:57 KST, full_loop
    mp = by["morning_publish"]
    assert _trigger_type(mp) == "daily" and _daily_at(mp) == (8, 57)
    assert _chain_of(mp) == "full_loop" and _protocol_of(mp) == "ingest_summarize_publish"
    assert mp["policy"]["max_publishes_per_day"] == 5  # 명시 정책 무손실(프리셋 환산 금지)
    # strict_publish — 보안 게이트 보존: 모든 발행 무조건 사람 승인
    sp = by["strict_publish"]
    assert _trigger_type(sp) == "webhook" and sp["event"]["name"] == "publish_request"
    assert sp["policy"]["always_gate"] == ["is_publish"]
    assert sp["policy"]["max_publishes_per_day"] == 0
    # draft_decision/auto_publish_demo — chain·타입 동등
    assert _chain_of(by["draft_decision"]) == "resource_to_decision"
    assert _trigger_type(by["draft_decision"]) == "manual"
    assert _chain_of(by["auto_publish_demo"]) == "full_loop"
    assert _trigger_type(by["auto_publish_demo"]) == "manual"
