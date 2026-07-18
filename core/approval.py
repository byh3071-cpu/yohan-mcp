# -*- coding: utf-8 -*-
"""yohan-mcp v2 — 승인큐 (P4, 자율성 L2 게이트).

프로토콜 체인이 **되돌리기 어려운 step**(외부 발행 등) 직전에서 멈추면,
그 시점 상태를 이 큐에 적재하고 사람의 결정을 기다린다(Human-in-the-loop).

저장소는 **로컬 append-only JSONL**(`memory/approvals.jsonl`) — 외부 큐/브로커
의존성 없음(n8n·Redis·RabbitMQ X). 한 (run_id, step_index) 의 현재 상태는
이벤트들을 시간순으로 접어(fold) 마지막 이벤트로 정한다:

    enqueue → {status:"pending", payload, ...}   (게이트 도달)
    resolve → {status:"approve"|"reject", note}  (사람 결정)

멱등: 같은 (run_id, step_index) 의 결정을 중복 처리하면 무시하고 기존 결과를 돌려준다
(append-only 라 과거 이벤트는 보존, 현재 상태만 마지막 이벤트로 해석).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.paths import resolve_mcp_runtime_dir
_KST = timezone(timedelta(hours=9))

_PENDING = "pending"
_APPROVE = "approve"
_REJECT = "reject"
_TERMINAL = (_APPROVE, _REJECT)


def _now_iso() -> str:
    return datetime.now(_KST).isoformat(timespec="seconds")


class ApprovalQueue:
    """승인 게이트 append-only JSONL 큐.

    한 줄 = 한 이벤트. (run_id, step_index) 별 최신 이벤트가 현재 상태.
    """

    def __init__(self, path: str | os.PathLike | None = None) -> None:
        if path is not None:
            self.path = Path(path)
        else:
            base = resolve_mcp_runtime_dir()
            self.path = base / "approvals.jsonl"

    # ── 저수준 IO ────────────────────────────────────────────────
    def _append(self, event: dict) -> dict:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
        return event

    def all(self) -> list[dict]:
        """적재된 모든 이벤트(시간순)."""
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

    # ── 상태 fold (append-only → 현재 상태) ──────────────────────
    def _latest_by_key(self) -> dict[tuple[str, int], dict]:
        """(run_id, step_index) → 마지막 이벤트."""
        latest: dict[tuple[str, int], dict] = {}
        for ev in self.all():
            key = (ev.get("run_id"), ev.get("step_index"))
            latest[key] = ev
        return latest

    def status_of(self, run_id: str, step_index: int) -> str | None:
        """(run_id, step_index) 의 현재 상태(pending/approve/reject). 없으면 None."""
        ev = self._latest_by_key().get((run_id, step_index))
        return ev.get("status") if ev else None

    # ── 게이트 적재 ──────────────────────────────────────────────
    def enqueue(self, run_id: str, protocol: str, step_index: int, payload: dict) -> dict:
        """게이트 도달 → pending 이벤트 적재 후 그 이벤트 반환.

        멱등: 같은 (run_id, step_index) 가 이미 적재돼 있으면(상태 불문) 중복 적재하지 않고
        기존 pending 이벤트를 돌려준다 — 재실행해도 큐가 부풀지 않는다.
        """
        existing = self._pending_event(run_id, step_index)
        if existing is not None:
            return existing
        event = {
            "run_id": run_id,
            "protocol": protocol,
            "step_index": step_index,
            "payload": payload,
            "status": _PENDING,
            "ts": _now_iso(),
        }
        return self._append(event)

    def _pending_event(self, run_id: str, step_index: int | None = None) -> dict | None:
        """그 run_id 의 pending 게이트 원본 이벤트(payload 보유)를 찾는다.

        fold 적용: raw 스캔이 아니라 `_latest_by_key`(이벤트 접기)를 경유해
        (run_id, step_index) 의 **현재 상태**가 pending 인 게이트만 본다. 종결(approve/reject)
        후 같은 게이트에 재도달하면 현재 상태가 pending 이 아니므로 잡히지 않아,
        enqueue 가 새 payload 로 재적재할 수 있다(좀비 게이트 방지).
        step_index 가 주어지면 그 step 의 게이트만, 없으면 현재 pending 중 가장 최근(ts).
        """
        found = None
        for (rid, sidx), ev in self._latest_by_key().items():
            if rid != run_id or ev.get("status") != _PENDING:
                continue
            if step_index is not None and sidx != step_index:
                continue
            if found is None or ev.get("ts", "") >= found.get("ts", ""):
                found = ev  # 현재 pending 중 최신(ts) 우선
        return found

    # ── 사람 결정 ────────────────────────────────────────────────
    def resolve(self, run_id: str, decision: str, note: str | None = None) -> dict:
        """run_id 의 대기 중 게이트를 승인/거부.

        반환: {run_id, step_index, status, note, ts, idempotent: bool}
        멱등: 이미 종결(approve/reject)된 게이트는 그대로 두고 기존 결정을 돌려준다.
        대기(pending) 게이트가 없으면 status="unknown" 반환(존재하지 않거나 이미 처리됨).
        """
        decision = _APPROVE if decision == _APPROVE else (_REJECT if decision == _REJECT else decision)
        if decision not in _TERMINAL:
            return {"run_id": run_id, "status": "invalid",
                    "note": f"decision 은 'approve'|'reject' 여야 함: {decision!r}", "idempotent": False}

        pending = self._pending_event(run_id)
        if pending is None:
            # 대기 게이트 없음 — 이미 처리됐는지(종결 상태) 확인
            latest = self._latest_by_key()
            terminal = [ev for (rid, _), ev in latest.items()
                        if rid == run_id and ev.get("status") in _TERMINAL]
            if terminal:
                ev = terminal[-1]
                return {**{k: ev.get(k) for k in ("run_id", "step_index", "status", "note", "ts")},
                        "idempotent": True}
            return {"run_id": run_id, "status": "unknown",
                    "note": "대기 중 게이트 없음(존재하지 않는 run_id)", "idempotent": False}

        step_index = pending["step_index"]
        cur = self.status_of(run_id, step_index)
        if cur in _TERMINAL:
            # 이미 결정됨 — 멱등 무시
            ev = self._latest_by_key()[(run_id, step_index)]
            return {**{k: ev.get(k) for k in ("run_id", "step_index", "status", "note", "ts")},
                    "idempotent": True}

        event = {
            "run_id": run_id,
            "protocol": pending.get("protocol"),
            "step_index": step_index,
            "status": decision,
            "note": note,
            "ts": _now_iso(),
        }
        self._append(event)
        return {"run_id": run_id, "step_index": step_index, "status": decision,
                "note": note, "ts": event["ts"], "idempotent": False}

    # ── 조회 ─────────────────────────────────────────────────────
    def list_pending(self) -> list[dict]:
        """현재 승인 대기(pending) 중인 게이트 목록(원본 이벤트)."""
        out = []
        for key, ev in self._latest_by_key().items():
            if ev.get("status") == _PENDING:
                out.append(ev)
        out.sort(key=lambda e: e.get("ts", ""))
        return out

    def get(self, run_id: str) -> dict | None:
        """그 run_id 의 가장 최근 이벤트(상태 조회용)."""
        evs = [ev for ev in self.all() if ev.get("run_id") == run_id]
        return evs[-1] if evs else None

    def payload_for(self, run_id: str) -> dict | None:
        """그 run_id 게이트의 payload(컨텍스트 스냅샷) — 승인 후 재개에 사용."""
        ev = self._pending_event(run_id)
        return ev.get("payload") if ev else None
