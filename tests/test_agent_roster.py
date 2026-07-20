# -*- coding: utf-8 -*-
"""Goal 8 — agent-roster digest 테스트."""

import textwrap

import pytest

from core import agent_roster as AR


@pytest.fixture(autouse=True)
def _reset_cache():
    AR._cache = None
    yield
    AR._cache = None


_ROSTER_YAML = textwrap.dedent("""\
    schema: agent-roster
    version: "0.3.0"
    status: active
    summary_ko: "테스트 요약"
    intent:
      ask_when_uncertain: true
    command_center:
      product: orca
    main_cli:
      default: claude-code
    concurrency:
      same_repo_same_branch: forbidden
      parallel: worktree_only
    quota_fallback:
      when_quota_exhausted:
        never: metered_api_bypass
    precedence:
      order:
        - repo_RULES_or_CLAUDE_LIVE
        - agent-roster_defaults
    conductor_always_on:
      enabled: true
      declaration_format: "라우팅: <S|M|L> — <계획>"
      size_criteria:
        S: { touched_files: "<=2", SHOULD_NOT_APPEAR_IN_DIGEST: true }
      routing:
        S: { pattern: solo_conductor }
        M: { pattern: subagent_tiering }
        L: { pattern: orca_pipeline }
    orca_patterns:
      enforcer: "C:\\\\test\\\\orca-enforcement.ps1"
    cli_fleet:
      claude-code:
        models:
          strong: [fable, opus, SHOULD_NOT_APPEAR_IN_DIGEST]
    """)


def _write_roster(tmp_path):
    d = tmp_path / "memory" / "core"
    d.mkdir(parents=True, exist_ok=True)
    (d / "agent-roster.yaml").write_text(_ROSTER_YAML, encoding="utf-8")


def test_load_and_digest_active(tmp_path):
    _write_roster(tmp_path)
    roster, source = AR.load_agent_roster(use_cache=False)
    assert source == "brain"
    assert roster["status"] == "active"
    digest = AR.build_roster_digest(roster)
    assert digest["status"] == "active"
    assert digest["version"] == "0.3.0"
    assert digest["main_cli_default"] == "claude-code"
    assert digest["concurrency"]["parallel"] == "worktree_only"
    assert "SHOULD_NOT_APPEAR_IN_DIGEST" not in str(digest)
    assert "cli_fleet" not in digest
    # 상시 지휘자(v0.4.0): 패턴명만 digest에, size_criteria 표 전문은 제외
    assert digest["conductor_always_on"]["enabled"] is True
    assert digest["conductor_always_on"]["routing"]["L"] == "orca_pipeline"
    assert digest["conductor_always_on"]["routing"]["S"] == "solo_conductor"


def test_graceful_none(tmp_path):
    roster, source = AR.load_agent_roster(use_cache=False)
    assert roster is None and source == "none"
    digest = AR.build_roster_digest(None)
    assert digest["status"] is None


def test_inject_idempotent(tmp_path):
    _write_roster(tmp_path)
    env = {"count": 0}
    AR.inject_agent_roster(env, enabled=True)
    assert env["agent_roster_digest"]["status"] == "active"
    first = env["agent_roster_digest"]
    AR.inject_agent_roster(env, enabled=True)
    assert env["agent_roster_digest"] is first


def test_should_inject_skip_env(monkeypatch):
    monkeypatch.setenv("YOHAN_SKIP_ROSTER_DIGEST", "1")
    assert AR.should_inject_roster({}) is False
    monkeypatch.delenv("YOHAN_SKIP_ROSTER_DIGEST", raising=False)
    assert AR.should_inject_roster({"skip_roster": True}) is False
    assert AR.should_inject_roster({}) is True
