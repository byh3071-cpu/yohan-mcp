# -*- coding: utf-8 -*-
"""yohan-mcp v2 — 스케줄러 추상화 (P5, "깨우는 신호"의 코드 진입점).

트리거(cron/webhook/manual)는 **언제 무엇을 어떤 정책으로** 돌릴지 정의한다.
이 모듈은 트리거를 **읽어 실행하는 진입점**(`run_trigger`)만 제공한다 —
실제 cron 타이머·웹훅 수신 같은 **구동(데이터 플레인)은 P5-B(배포)** 에서 주입하며,
호스트는 아직 미정이다. 여기까지는 호스트·외부 스케줄러 의존성 0 으로 PC 에서 테스트된다.

    Trigger = {id, type:"interval"|"daily"|"webhook"|"manual", target:{chain, params?},
               params?, policy?, schedule?, event?, desc?}  # legacy kind/protocol 은 거부

- 트리거 **정의**는 `triggers.json`(스키마 불변, 리포에 커밋).
- 트리거 **실행 이력**(런타임 상태)은 `memory/trigger_runs.jsonl`(gitignore).
- run_trigger 는 트리거의 policy 를 적용해 run_action(프로토콜)을 호출한다 → 정책 경유.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.paths import resolve_mcp_runtime_dir

ROOT = Path(__file__).resolve().parent.parent
_KST = timezone(timedelta(hours=9))

def _now_iso() -> str:
    return datetime.now(_KST).isoformat(timespec="seconds")


class TriggerRunLog:
    """트리거 실행 이력 append-only JSONL."""

    def __init__(self, path: str | os.PathLike | None = None) -> None:
        if path is not None:
            self.path = Path(path)
        else:
            base = resolve_mcp_runtime_dir()
            self.path = base / "trigger_runs.jsonl"

    def record(self, entry: dict) -> dict:
        entry = {**entry, "ts": _now_iso()}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry

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


class Scheduler:
    """트리거 정의 로더 + 실행 이력. 실제 구동은 P5-B 에서 이 진입점을 호출한다."""

    def __init__(
        self,
        triggers_path: str | os.PathLike | None = None,
        runlog_path: str | os.PathLike | None = None,
    ) -> None:
        self.triggers_path = Path(triggers_path) if triggers_path else ROOT / "triggers.json"
        self.runlog = TriggerRunLog(runlog_path)

    def load(self) -> list[dict]:
        """triggers.json 의 트리거 정의 목록(없으면 빈 목록)."""
        if not self.triggers_path.exists():
            return []
        try:
            doc = json.loads(self.triggers_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []
        return doc.get("triggers", []) if isinstance(doc, dict) else []

    def list(self) -> list[dict]:
        """트리거 요약 목록(실행 가능한 진입점 카탈로그)."""
        out = []
        for t in self.load():
            out.append({
                "id": t.get("id"),
                "type": t.get("type"),
                "chain": (t.get("target") or {}).get("chain"),
                "schedule": t.get("schedule"),
                "has_policy": bool(t.get("policy")),
                "desc": t.get("desc"),
            })
        return out

    def get(self, trigger_id: str) -> dict | None:
        for t in self.load():
            if t.get("id") == trigger_id:
                return t
        return None


async def run_trigger(ctx, trigger_id: str, params: dict | None = None) -> dict:
    """트리거 정의를 읽어 정책을 적용해 프로토콜 실행 (run_action 경유).

    선언 해석(체인 해소·policy 번역·publish_mode 임시 적용)은 TriggerEngine 과
    **단일 지점**(core.triggers.dispatch_declared_trigger)을 공유한다 — 같은 선언이
    진입점(엔진 발화 vs MCP 도구)에 따라 다르게 실행되지 않는다(설정 정합).
    실행 결과 봉투에 trigger 메타를 덧붙이고, 실행 이력을 trigger_runs.jsonl 에 남긴다.
    """
    from core import tools as T  # 지연 임포트: tools ↔ scheduler 순환 방지
    from core import triggers as TR  # 지연 임포트: 선언 해석 규칙 공유(순환 방지)

    sched: Scheduler = ctx.scheduler
    trig = sched.get(trigger_id)
    if trig is None:
        return T._envelope(
            {"status": "unknown_trigger", "trigger_id": trigger_id,
             "known_triggers": [t["id"] for t in sched.list()]},
            None, [], errors=[f"알 수 없는 trigger_id: {trigger_id}"],
        )

    merged = {**(trig.get("params") or {}), **(params or {})}
    # 정책 번역·publish_mode·체인 해소 — TriggerEngine 과 같은 규칙(단일 해석 지점).
    # 과거엔 여기서 publish_mode 미적용 + raw policy 로 자체 해석해, publish_mode=dry_run
    # 선언 트리거가 이 진입점에서만 env 모드(file/pr)로 실발행됐다(YOHA-5).
    env = await TR.dispatch_declared_trigger(ctx, trig, merged)

    status = env.get("data", {}).get("status") if isinstance(env.get("data"), dict) else None
    sched.runlog.record({
        "trigger_id": trigger_id,
        "type": trig.get("type"),
        "protocol": TR._protocol_of(trig) or TR._chain_of(trig),
        "run_id": (env.get("data") or {}).get("run_id"),
        "status": status,
    })
    # 봉투에 트리거 출처 표기(불변 봉투 키 보존)
    env["trigger"] = {"id": trigger_id, "type": trig.get("type")}
    return env


def list_triggers(ctx) -> dict:
    """등록 트리거 카탈로그(읽기 전용)."""
    from core import tools as T
    sched: Scheduler = ctx.scheduler
    return T._envelope({"triggers": sched.list(), "count": len(sched.list())}, None, [])
