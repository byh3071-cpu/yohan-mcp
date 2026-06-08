# -*- coding: utf-8 -*-
"""yohan-mcp v2 — 정책 엔진 (P5, 자율성 L2→L3 코드).

P4 는 게이트마다 **사람**이 승인했다(L2). P5 는 그 결정을 **정책**으로 자동화한다(L3):
정책 한도 내면 자동 진행(auto_approve), 한도 초과/위험 행위면 P4 승인큐로 폴백(사람 호출).

정책 평가 순서(엄격) ::
    1) always_gate   — 절대 자동화 금지(예: 외부 실발행). 매칭되면 무조건 사람 승인.
    2) limits        — 일일 발행 한도·런당 액션 한도. 초과면 자동승인 거부 → 폴백.
    3) auto_approve  — 안전 조건 충족(예: dry_run & quality_score>=5)이면 자동 진행.
    4) default       — 매칭 없음 → 사람 승인(보수적 기본값).

모든 자동 결정은 `memory/policy_log.jsonl` 에 감사 로그로 남긴다(누가/왜/어떤 규칙).
일일 카운터는 그 로그를 fold 해서 산출 — 외부 의존성 0, 멱등(append-only).

호스트·외부 스케줄러·외부 큐 의존성 없음. PC 에서 전부 테스트 가능.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_KST = timezone(timedelta(hours=9))


def _now() -> datetime:
    return datetime.now(_KST)


def _now_iso() -> str:
    return _now().isoformat(timespec="seconds")


def _today() -> str:
    return _now().date().isoformat()


# ── 규칙 술어(predicate) — facts dict → bool ────────────────────────
# facts: {tool, dry_run, external_publish, quality_score, step_index, run_id, protocol}
_PREDICATES = {
    # 드라이런(외부 미발행) + 고품질(>=5) → 가역적이라 자동 진행 안전
    "dry_run_high_quality": lambda f: bool(f.get("dry_run")) and int(f.get("quality_score") or 0) >= 5,
    # 외부 실발행(URL+KEY 존재) → 되돌리기 어려움 → 절대 자동화 금지
    "external_publish": lambda f: bool(f.get("external_publish")),
    # 발행 step 일반
    "is_publish": lambda f: f.get("tool") == "publish",
}


def _false(_f) -> bool:
    return False


# 기본 정책 — **안전 우선(opt-in)**. 자동승인 없음 = P4 동등(항상 사람 게이트).
# 자동화는 트리거/호출자가 정책을 명시 채택할 때만 켜진다(무인 자동화는 의도적 선택).
DEFAULT_POLICY: dict = {
    "auto_approve_when": [],                 # 기본은 자동승인 안 함 → 사람 승인
    "always_gate": ["external_publish"],     # 외부 실발행은 절대 자동 금지
    "max_actions_per_run": 10,
    "max_publishes_per_day": 20,
}

# 권장 자동화 프리셋 — 드라이런 고품질(>=5)만 자동, 외부 실발행은 게이트.
# 트리거가 명시 채택(triggers.json 의 policy)하거나 호출자가 주입해 사용한다.
RECOMMENDED_AUTO_POLICY: dict = {
    "auto_approve_when": ["dry_run_high_quality"],
    "always_gate": ["external_publish"],
    "max_actions_per_run": 10,
    "max_publishes_per_day": 20,
}


class PolicyLog:
    """정책 자동 결정 감사 로그 — append-only JSONL."""

    def __init__(self, path: str | os.PathLike | None = None) -> None:
        if path is not None:
            self.path = Path(path)
        else:
            base = Path(os.getenv("MEMORY_DIR", ROOT / "memory"))
            self.path = base / "policy_log.jsonl"

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

    def count_auto_publishes_today(self) -> int:
        """오늘(KST) 자동승인된 발행 수 — 일일 한도 산출(멱등 fold)."""
        today = _today()
        return sum(
            1 for e in self.all()
            if e.get("tool") == "publish" and e.get("auto_approve") is True
            and str(e.get("ts", ""))[:10] == today
        )


class PolicyEngine:
    """정책 평가 + 감사 로깅. 게이트에서 호출돼 자동승인/폴백을 결정한다."""

    def __init__(
        self,
        policy: dict | None = None,
        log: "PolicyLog | None" = None,
        log_path: str | os.PathLike | None = None,
    ) -> None:
        self.policy = {**DEFAULT_POLICY, **(policy or {})}
        self.log = log or PolicyLog(log_path)

    # ── 조회 ─────────────────────────────────────────────────────
    def publishes_today(self) -> int:
        return self.log.count_auto_publishes_today()

    def snapshot(self) -> dict:
        """현재 정책 + 오늘 발행 수(읽기 전용 조회용)."""
        return {
            "policy": self.policy,
            "publishes_today": self.publishes_today(),
            "rules_available": sorted(_PREDICATES.keys()),
        }

    # ── 결정 ─────────────────────────────────────────────────────
    def decide(self, facts: dict) -> dict:
        """게이트 facts → 결정 dict {auto_approve, rule, reason, facts}. 결정은 감사 로그에 기록."""
        facts = facts or {}

        # 1) always_gate — 절대 자동화 금지
        for rule in self.policy.get("always_gate", []):
            if _PREDICATES.get(rule, _false)(facts):
                return self._emit(facts, False, rule, "always_gate 규칙 — 무조건 사람 승인")

        # 2) limits
        max_actions = self.policy.get("max_actions_per_run")
        idx = facts.get("step_index")
        if isinstance(max_actions, int) and isinstance(idx, int) and (idx + 1) > max_actions:
            return self._emit(facts, False, "max_actions_per_run",
                              f"런당 액션 한도 {max_actions} 초과(step {idx})")
        if facts.get("tool") == "publish":
            cap = self.policy.get("max_publishes_per_day")
            if isinstance(cap, int):
                today = self.publishes_today()
                if today >= cap:
                    return self._emit(facts, False, "max_publishes_per_day",
                                      f"일일 발행 한도 {cap} 초과(오늘 {today})")

        # 3) auto_approve
        for rule in self.policy.get("auto_approve_when", []):
            if _PREDICATES.get(rule, _false)(facts):
                return self._emit(facts, True, rule, "정책 자동승인 조건 충족")

        # 4) default — 사람 승인
        return self._emit(facts, False, None, "매칭 규칙 없음 — 기본 사람 승인")

    def _emit(self, facts: dict, auto: bool, rule: str | None, reason: str) -> dict:
        sig = {k: facts.get(k) for k in ("dry_run", "quality_score", "external_publish")}
        self.log.record({
            "run_id": facts.get("run_id"),
            "protocol": facts.get("protocol"),
            "step_index": facts.get("step_index"),
            "tool": facts.get("tool"),
            "auto_approve": auto,
            "rule": rule,
            "reason": reason,
            "facts": sig,
        })
        return {"auto_approve": auto, "rule": rule, "reason": reason, "facts": sig}
