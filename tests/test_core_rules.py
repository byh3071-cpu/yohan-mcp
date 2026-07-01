# -*- coding: utf-8 -*-
"""ADR-008 P0 — brain 코어룰셋 로더·다이제스트·주입 테스트.

conftest autouse 픽스처가 MEMORY_DIR 을 tmp_path/memory 로 잡으므로, 기본적으로 brain yaml 은
없고 인레포 스냅샷(.agents/CORE-RULES.md)으로 폴백된다. brain 경로 테스트는 tmp 에 yaml 을 깐다.
"""
import textwrap

import pytest

from core import core_rules as CR


@pytest.fixture(autouse=True)
def _reset_cache():
    # 모듈 레벨 캐시가 테스트 간 새지 않도록 매 테스트 리셋
    CR._cache = None
    yield
    CR._cache = None


_BRAIN_YAML = textwrap.dedent("""\
    version: 9.9.9
    identity:
      role: "테스트 역할"
      doctrine: "테스트 독트린"
    non_negotiable:
      - "절대규칙 A"
      - "절대규칙 B"
    coding_execution:
      - "코딩규율(다이제스트에서 제외돼야)"
    safety:
      capability_gating: "외부 쓰기 기본 잠금"
      instruction_hierarchy: "시스템 > 개발자 > 유저"
    cost:
      - "비용규칙(제외돼야)"
    pattern_refs:
      - PAT-001
      - PAT-002
    """)


def _write_brain_yaml(tmp_path):
    d = tmp_path / "memory" / "core"
    d.mkdir(parents=True, exist_ok=True)
    (d / "core-ruleset.yaml").write_text(_BRAIN_YAML, encoding="utf-8")


# ① brain yaml 로드 (MEMORY_DIR=tmp/memory 는 conftest 가 이미 설정)
def test_load_brain_yaml(tmp_path):
    _write_brain_yaml(tmp_path)
    ruleset, source = CR.load_core_ruleset(use_cache=False)
    assert source == "brain"
    assert ruleset["version"] == "9.9.9"
    assert ruleset["non_negotiable"] == ["절대규칙 A", "절대규칙 B"]


# ② yaml 없으면 인레포 스냅샷(.agents/CORE-RULES.md) 폴백
def test_fallback_to_snapshot():
    # tmp/memory/core/core-ruleset.yaml 없음 → 스냅샷 폴백
    ruleset, source = CR.load_core_ruleset(use_cache=False)
    assert source == "snapshot"
    assert ruleset is not None
    assert len(ruleset["non_negotiable"]) >= 1  # §1 절대규칙 불릿 회수
    assert ruleset["identity"].get("role")       # §0 정체성 파싱
    assert any(p.startswith("PAT-") for p in ruleset["pattern_refs"])


# ③ 둘 다 없으면 (None,"none") graceful
def test_graceful_when_both_missing(monkeypatch, tmp_path):
    # 스냅샷 경로를 존재하지 않는 곳으로 돌려 폴백도 막는다(brain yaml 은 tmp 에 애초에 없음)
    monkeypatch.setattr(CR, "_SNAPSHOT", tmp_path / "nope" / "CORE-RULES.md")
    ruleset, source = CR.load_core_ruleset(use_cache=False)
    assert ruleset is None and source == "none"


# ④ build_digest — §0/1/5/패턴 + version 만, §2~4/6~9 전문 제외
def test_build_digest_minimal(tmp_path):
    _write_brain_yaml(tmp_path)
    ruleset, _ = CR.load_core_ruleset(use_cache=False)
    d = CR.build_digest(ruleset)
    assert set(d) == {"version", "identity", "non_negotiable", "safety", "pattern_refs"}
    assert d["version"] == "9.9.9"
    assert d["safety"]["capability_gating"].startswith("외부 쓰기")
    assert "coding_execution" not in d and "cost" not in d  # 전문 제외(비용 §6)


def test_build_digest_none_is_empty():
    d = CR.build_digest(None)
    assert d["non_negotiable"] == [] and d["identity"] == {} and d["version"] is None


# ⑤ inject 멱등 — 2회 호출해도 1회 효과
def test_inject_idempotent():
    env = {"data": {"x": 1}, "verification": {}, "provenance": {"sources_used": []}}
    CR.inject_core_rules(env, enabled=True)
    first_digest = env["core_rules_digest"]
    first_tools = env["available_tools"]
    CR.inject_core_rules(env, enabled=True)  # 재호출
    assert env["core_rules_digest"] is first_digest       # 재부착 안 함(동일 객체)
    assert env["available_tools"] is first_tools


# ⑥ enabled=False → no-op
def test_inject_disabled_noop():
    env = {"data": {}, "verification": {}, "provenance": {}}
    out = CR.inject_core_rules(env, enabled=False)
    assert "core_rules_digest" not in out and "available_tools" not in out


# ⑦ capability gating — 미부여 시 gated 도구 locked, 부여 시 해제
def test_available_tools_capability_gating():
    tools = {t["name"]: t for t in CR.available_tools(None)}
    assert tools["create"]["gated"] is True and tools["create"]["locked"] is True   # 쓰기 잠금
    assert tools["search"]["gated"] is False and tools["search"]["locked"] is False  # 읽기 개방
    granted = {t["name"]: t for t in CR.available_tools({"create"})}
    assert granted["create"]["locked"] is False   # 명시 승인 → 해제
    assert granted["update"]["locked"] is True     # 미승인 쓰기 도구는 여전히 잠금


# ⑧ get_context 옵트인 시 두 키 부착 / 미옵트인 시 봉투 불변
async def test_get_context_optin_injection(tmp_path):
    from adapters.memory_adapter import MemoryAdapter
    from core.router import SmartRouter
    from core.schema_validator import SchemaValidator
    from core import tools as T
    from core.tools import ToolContext

    adapters = {"memory": MemoryAdapter(base_dir=tmp_path)}
    ctx = ToolContext(adapters, SmartRouter(adapters), SchemaValidator())

    plain = await T.tool_get_context(ctx, "아무거나", {})
    assert "core_rules_digest" not in plain and "available_tools" not in plain  # 기본 off

    injected = await T.tool_get_context(ctx, "아무거나", {"inject_rules": True})
    assert "core_rules_digest" in injected and "available_tools" in injected
    # conftest 가 MEMORY_DIR=tmp 로 brain 부재 보장 → 스냅샷 폴백이 실제로 탔는지 핀박기(공허단정 방지)
    assert injected["core_rules_digest"]["source"] == "snapshot"


# ⑧-b tool_get_core_ruleset 직접 pull
async def test_tool_get_core_ruleset(tmp_path):
    from adapters.memory_adapter import MemoryAdapter
    from core.router import SmartRouter
    from core.schema_validator import SchemaValidator
    from core import tools as T
    from core.tools import ToolContext

    adapters = {"memory": MemoryAdapter(base_dir=tmp_path)}
    ctx = ToolContext(adapters, SmartRouter(adapters), SchemaValidator())
    env = await T.tool_get_core_ruleset(ctx)
    assert set(env) >= {"data", "verification", "provenance"}
    assert "core_rules_digest" in env["data"] and "available_tools" in env["data"]
    assert env["provenance"]["sources_used"][0].startswith("core-ruleset:")


# ── 적대리뷰 반영 회귀 ──────────────────────────────────────────
def test_snapshot_safety_is_dict():
    # 폴백(snapshot) 에서도 digest.safety 는 dict — brain 과 shape 일치(C3). list 였으면 소비자 깨짐.
    ruleset, source = CR.load_core_ruleset(use_cache=False)
    assert source == "snapshot"
    d = CR.build_digest(ruleset)
    assert isinstance(d["safety"], dict)
    assert "capability_gating" in d["safety"]  # §5 불릿이 named dict 로 정규화됨
    assert isinstance(d["identity"], dict) and d["identity"].get("role")


def test_capabilities_coercion_safe():
    # 잘못된 capabilities 타입이 크래시/오게이팅 못 하게(C2).
    assert CR.available_tools(5)          # int → 크래시 없이 목록 반환(빈 caps 취급)
    assert CR.available_tools(True)       # bool → 크래시 없음
    # str 단일 도구명은 문자셋 분해 없이 그 도구만 해제
    by = {t["name"]: t for t in CR.available_tools("create")}
    assert by["create"]["locked"] is False
    assert by["update"]["locked"] is True
    # 문자셋 분해 버그였다면 'c','r','e'... 로 아무 도구도 매칭 못 해 create 가 locked 였을 것


def test_is_truthy():
    for v in ("1", "true", "TRUE", "Yes", "on", " true ", True):
        assert CR.is_truthy(v) is True, v
    for v in ("0", "false", "False", "no", "", None, False, "off"):
        assert CR.is_truthy(v) is False, v


def test_tool_registry_matches_server():
    # _TOOL_REGISTRY 가 server.py 의 @mcp.tool 목록과 드리프트 없는지 가드(F5).
    # server 임포트 부작용을 피해 소스만 정규식 스캔.
    import re
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent / "server.py").read_text(encoding="utf-8")
    server_tools = set(re.findall(r"@mcp\.tool\(\)\s*\nasync def (\w+)\(", src))
    registry = {t["name"] for t in CR._TOOL_REGISTRY}
    assert registry == server_tools, f"드리프트: registry-server={registry - server_tools}, server-registry={server_tools - registry}"


def test_cache_not_stuck_on_snapshot(tmp_path):
    # snapshot 폴백은 캐시되지 않아, 이후 brain 이 붙으면 승격돼야 한다(C1 영구고착 방지).
    CR._cache = None
    r1, s1 = CR.load_core_ruleset()      # brain 부재(conftest tmp) → snapshot, 캐시 안 됨
    assert s1 == "snapshot" and CR._cache is None
    _write_brain_yaml(tmp_path)          # 이제 brain yaml 등장
    r2, s2 = CR.load_core_ruleset()      # 재호출 → brain 승격(고착 없음)
    assert s2 == "brain" and r2["version"] == "9.9.9"
