# -*- coding: utf-8 -*-
"""yohan-mcp v2 — 트리거 실발화 엔진 (P7, 완결 루프 무인화).

P5 는 트리거를 **선언**(triggers.json)하고 수동 진입점(run_trigger)만 뒀다. P7 은
선언된 트리거를 **실제로 발화**시켜 RESOURCE→SUMMARY(→POST) 완결 루프를 무인 구동한다.

원칙(★ always_gate):
- 수집·요약(ingest→create)까지는 무인 자동.
- 외부 실발행(publish)은 **무인 발화여도 자동 금지** — 트리거 정책이 require_approval 이면
  publish 게이트에서 승인 큐로 폴백(dry_run 봉투)까지만. 명시적 승인 통과 시에만 실발행.

구동 방식:
- cron 계열(interval/daily): `tick()` 이 due 트리거를 발화. cron 풀파서 없음 — every_sec/at(HH:MM)
  과 cron 부분집합 "M H * * *"(→ daily)만 지원(새 의존성 0).
- webhook 계열: `handle_webhook(body, signature)` — HMAC(stdlib hmac+hashlib) 검증 후 발화.

안전장치(외부 브로커 0, 전부 로컬 JSONL/락 파일):
- 멱등: run_key = sha256(trigger_id:watermark:input_hash). 저널에 있으면 replay(중복 실행 0).
- 증분: 트리거별 watermark(마지막 처리 입력 해시) — 동일 입력이면 no-op.
- single-flight: data/locks/{id}.lock(PID+ts). 점유 중 재진입 skip, 스테일(TTL/죽은 PID) 회수.
- 실패 격리: 트리거 1개 raise 해도 잡아서 기록 후 다음 트리거 계속(스케줄러 안 죽음).
- 재시도/백오프: 외부 일시 실패 시 지수 백오프 후 드라이런 폴백.
"""
from __future__ import annotations

import errno
import hashlib
import hmac
import json
import os
import signal
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core import protocols as P
from core import verify as V
from core.policy import PolicyEngine, RECOMMENDED_AUTO_POLICY

ROOT = Path(__file__).resolve().parent.parent
_KST = timezone(timedelta(hours=9))
_LOCK_TTL_SEC = 300


# ── 시간/해시 유틸 ──────────────────────────────────────────────────
def _now_kst() -> datetime:
    return datetime.now(_KST)


def _parse_iso(s) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=_KST)


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _canon(obj) -> str:
    return json.dumps(obj or {}, sort_keys=True, ensure_ascii=False, default=str)


def _tzinfo(name: str | None):
    """tz 이름 → tzinfo. Asia/Seoul 은 고정 KST. 그 외는 zoneinfo 시도(없으면 KST 폴백, 새 의존성 0)."""
    if not name or name == "Asia/Seoul":
        return _KST
    try:
        from zoneinfo import ZoneInfo  # stdlib
        return ZoneInfo(name)
    except Exception:
        return _KST  # tzdata 없을 때 graceful 폴백


def _pid_alive(pid) -> bool:
    if not pid:
        return False
    if os.name == "nt":
        return True  # Windows: PID liveness 생략 → TTL 로 스테일 판정
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return True
    return True


# ── 트리거 타입/체인/정책 해소 ──────────────────────────────────────
def _trigger_type(trig: dict) -> str:
    """type 명시 우선, 없으면 kind/schedule 로 추론(interval|daily|webhook|manual)."""
    t = trig.get("type")
    if t:
        return t
    kind = trig.get("kind")
    if kind == "webhook":
        return "webhook"
    if kind == "manual":
        return "manual"
    sch = trig.get("schedule")
    if isinstance(sch, dict):
        return "interval" if "every_sec" in sch else "daily"
    if isinstance(sch, str):
        return "daily"  # cron 부분집합 "M H * * *"
    return "manual"


def _chain_of(trig: dict) -> str:
    """target.chain 우선, 없으면 legacy protocol 매핑."""
    tgt = trig.get("target") or {}
    if tgt.get("chain"):
        return tgt["chain"]
    proto = trig.get("protocol")
    if proto == "ingest_summarize_publish":
        return "full_loop"
    return proto or "full_loop"


def _protocol_of(trig: dict) -> str | None:
    chain = _chain_of(trig)
    if chain == "full_loop":
        return "ingest_summarize_publish"
    if chain == "ingest_summarize":
        return None  # 인라인 처리(프로토콜 추가 없이)
    return chain  # resource_to_decision 등 legacy


def _daily_at(trig: dict) -> tuple[int, int] | None:
    """daily 트리거의 (hh, mm). schedule.at "HH:MM" 또는 cron "M H * * *"."""
    sch = trig.get("schedule")
    if isinstance(sch, dict) and sch.get("at"):
        try:
            hh, mm = str(sch["at"]).split(":")
            return int(hh), int(mm)
        except (ValueError, AttributeError):
            return None
    if isinstance(sch, str):
        parts = sch.split()
        if len(parts) == 5 and parts[0].isdigit() and parts[1].isdigit():
            return int(parts[1]), int(parts[0])  # "M H * * *" → (H, M)
    return None


def validate_trigger(trig: dict) -> tuple[bool, str]:
    """선언 트리거 스키마/타입 검증 → (ok, reason). 잘못된 트리거는 호출자가 비활성 처리."""
    if not isinstance(trig, dict):
        return False, "트리거가 dict 아님"
    if not trig.get("id"):
        return False, "id 누락"
    typ = _trigger_type(trig)
    if typ == "interval":
        sch = trig.get("schedule")
        if not (isinstance(sch, dict) and isinstance(sch.get("every_sec"), int) and sch["every_sec"] > 0):
            return False, "interval: schedule.every_sec(양의 int) 필요"
    elif typ == "daily":
        if _daily_at(trig) is None:
            return False, "daily: schedule.at('HH:MM') 또는 cron 'M H * * *' 필요"
    elif typ in ("webhook", "manual"):
        pass
    else:
        return False, f"알 수 없는 type: {typ}"
    chain = _chain_of(trig)
    if chain not in ("full_loop", "ingest_summarize") and chain not in P.PROTOCOLS:
        return False, f"알 수 없는 chain/protocol: {chain}"
    return True, "ok"


# ── 웹훅 HMAC ───────────────────────────────────────────────────────
def verify_hmac(secret: str, body: bytes | str, signature: str | None) -> bool:
    """HMAC-SHA256 서명 검증. 'sha256=' 접두 허용. 상수시간 비교."""
    if not secret or not signature:
        return False
    body_b = body.encode("utf-8") if isinstance(body, str) else body
    sec_b = secret.encode("utf-8") if isinstance(secret, str) else secret
    mac = hmac.new(sec_b, body_b, hashlib.sha256).hexdigest()
    sig = signature.split("=", 1)[1] if signature.startswith("sha256=") else signature
    return hmac.compare_digest(mac, sig)


# ── 로컬 저널/락 ────────────────────────────────────────────────────
class _Journal:
    """append-only JSONL fold 저장소(키별 마지막 우선)."""

    def __init__(self, path: str | os.PathLike) -> None:
        self.path = Path(path)

    def all(self) -> list[dict]:
        if not self.path.exists():
            return []
        out: list[dict] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def append(self, entry: dict) -> dict:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry


class TriggerState(_Journal):
    """트리거별 watermark/last_fire 상태(fold = 키별 마지막)."""

    def get(self, tid: str) -> dict | None:
        found = None
        for e in self.all():
            if e.get("trigger_id") == tid:
                found = e
        return found

    def merge(self, tid: str, patch: dict) -> dict:
        cur = self.get(tid) or {"trigger_id": tid}
        cur = {**cur, **patch, "trigger_id": tid}
        return self.append(cur)


class TriggerRuns(_Journal):
    """발화 이력 + run_key 멱등 인덱스 + event_id 디바운스."""

    def get(self, run_key: str) -> dict | None:
        found = None
        for e in self.all():
            if e.get("run_key") == run_key:
                found = e
        return found

    def has_event(self, tid: str, event_id: str) -> bool:
        return any(e.get("trigger_id") == tid and e.get("event_id") == event_id
                   for e in self.all() if event_id)

    def record(self, entry: dict, now: datetime) -> dict:
        return self.append({**entry, "ts": now.isoformat(timespec="seconds")})


class TriggerLock:
    """single-flight 락 — 원자적 O_EXCL 생성. 점유 중 재진입 skip, 스테일 회수."""

    def __init__(self, path: str | os.PathLike, now_fn) -> None:
        self.path = Path(path)
        self._now = now_fn

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if not self._is_stale():
                return False
            self._steal()
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                return False
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps({"pid": os.getpid(), "ts": self._now().isoformat()}))
        return True

    def _is_stale(self) -> bool:
        try:
            info = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return True
        ts = _parse_iso(info.get("ts"))
        if ts is None or (self._now() - ts).total_seconds() > _LOCK_TTL_SEC:
            return True
        return not _pid_alive(info.get("pid"))

    def _steal(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def release(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


# ── 인라인 체인: ingest_summarize (수집 → SUMMARY, 발행 없음) ────────
async def _chain_ingest_summarize(ctx, params: dict) -> dict:
    """RESOURCE 수집 → SUMMARY create(내부, always_gate 비대상). 무인 자동 OK.

    프로토콜을 새로 등록하지 않고(plan 후보 수 불변) tools 를 직접 합성해 표준봉투를 만든다.
    """
    from core import tools as T  # 지연 임포트: triggers ↔ tools 순환 방지
    url = params.get("url") or params.get("source") or ""
    ing = await T.tool_ingest(ctx, url)
    if ing.get("data") is None:  # fetch 실패 등 — 그대로 전달(graceful)
        return V.make_envelope(
            {"status": "aborted", "chain": "ingest_summarize", "stage": "ingest"},
            sources_used=ing["provenance"]["sources_used"], schema_valid=False,
            reasoning_steps=["ingest 실패 → 체인 중단"], errors=ing.get("errors"), aborted=True,
        )
    built = P._build_summary_from_ingest({"ingest": {"output": ing}}, params)
    summary = built["data"]
    created = await T.tool_create(ctx, "summary", summary)
    return V.make_envelope(
        {
            "status": "completed", "chain": "ingest_summarize",
            "resource_id": ing["data"].get("resource_id"),
            "summary": created.get("data"),
        },
        sources_used=ing["provenance"]["sources_used"] + created["provenance"]["sources_used"],
        schema_valid=created["verification"].get("schema_valid"),
        reasoning_steps=["RESOURCE 수집", "SUMMARY create(내부 — always_gate 비대상)"],
        dry_run=bool(created.get("dry_run")), completed=True,
    )


# ── 트리거 엔진 ─────────────────────────────────────────────────────
class TriggerEngine:
    """선언 트리거를 실제로 발화시키는 엔진(cron 계열 tick + webhook 핸들러)."""

    def __init__(
        self,
        ctx,
        *,
        data_dir: str | os.PathLike | None = None,
        now=None,
        sleep=None,
        secret: str | None = None,
        max_retries: int = 2,
    ) -> None:
        self.ctx = ctx
        d = Path(data_dir) if data_dir else (ROOT / "data")
        self.state = TriggerState(d / "trigger_state.jsonl")
        self.runs = TriggerRuns(d / "trigger_runs.jsonl")
        self.locks_dir = d / "locks"
        self._now = now or _now_kst
        self._sleep = sleep if sleep is not None else __import__("time").sleep
        self._secret = secret
        self._max_retries = max_retries

    # ── due 판정 ────────────────────────────────────────────────
    def is_due(self, trig: dict, now: datetime | None = None) -> bool:
        now = now or self._now()
        typ = _trigger_type(trig)
        if typ not in ("interval", "daily"):
            return False
        st = self.state.get(trig["id"]) or {}
        last = _parse_iso(st.get("last_fire_ts"))
        if typ == "interval":
            every = (trig.get("schedule") or {}).get("every_sec")
            if not every:
                return False
            return last is None or (now - last).total_seconds() >= every
        at = _daily_at(trig)
        if at is None:
            return False
        tz = _tzinfo((trig.get("schedule") or {}).get("tz") if isinstance(trig.get("schedule"), dict) else None)
        now_l = now.astimezone(tz)
        due_today = now_l.replace(hour=at[0], minute=at[1], second=0, microsecond=0)
        if now_l < due_today:
            return False
        if last is not None and last.astimezone(tz).date() == now_l.date():
            return False  # 오늘 이미 발화
        return True

    # ── 발화 ────────────────────────────────────────────────────
    async def fire(self, trig: dict, *, input_params: dict | None = None,
                   event_id: str | None = None, force: bool = False) -> dict:
        tid = trig.get("id") or "?"
        ok, reason = validate_trigger(trig)
        if not ok:
            self._record(tid, None, "invalid", event_id, reason=reason)
            return self._skip(tid, "invalid", reason)
        if not trig.get("enabled", True):
            return self._skip(tid, "disabled", "enabled=false")

        lock = TriggerLock(self.locks_dir / f"{tid}.lock", self._now)
        if not lock.acquire():
            return self._skip(tid, "locked", "single-flight: 다른 실행 진행 중 — skip")
        try:
            now = self._now()
            params = {**(trig.get("params") or {}), **(input_params or {})}
            input_hash = _sha(_canon(params))
            st = self.state.get(tid) or {}
            watermark = st.get("watermark")

            # 웹훅 event 디바운스(동일 event_id 재전송 1회만)
            if event_id and self.runs.has_event(tid, event_id):
                return self._skip(tid, "duplicate_event", f"event_id={event_id} 이미 처리됨")

            # 발화 평가 시점 기록(interval 간격 유지)
            self.state.merge(tid, {"last_fire_ts": now.isoformat()})

            # 증분 — 동일 입력이면 no-op
            if not force and watermark == input_hash:
                self._record(tid, None, "no_new", event_id)
                return self._skip(tid, "no_new", "watermark 동일 — 신규 입력 없음")

            # 멱등 — 처리 성공(processed) run 만 replay.
            # 실패 봉투(fallback/aborted 등)는 watermark 를 전진시키지 않으므로(아래 418행)
            # replay 하면 '다음 tick 재시도' 의도가 무효화됨 → 실패 prior 는 체인 재실행.
            run_key = _sha(f"{tid}:{watermark or ''}:{input_hash}")
            prior = self.runs.get(run_key)
            if prior is not None and not force and (
                    prior.get("processed") == 1
                    or prior.get("status") in ("completed", "pending_approval")):
                env = dict(prior.get("envelope") or {})
                env["idempotent_replay"] = True
                return env

            env = await self._run_chain(trig, params)
            env.setdefault("trigger", {"id": tid, "type": _trigger_type(trig)})
            status = self._status_of(env)
            processed = 1 if status in ("completed", "pending_approval") else 0
            if processed:  # watermark 전진(처리됨) — 실패는 전진 안 함
                self.state.merge(tid, {"watermark": input_hash,
                                       "last_processed_ts": now.isoformat()})
            self.runs.record({
                "trigger_id": tid, "run_key": run_key, "event_id": event_id,
                "status": status, "input_hash": input_hash, "watermark": watermark,
                "processed": processed, "envelope": env,
            }, now)
            return env
        except Exception as exc:  # 실패 격리 — 기록 후 폴백(스케줄러 안 죽음)
            self._record(tid, None, "fail", event_id, reason=f"{type(exc).__name__}: {exc}")
            return self._fail(tid, exc)
        finally:
            lock.release()

    async def _run_chain(self, trig: dict, params: dict) -> dict:
        """체인 실행 + 지수 백오프 재시도. 한도 초과 시 드라이런 폴백 봉투."""
        attempts = 0
        delay = 1
        while True:
            try:
                return await self._dispatch_chain(trig, params)
            except Exception as exc:
                attempts += 1
                if attempts > self._max_retries:
                    return V.make_envelope(
                        {"status": "fallback", "chain": _chain_of(trig)},
                        sources_used=[], schema_valid=None,
                        reasoning_steps=[f"{attempts - 1}회 재시도 후 드라이런 폴백"],
                        dry_run=True, errors=[f"{type(exc).__name__}: {exc}"],
                    )
                self._sleep(delay)
                delay *= 2

    async def _dispatch_chain(self, trig: dict, params: dict) -> dict:
        from core import tools as T  # 지연 임포트
        chain = _chain_of(trig)
        if chain == "ingest_summarize":
            return await _chain_ingest_summarize(self.ctx, params)
        proto = _protocol_of(trig)
        engine = self._policy_for(trig)
        # publish_mode 를 이 발화에 한해 studio 어댑터에 반영(끝나면 복원)
        mode = self._publish_mode(trig)
        studio = self.ctx.adapters.get("studio")
        prev_mode = getattr(studio, "mode", None)
        if mode and studio is not None:
            studio.mode = mode
        try:
            return await T.tool_run_action(self.ctx, proto, params, policy=engine)
        finally:
            if mode and studio is not None:
                studio.mode = prev_mode

    # ── 정책/모드 ───────────────────────────────────────────────
    def _policy_for(self, trig: dict):
        pol = trig.get("policy") or {}
        if "require_approval" in pol:
            if pol.get("require_approval", True):
                # 트리거발 publish 는 무조건 사람 승인(always_gate) — 무인 통과 금지
                base = {"auto_approve_when": [], "always_gate": ["is_publish", "external_publish"],
                        "max_publishes_per_day": 0}
            else:
                base = dict(RECOMMENDED_AUTO_POLICY)
            return PolicyEngine(base, log=self.ctx.policy.log)
        if pol:  # legacy 명시 정책(auto_approve_when/always_gate 등)
            return PolicyEngine(pol, log=self.ctx.policy.log)
        return None  # ctx.policy 기본(보수적)

    @staticmethod
    def _publish_mode(trig: dict) -> str | None:
        pol = trig.get("policy") or {}
        tgt = trig.get("target") or {}
        return pol.get("publish_mode") or tgt.get("publish_mode")

    # ── tick (cron 계열) ────────────────────────────────────────
    async def tick(self, now: datetime | None = None) -> list[dict]:
        """due 인 interval/daily 트리거를 발화. webhook/manual 은 제외. 실패 격리."""
        now = now or self._now()
        out: list[dict] = []
        for trig in self.ctx.scheduler.load():
            tid = trig.get("id") or "?"
            try:
                if not trig.get("enabled", True):
                    continue
                if _trigger_type(trig) not in ("interval", "daily"):
                    continue
                if not self.is_due(trig, now):
                    continue
                out.append(await self.fire(trig))
            except Exception as exc:  # 한 트리거 실패가 tick 전체를 죽이지 않게
                self._record(tid, None, "fail", None, reason=f"tick: {type(exc).__name__}: {exc}")
                out.append(self._fail(tid, exc))
        return out

    # ── webhook ─────────────────────────────────────────────────
    async def handle_webhook(self, body: bytes | str, signature: str | None,
                             *, secret: str | None = None) -> dict:
        """웹훅 수신 → HMAC 검증 → 매핑 트리거 발화. 검증 실패는 발화 안 함(401)."""
        secret = secret or self._secret or os.getenv("WEBHOOK_SECRET")
        if not secret:
            return self._reject(401, "WEBHOOK_SECRET 미설정 — 거부")
        if not verify_hmac(secret, body, signature):
            return self._reject(401, "HMAC 서명 불일치/누락 — 거부")
        try:
            raw = body.decode("utf-8") if isinstance(body, bytes) else body
            payload = json.loads(raw)
        except Exception:
            return self._reject(400, "payload JSON 파싱 실패")
        event_name = payload.get("event") or payload.get("name")
        event_id = payload.get("event_id") or payload.get("id")
        trig = self._webhook_trigger_for(event_name)
        if trig is None:
            return self._skip(str(event_name), "no_trigger", f"event={event_name!r} 매핑 트리거 없음")
        return await self.fire(trig, input_params=payload.get("params") or {}, event_id=event_id)

    def _webhook_trigger_for(self, event_name) -> dict | None:
        for trig in self.ctx.scheduler.load():
            if _trigger_type(trig) != "webhook":
                continue
            ev = (trig.get("event") or {}).get("name")
            if event_name is None:
                return trig  # 단일 웹훅 트리거 매핑
            if ev == event_name or trig.get("id") == event_name:
                return trig
        return None

    # ── run_forever (데몬, graceful shutdown) ──────────────────
    def run_forever(self, *, poll_sec: float = 1.0) -> None:  # pragma: no cover
        """SIGINT/SIGTERM 까지 tick 루프. 진행 중 run 저널 flush 후 종료."""
        import asyncio
        stop = {"flag": False}

        def _handler(_signum, _frame):
            stop["flag"] = True

        for s in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
            if s is not None:
                try:
                    signal.signal(s, _handler)
                except (ValueError, OSError):
                    pass

        async def _loop():
            while not stop["flag"]:
                await self.tick()
                await asyncio.sleep(poll_sec)

        asyncio.run(_loop())

    # ── 봉투 헬퍼 ───────────────────────────────────────────────
    @staticmethod
    def _status_of(env: dict) -> str:
        d = env.get("data") if isinstance(env, dict) else None
        return d.get("status") if isinstance(d, dict) else "unknown"

    def _skip(self, tid, reason_code, detail) -> dict:
        return V.make_envelope(
            {"status": "skipped", "trigger_id": tid, "skip_reason": reason_code, "detail": detail},
            sources_used=[], schema_valid=None,
            reasoning_steps=[f"트리거 {tid} skip({reason_code}): {detail}"],
            dry_run=True, skipped=True,
        )

    def _fail(self, tid, exc) -> dict:
        return V.make_envelope(
            {"status": "failed", "trigger_id": tid, "error": f"{type(exc).__name__}: {exc}"},
            sources_used=[], schema_valid=False,
            reasoning_steps=[f"트리거 {tid} 실패(격리됨): {exc}"],
            errors=[f"{type(exc).__name__}: {exc}"], failed=True,
        )

    def _reject(self, code, detail) -> dict:
        return V.make_envelope(
            {"status": "rejected", "http_status": code, "detail": detail},
            sources_used=[], schema_valid=None,
            reasoning_steps=[f"웹훅 거부({code}): {detail}"],
            errors=[detail], rejected=True,
        )

    def _record(self, tid, run_key, status, event_id, *, reason=None) -> None:
        self.runs.record({
            "trigger_id": tid, "run_key": run_key, "event_id": event_id,
            "status": status, "reason": reason,
        }, self._now())
