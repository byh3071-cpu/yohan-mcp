# -*- coding: utf-8 -*-
"""agent-roster.yaml 로더·다이제스트 (Goal 8 AR-E-P1).

brain SoT만 읽는다. 모델/effort 표 전문은 digest에 넣지 않는다.
기본: get_context에 부착. YOHAN_SKIP_ROSTER_DIGEST=1 이면 스킵.
"""

from __future__ import annotations

import logging
import os

import yaml

from core.paths import resolve_memory_dir

logger = logging.getLogger(__name__)

_cache: tuple[dict | None, str] | None = None


def load_agent_roster(*, use_cache: bool = True) -> tuple[dict | None, str]:
    """(roster_dict, source). source ∈ {brain, none}."""
    global _cache
    if use_cache and _cache is not None:
        return _cache

    result: tuple[dict | None, str] = (None, "none")
    path = resolve_memory_dir() / "core" / "agent-roster.yaml"
    try:
        if path.exists():
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                result = (data, "brain")
    except Exception as exc:
        logger.warning("agent-roster.yaml 로드 실패: %s: %s", type(exc).__name__, exc)

    if use_cache and result[1] == "brain":
        _cache = result
    return result


def build_roster_digest(roster: dict | None) -> dict:
    """토큰 미니멀 digest — 모델 표·lifecycle 전문 제외."""
    if not isinstance(roster, dict):
        return {
            "version": None,
            "status": None,
            "summary_ko": None,
            "source": "none",
        }
    main = roster.get("main_cli") or {}
    command = roster.get("command_center") or {}
    concurrency = roster.get("concurrency") or {}
    quota = roster.get("quota_fallback") or {}
    precedence = roster.get("precedence") or {}
    always = roster.get("conductor_always_on") or {}
    return {
        "version": roster.get("version"),
        "status": roster.get("status"),
        "summary_ko": roster.get("summary_ko"),
        "command_center": command.get("product"),
        "main_cli_default": main.get("default"),
        "concurrency": {
            "same_repo_same_branch": concurrency.get("same_repo_same_branch"),
            "parallel": concurrency.get("parallel"),
        },
        "quota_fallback_never": (quota.get("when_quota_exhausted") or {}).get("never"),
        "precedence_order": precedence.get("order"),
        "orca_enforcer": (roster.get("orca_patterns") or {}).get("enforcer"),
        "ask_when_uncertain": (roster.get("intent") or {}).get("ask_when_uncertain"),
        # 상시 지휘자(v0.4.0) — 패턴명만. size_criteria 표 전문은 digest 제외(토큰 미니멀).
        "conductor_always_on": (
            {
                "enabled": always.get("enabled"),
                "routing": {
                    k: (v or {}).get("pattern")
                    for k, v in (always.get("routing") or {}).items()
                },
                "declaration_format": always.get("declaration_format"),
            }
            if always
            else None
        ),
    }


def should_inject_roster(opts: dict | None = None) -> bool:
    """기본 on. opts.skip_roster / YOHAN_SKIP_ROSTER_DIGEST 로 끔."""
    opts = opts or {}
    if os.getenv("YOHAN_SKIP_ROSTER_DIGEST", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    ):
        return False
    skip = opts.get("skip_roster")
    if skip is True or (
        isinstance(skip, str) and skip.strip().lower() in ("1", "true", "yes", "on")
    ):
        return False
    return True


def inject_agent_roster(envelope: dict, *, enabled: bool = True) -> dict:
    if not enabled or not isinstance(envelope, dict):
        return envelope
    if "agent_roster_digest" in envelope:
        return envelope
    roster, source = load_agent_roster()
    digest = build_roster_digest(roster)
    digest["source"] = source
    envelope["agent_roster_digest"] = digest
    return envelope
