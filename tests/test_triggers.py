# -*- coding: utf-8 -*-
"""트리거 실발화 엔진 (P7) — 로드/due/멱등/always_gate/HMAC/실패격리/락/watermark/완결루프."""
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

from adapters.base import make_record
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
from core.triggers import TriggerEngine, TriggerLock, validate_trigger, verify_hmac

_KST = timezone(timedelta(hours=9))


def _ctx(tmp_path, triggers, studio=None):
    tpath = tmp_path / "triggers.json"
    tpath.write_text(json.dumps({"triggers": triggers}, ensure_ascii=False), encoding="utf-8")
    adapters = {
        "notion": NotionAdapter(token=""),
        "memory": MemoryAdapter(base_dir=tmp_path),
        "qdrant": QdrantAdapter(url=None, embedder=HashEmbedder()),
        "studio": studio if studio is not None else StudioAdapter(url="", api_key=""),
        "n8n": N8nAdapter(url=""),
    }
    sched = Scheduler(triggers_path=tpath, runlog_path=tmp_path / "trigger_runs.jsonl")
    return ToolContext(adapters, SmartRouter(adapters), SchemaValidator(), scheduler=sched)


def _engine(ctx, tmp_path, **kw):
    return TriggerEngine(ctx, data_dir=tmp_path / "tdata", sleep=lambda s: None, **kw)


async def _fake_fetch(url, client=None):
    return ("어텐션 메커니즘 정리", "본문 트랜스포머 어텐션")


def _iv(tid, url, every=1):
    return {"id": tid, "type": "interval", "enabled": True,
            "schedule": {"every_sec": every}, "target": {"chain": "ingest_summarize"},
            "params": {"url": url}}


def _tid(env):
    return (env.get("trigger") or {}).get("id") or (env.get("data") or {}).get("trigger_id")


# ── ① 로드/스키마 검증 ─────────────────────────────────────────
def test_validate_trigger_good_and_bad():
    ok, _ = validate_trigger(_iv("x", "https://e.com/x"))
    assert ok
    bad1, r1 = validate_trigger({"id": "y", "type": "interval", "schedule": {}})
    assert not bad1 and "every_sec" in r1
    bad2, _ = validate_trigger({"type": "interval", "schedule": {"every_sec": 1}})  # id 누락
    assert not bad2
    bad3, _ = validate_trigger({"id": "z", "type": "daily", "schedule": {}})       # at 누락
    assert not bad3
    bad4, r4 = validate_trigger({"id": "w", "type": "interval", "schedule": {"every_sec": 1},
                                 "target": {"chain": "no_such_chain"}})
    assert not bad4 and "chain" in r4
    # 시/분 범위초과 daily(자정 오타 '24:00' 포함)는 검증 거부 —
    # 과거엔 (True,'ok')로 통과 후 is_due 에서 ValueError → 트리거 영구 미발화(회귀 가드)
    for sch in ({"at": "25:00"}, {"at": "24:00"}, {"at": "-1:00"}, "0 25 * * *", "61 9 * * *"):
        bad, r = validate_trigger({"id": "oob", "type": "daily", "schedule": sch,
                                   "target": {"chain": "ingest_summarize"}})
        assert not bad, (sch, r)


def test_repo_triggers_all_valid():
    """리포 커밋된 triggers.json 의 모든 트리거가 스키마 검증을 통과한다."""
    for t in Scheduler().load():
        ok, reason = validate_trigger(t)
        assert ok, (t.get("id"), reason)


async def test_tick_isolates_invalid_trigger(tmp_path, monkeypatch):
    monkeypatch.setattr(T, "_fetch_url", _fake_fetch)
    bad = {"id": "b", "type": "interval", "enabled": True, "schedule": {}}  # invalid
    ctx = _ctx(tmp_path, [bad, _iv("g", "https://e.com/g")])
    eng = _engine(ctx, tmp_path)
    res = await eng.tick(datetime(2026, 6, 9, 10, 0, tzinfo=_KST))
    # invalid 는 due 평가에서 걸러져 발화 안 됨, good 만 발화
    assert [_tid(r) for r in res] == ["g"]
    inv = await eng.fire(bad)  # 직접 발화해도 invalid 로 격리
    assert inv["data"]["status"] == "skipped" and inv["data"]["skip_reason"] == "invalid"


# ── ② due 계산(interval / daily) ───────────────────────────────
def test_due_interval(tmp_path):
    trig = _iv("iv", "https://e.com/iv", every=3600)
    eng = _engine(_ctx(tmp_path, [trig]), tmp_path)
    t0 = datetime(2026, 6, 9, 10, 0, tzinfo=_KST)
    assert eng.is_due(trig, t0) is True                 # 미발화 → due
    eng.state.merge("iv", {"last_fire_ts": t0.isoformat()})
    assert eng.is_due(trig, t0 + timedelta(seconds=1800)) is False
    assert eng.is_due(trig, t0 + timedelta(seconds=3601)) is True


def test_due_daily(tmp_path):
    trig = {"id": "dl", "type": "daily", "enabled": True,
            "schedule": {"at": "02:30", "tz": "Asia/Seoul"},
            "target": {"chain": "ingest_summarize"}}
    eng = _engine(_ctx(tmp_path, [trig]), tmp_path)
    assert eng.is_due(trig, datetime(2026, 6, 9, 2, 0, tzinfo=_KST)) is False   # 시각 전
    after = datetime(2026, 6, 9, 2, 31, tzinfo=_KST)
    assert eng.is_due(trig, after) is True
    eng.state.merge("dl", {"last_fire_ts": after.isoformat()})
    assert eng.is_due(trig, datetime(2026, 6, 9, 3, 0, tzinfo=_KST)) is False   # 오늘 이미 발화
    assert eng.is_due(trig, datetime(2026, 6, 10, 2, 31, tzinfo=_KST)) is True  # 다음날


def test_due_daily_out_of_range_not_due_no_crash(tmp_path):
    """범위초과 시/분(자정 오타 '24:00' 포함)은 is_due 가 ValueError 없이 not-due(False)로 처리.

    회귀: 과거 _daily_at 이 범위검증을 안 해 validate_trigger 는 통과하되
    is_due 의 datetime.replace(hour=25) 등이 ValueError → tick 이 매번 격리만 하고
    해당 트리거는 영원히 미발화(다른 트리거는 생존 → 조용한 사망).
    """
    eng = _engine(_ctx(tmp_path, []), tmp_path)
    now = datetime(2026, 6, 9, 23, 59, tzinfo=_KST)
    for sch in ({"at": "25:00"}, {"at": "24:00"}, {"at": "-1:00"}, "0 25 * * *", "61 9 * * *"):
        trig = {"id": "oob", "type": "daily", "enabled": True, "schedule": sch,
                "target": {"chain": "ingest_summarize"}}
        assert eng.is_due(trig, now) is False, sch   # 크래시 없이 not-due


def test_cron_outside_subset_rejected_not_daily(tmp_path):
    """부분집합 밖 cron(일/월/요일 제한)은 검증 거부 + not-due — daily 오해석 금지.

    회귀: 과거 _daily_at 이 parts[2:](일/월/요일)를 안 봐서 '0 9 * * 1'(매주 월요일),
    '0 9 1 * *'(매월 1일)이 (True,'ok')로 통과 후 daily 로 오해석 → 매일 발화
    (의도 대비 최대 7~30배 과발화: 무인 ingest/summary 생성·승인큐 적재).
    """
    eng = _engine(_ctx(tmp_path, []), tmp_path)
    wed = datetime(2026, 7, 1, 10, 0, tzinfo=_KST)   # 수요일 10:00 (cron 시각 지난 뒤)
    thu = datetime(2026, 7, 2, 10, 0, tzinfo=_KST)   # 목요일
    for sch in ("0 9 * * 1", "0 9 1 * *", "0 9 * 6 *", "0 9 1-15 * *", "0 9 */2 * *"):
        trig = {"id": "sub", "type": "daily", "enabled": True, "schedule": sch,
                "target": {"chain": "ingest_summarize"}}
        ok, reason = validate_trigger(trig)
        assert not ok and "M H * * *" in reason, (sch, reason)
        assert eng.is_due(trig, wed) is False, sch   # 수요일에 발화 안 함
        assert eng.is_due(trig, thu) is False, sch   # 다음날도 발화 안 함(매일 발화 회귀)
    # 지원 부분집합("M H * * *")은 계속 통과 — 가드가 정상 daily 를 깨지 않음
    good = {"id": "dy", "type": "daily", "enabled": True, "schedule": "0 9 * * *",
            "target": {"chain": "ingest_summarize"}}
    assert validate_trigger(good) == (True, "ok")
    assert eng.is_due(good, wed) is True and eng.is_due(good, thu) is True


# ── ③ 멱등(중복 발화 0) + ⑧ watermark 증분 ─────────────────────
async def test_idempotent_and_watermark(tmp_path, monkeypatch):
    monkeypatch.setattr(T, "_fetch_url", _fake_fetch)
    trig = _iv("wm", "https://e.com/wm")
    eng = _engine(_ctx(tmp_path, [trig]), tmp_path)
    r1 = await eng.fire(trig)
    assert r1["data"]["status"] == "completed"
    r2 = await eng.fire(trig)                               # 동일 입력 → no_new
    assert r2["data"]["skip_reason"] == "no_new"
    # 처리된 run 은 1건뿐(중복 실행 0)
    assert len([e for e in eng.runs.all() if e.get("processed") == 1]) == 1
    r3 = await eng.fire(trig, input_params={"url": "https://e.com/wm2"})  # 신규 입력 → 처리
    assert r3["data"]["status"] == "completed"
    assert len([e for e in eng.runs.all() if e.get("processed") == 1]) == 2


# ── ④ ★always_gate: 트리거발 publish 는 미승인 시 실발행 0 ──────
async def test_trigger_full_loop_always_gate(tmp_path, monkeypatch):
    monkeypatch.setattr(T, "_fetch_url", _fake_fetch)
    repo = tmp_path / "repo"
    studio = StudioAdapter(repo_path=str(repo), mode="file", journal_path=tmp_path / "pub.jsonl")
    trig = {"id": "fl", "type": "daily", "enabled": True, "schedule": {"at": "00:00"},
            "target": {"chain": "full_loop"}, "params": {"url": "https://e.com/fl"},
            "policy": {"publish_mode": "file", "require_approval": True}}
    ctx = _ctx(tmp_path, [trig], studio=studio)
    eng = _engine(ctx, tmp_path)

    env = await eng.fire(trig)
    # 무인 발화여도 publish 는 게이트 → 승인 큐 폴백(실발행 0)
    assert env["data"]["status"] == "pending_approval"
    assert len(ctx.approvals_store.list_pending()) == 1
    blog = repo / "src" / "content" / "blog"
    assert not blog.exists() or not list(blog.glob("*.mdx"))   # 실파일 없음

    # 명시적 승인 통과 시에만 실발행
    done = await T.tool_approve(ctx, env["data"]["run_id"], "approve")
    assert done.get("published") is True
    assert list(blog.glob("*.mdx"))                            # 승인 후 실파일 생성


# ── ⑤ 웹훅 HMAC + event 디바운스 ───────────────────────────────
def test_verify_hmac_unit():
    sig = hmac.new(b"k", b"body", hashlib.sha256).hexdigest()
    assert verify_hmac("k", b"body", sig) is True
    assert verify_hmac("k", b"body", "sha256=" + sig) is True
    assert verify_hmac("k", b"body", "wrong") is False
    assert verify_hmac("k", b"body", None) is False
    assert verify_hmac("", b"body", sig) is False


async def test_webhook_hmac_and_debounce(tmp_path, monkeypatch):
    monkeypatch.setattr(T, "_fetch_url", _fake_fetch)
    trig = {"id": "wh", "type": "webhook", "enabled": True, "event": {"name": "ev1"},
            "target": {"chain": "ingest_summarize"}, "params": {"url": "https://e.com/wh"}}
    ctx = _ctx(tmp_path, [trig])
    eng = _engine(ctx, tmp_path, secret="s3cr3t")
    body = json.dumps({"event": "ev1", "event_id": "e-100", "params": {"url": "https://e.com/wh"}})
    good = hmac.new(b"s3cr3t", body.encode("utf-8"), hashlib.sha256).hexdigest()

    bad = await eng.handle_webhook(body, "deadbeef")         # 틀린 서명 → 401, 발화 안 함
    assert bad["data"]["http_status"] == 401 and bad["data"]["status"] == "rejected"
    assert eng.runs.all() == [] or all(e.get("status") != "completed" for e in eng.runs.all())

    ok = await eng.handle_webhook(body, good)                # 맞는 서명 → 발화
    assert ok["data"]["status"] == "completed"
    again = await eng.handle_webhook(body, good)             # 동일 event_id 재전송 → 1회만
    assert again["data"]["skip_reason"] == "duplicate_event"


async def test_webhook_no_secret_rejected(tmp_path):
    ctx = _ctx(tmp_path, [{"id": "wh", "type": "webhook", "event": {"name": "ev"},
                           "target": {"chain": "ingest_summarize"}}])
    eng = _engine(ctx, tmp_path, secret=None)                # 시크릿 없음
    res = await eng.handle_webhook("{}", "anything")
    assert res["data"]["http_status"] == 401


# ── ⑥ 실패 격리 ────────────────────────────────────────────────
async def test_failure_isolation(tmp_path, monkeypatch):
    monkeypatch.setattr(T, "_fetch_url", _fake_fetch)
    real_ingest = T.tool_ingest

    async def boom(ctx_, url, *a, **k):
        if "BOOM" in url:
            raise RuntimeError("A 폭발")
        return await real_ingest(ctx_, url, *a, **k)
    monkeypatch.setattr(T, "tool_ingest", boom)

    ctx = _ctx(tmp_path, [_iv("A", "https://e.com/BOOM"), _iv("B", "https://e.com/ok")])
    eng = _engine(ctx, tmp_path, max_retries=1)
    res = await eng.tick(datetime(2026, 6, 9, 10, 0, tzinfo=_KST))
    by = {_tid(r): r["data"]["status"] for r in res}
    assert by.get("A") in ("fallback", "failed")    # A 실패 격리(폴백)
    assert by.get("B") == "completed"               # B 는 정상 실행(스케줄러 생존)


# ── ⑥-b 실패 봉투는 replay 금지 → 다음 발화에서 재시도 ─────────
async def test_failed_run_retries_next_fire(tmp_path, monkeypatch):
    """일시 장애로 폴백된 run 은 run_key replay 대상이 아니며 다음 발화에서 체인을 재실행한다."""
    monkeypatch.setattr(T, "_fetch_url", _fake_fetch)
    real_ingest = T.tool_ingest
    calls = {"n": 0}

    async def flaky(ctx_, url, *a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("일시 네트워크 장애")
        return await real_ingest(ctx_, url, *a, **k)
    monkeypatch.setattr(T, "tool_ingest", flaky)

    trig = _iv("rt", "https://e.com/rt")
    eng = _engine(_ctx(tmp_path, [trig]), tmp_path, max_retries=0)
    r1 = await eng.fire(trig)
    assert r1["data"]["status"] == "fallback"        # 1차: 장애 → 폴백(격리)
    assert r1.get("idempotent_replay") is None

    r2 = await eng.fire(trig)                        # 2차: 장애 복구 → 재시도
    assert r2.get("idempotent_replay") is None       # 실패 봉투 replay 아님
    assert r2["data"]["status"] == "completed"
    assert calls["n"] == 2                           # 체인이 실제로 재호출됨

    r3 = await eng.fire(trig)                        # 3차: 성공 후 watermark 전진 → no_new
    assert r3["data"]["skip_reason"] == "no_new"
    assert len([e for e in eng.runs.all() if e.get("processed") == 1]) == 1  # 중복 실행 0 유지


# ── ⑥-c full_loop(run_protocol 경유)도 동일 — aborted run 재시도 ──
async def test_full_loop_aborted_run_retries_next_fire(tmp_path, monkeypatch):
    """run_protocol 기반 체인(full_loop)이 일시 장애로 abort 돼도 다음 발화에서 재시도한다.

    회귀 가드: 결정적 run_id(protocol+params 해시) 탓에 재발화가 같은 run_id 로
    들어오는데, 과거엔 엔진이 aborted 저널을 idempotent replay 해 장애 복구 후에도
    체인이 영구 재실행 불능(트리거 좀비화)이었다 — ⑥-b 의 재시도 계약이 무효화됨.
    """
    calls = {"n": 0}

    async def flaky(url, client=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("일시 네트워크 장애")
        return await _fake_fetch(url)
    monkeypatch.setattr(T, "_fetch_url", flaky)

    trig = {"id": "flr", "type": "interval", "enabled": True, "schedule": {"every_sec": 1},
            "target": {"chain": "full_loop"}, "params": {"url": "https://e.com/flr"},
            "policy": {"publish_mode": "dry_run", "require_approval": False}}
    eng = _engine(_ctx(tmp_path, [trig]), tmp_path)

    r1 = await eng.fire(trig)
    assert r1["data"]["status"] == "aborted"         # 1차: 장애 → step0(ingest) 중단
    assert r1.get("idempotent_replay") is None

    r2 = await eng.fire(trig)                        # 2차: 장애 복구 → 체인 재실행
    assert r2.get("idempotent_replay") is None       # 저장된 aborted 봉투 replay 아님
    assert r2["data"]["status"] == "completed"
    assert calls["n"] == 2                           # fetch 가 실제로 재호출됨

    r3 = await eng.fire(trig)                        # 3차: watermark 전진 → no_new
    assert r3["data"]["skip_reason"] == "no_new"

# ── ⑥-c summary create 실패는 completed 위장 금지 → watermark 미전진·재시도 ──
class _SummaryFailingNotion:
    """summary create 만 죽는 notion(백엔드 장애 시뮬레이션) — ingest 의 resource 적재는 성공.

    can_create=True 라 tool_create 가 드라이런 폴백 없이 실호출 분기로 들어가고,
    거기서 예외 → tools.py 가 data=None 실패 봉투로 격리한다(예외 미전파 계약).
    """
    can_create = True

    def __init__(self):
        self.fail_summary = True

    async def create(self, type_, data):
        if type_ == "summary" and self.fail_summary:
            raise RuntimeError("notion 503(일시 장애)")
        pk = str(data.get("summary_id") or data.get("resource_id") or "x")
        return make_record(pk, type_, "notion", data)


async def test_summary_create_backend_failure_aborts_then_retries(tmp_path, monkeypatch):
    """(회귀 YOHA-2) summary create 백엔드 장애 → aborted 보고·watermark 미전진·다음 발화 재시도.

    증상: ingest 성공 후 tool_create 가 백엔드 예외를 data=None 봉투로 격리하면
    체인이 status=completed·errors 미전파로 감싸 watermark 가 전진 → 같은 입력은
    이후 영원히 no_new 스킵(요약 조용히 소실, 재시도 0회 — 무인 루프 영구 데이터 소실).
    """
    monkeypatch.setattr(T, "_fetch_url", _fake_fetch)
    trig = _iv("sc", "https://e.com/sc")
    ctx = _ctx(tmp_path, [trig])
    notion = _SummaryFailingNotion()
    ctx.adapters["notion"] = notion
    eng = _engine(ctx, tmp_path)

    r1 = await eng.fire(trig)
    assert r1["data"]["status"] == "aborted"             # completed 위장 금지
    assert r1["data"]["stage"] == "summary_create"
    assert r1["data"]["resource_id"]                     # 수집은 성공 — 고아 리소스 추적 가능
    assert r1["errors"] and "notion" in str(r1["errors"])  # create 실패 사유 전파
    assert r1.get("completed") is None
    assert (eng.state.get("sc") or {}).get("watermark") is None   # 실패는 전진 안 함

    notion.fail_summary = False                          # 장애 복구
    r2 = await eng.fire(trig)                            # 동일 입력 → no_new 아님(재시도)
    assert r2.get("idempotent_replay") is None           # 실패 봉투 replay 금지
    assert r2["data"]["status"] == "completed"
    assert r2["data"]["summary"] is not None

    r3 = await eng.fire(trig)                            # 성공 후에만 watermark 전진
    assert r3["data"]["skip_reason"] == "no_new"
    assert len([e for e in eng.runs.all() if e.get("processed") == 1]) == 1


async def test_summary_create_validation_failure_aborts_no_watermark(tmp_path, monkeypatch):
    """트리거 params.domain 이 enum 밖 → summary 스키마 검증 실패(data=None 봉투).

    completed 위장·watermark 전진 없이 aborted+errors 로 드러나야 운영자가
    트리거 params 를 고치면 다음 발화에서 처리된다(영구 no_new 스킵 금지).
    """
    monkeypatch.setattr(T, "_fetch_url", _fake_fetch)
    trig = {"id": "vd", "type": "interval", "enabled": True,
            "schedule": {"every_sec": 1}, "target": {"chain": "ingest_summarize"},
            "params": {"url": "https://e.com/vd", "domain": "존재하지않는도메인"}}
    eng = _engine(_ctx(tmp_path, [trig]), tmp_path)

    r1 = await eng.fire(trig)
    assert r1["data"]["status"] == "aborted"
    assert r1["data"]["stage"] == "summary_create"
    assert r1["errors"]                                  # 검증 실패 사유 전파(가시화)
    assert r1["verification"]["schema_valid"] is False
    assert (eng.state.get("vd") or {}).get("watermark") is None   # 미전진
    assert all(e.get("processed") != 1 for e in eng.runs.all())

    r2 = await eng.fire(trig)                            # 같은 입력 재발화 → 스킵 아닌 재실행
    assert (r2["data"] or {}).get("skip_reason") != "no_new"
    assert r2["data"]["status"] == "aborted"


# ── ⑦ single-flight 락 ─────────────────────────────────────────
def test_single_flight_lock_and_stale(tmp_path):
    fresh = lambda: datetime(2026, 6, 9, 10, 0, tzinfo=_KST)  # noqa: E731
    L1 = TriggerLock(tmp_path / "x.lock", fresh)
    assert L1.acquire() is True
    assert TriggerLock(tmp_path / "x.lock", fresh).acquire() is False   # 점유 중 재진입 skip
    stale = lambda: datetime(2026, 6, 9, 10, 10, tzinfo=_KST)  # noqa: E731  (>TTL 300s)
    assert TriggerLock(tmp_path / "x.lock", stale).acquire() is True    # 스테일 회수


async def test_fire_skips_when_locked(tmp_path, monkeypatch):
    monkeypatch.setattr(T, "_fetch_url", _fake_fetch)
    trig = _iv("sf", "https://e.com/sf")
    ctx = _ctx(tmp_path, [trig])
    eng = _engine(ctx, tmp_path)
    held = TriggerLock(eng.locks_dir / "sf.lock", eng._now)
    assert held.acquire() is True
    env = await eng.fire(trig)
    assert env["data"]["skip_reason"] == "locked"
    held.release()


# ── ⑨ 완결 루프 무인 드라이런 ──────────────────────────────────
async def test_full_loop_unmanned_dry_run(tmp_path, monkeypatch):
    monkeypatch.setattr(T, "_fetch_url", _fake_fetch)
    trig = {"id": "fld", "type": "daily", "enabled": True, "schedule": {"at": "00:00"},
            "target": {"chain": "full_loop"}, "params": {"url": "https://e.com/fld"},
            "policy": {"publish_mode": "dry_run", "require_approval": False}}
    ctx = _ctx(tmp_path, [trig])
    eng = _engine(ctx, tmp_path)
    env = await eng.fire(trig)
    # require_approval=False + 드라이런 고품질 → 무인 완주(auto_approve), 실발행 0
    assert env["data"]["status"] == "completed"
    assert env["data"]["auto_approved"] is True
    assert env["dry_run"] is True


async def test_tick_fires_due_only(tmp_path, monkeypatch):
    monkeypatch.setattr(T, "_fetch_url", _fake_fetch)
    triggers = [
        _iv("due_iv", "https://e.com/d"),
        {"id": "man", "kind": "manual", "protocol": "resource_to_decision",
         "params": {"query": "x"}},                       # manual → tick 제외
        {"id": "wh", "type": "webhook", "event": {"name": "e"},
         "target": {"chain": "ingest_summarize"}},        # webhook → tick 제외
    ]
    ctx = _ctx(tmp_path, triggers)
    eng = _engine(ctx, tmp_path)
    res = await eng.tick(datetime(2026, 6, 9, 10, 0, tzinfo=_KST))
    assert [_tid(r) for r in res] == ["due_iv"]            # interval 만 발화
