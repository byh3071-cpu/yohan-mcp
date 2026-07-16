# -*- coding: utf-8 -*-
"""brain 코어룰셋 로더·다이제스트·주입 (ADR-008 P0).

yohan-mcp(배관)가 brain(원천)의 범용 코어 독트린(`memory/core/core-ruleset.yaml`)을 읽어
도구 응답에 주입한다. `get_core_ruleset()`으로 직접 pull, `inject_core_rules()`로 봉투에 부착.
brain 미연결(YOHAN_BRAIN_ROOT/MEMORY_DIR 미설정)이면 인레포 스냅샷(`.agents/CORE-RULES.md`)으로
graceful 폴백 — CI/오프라인에서도 동작. 둘 다 없으면 (None, "none")로 크래시 없이 비운다.

주입은 **옵트인·멱등·capability gating**: 기본 off, 두 번 부착해도 1회, 쓰기 도구는 기본 잠금.
다이제스트는 **토큰 미니멀** — 전문이 아니라 §0(정체성)·§1(절대규칙)·§5(안전)·패턴참조만(비용 §6).
"""
from __future__ import annotations

import logging
import re

import yaml

from core.paths import ROOT, resolve_memory_dir

logger = logging.getLogger(__name__)

_SNAPSHOT = ROOT / ".agents" / "CORE-RULES.md"
_cache: tuple[dict | None, str] | None = None

# 도구 카탈로그 — server.py 의 @mcp.tool 과 1:1 (도구 추가·삭제 시 함께 갱신).
# gated=외부 쓰기·배포·삭제·되돌리기어려움(기본 잠금, CORE-RULES §5 capability_gating). open=읽기.
_TOOL_REGISTRY: list[dict] = [
    {"name": "search", "purpose": "통합 검색(RRF 융합)", "gated": False},
    {"name": "get_context", "purpose": "질의 관련 엔티티+관계+역방향 회수", "gated": False},
    {"name": "get_core_ruleset", "purpose": "코어 독트린 다이제스트", "gated": False},
    {"name": "get_agent_roster", "purpose": "에이전트 편성표 축소 digest", "gated": False},
    {"name": "status", "purpose": "백엔드 헬스 요약", "gated": False},
    {"name": "policy", "purpose": "정책·일일한도 조회", "gated": False},
    {"name": "list_triggers", "purpose": "트리거 카탈로그(읽기)", "gated": False},
    {"name": "plan", "purpose": "프로토콜 dry plan(실행 안 함)", "gated": False},
    {"name": "check", "purpose": "스키마 검증+품질점수", "gated": False},
    {"name": "create", "purpose": "엔티티 생성", "gated": True},
    {"name": "update", "purpose": "엔티티 부분 갱신", "gated": True},
    {"name": "ingest", "purpose": "URL 수집 3중 적재", "gated": True},
    {"name": "publish", "purpose": "Studio 블로그 발행", "gated": True},
    {"name": "run_action", "purpose": "프로토콜 체인 실행", "gated": True},
    {"name": "approve", "purpose": "승인 게이트 결정", "gated": True},
    {"name": "run_trigger", "purpose": "트리거 실행", "gated": True},
    {"name": "fire_due_triggers", "purpose": "due 트리거 발화", "gated": True},
    {"name": "webhook", "purpose": "웹훅 수신 발화", "gated": True},
]


# ── 로더 ────────────────────────────────────────────────────────
def load_core_ruleset(*, use_cache: bool = True) -> tuple[dict | None, str]:
    """코어룰셋 로드: brain yaml(원천) → 인레포 스냅샷 폴백 → (None,"none").

    반환 (ruleset_dict, source). source ∈ {"brain","snapshot","none"}.
    """
    global _cache
    if use_cache and _cache is not None:
        return _cache

    result: tuple[dict | None, str] = (None, "none")
    # 1) brain 원천 (YAML)
    yaml_path = resolve_memory_dir() / "core" / "core-ruleset.yaml"
    try:
        if yaml_path.exists():
            data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                result = (data, "brain")
    except Exception as exc:  # 파싱 실패가 도구 전체를 죽이지 않음
        logger.warning("core-ruleset.yaml 로드 실패: %s: %s", type(exc).__name__, exc)

    # 2) 인레포 스냅샷 폴백 (.agents/CORE-RULES.md)
    if result[0] is None:
        try:
            if _SNAPSHOT.exists():
                parsed = _parse_snapshot(_SNAPSHOT.read_text(encoding="utf-8"))
                if parsed:
                    result = (parsed, "snapshot")
        except Exception as exc:
            logger.warning("CORE-RULES.md 스냅샷 파싱 실패: %s: %s", type(exc).__name__, exc)

    # 캐시는 **brain 성공만** 고정한다. snapshot/none(폴백·실패)은 캐시하지 않아,
    # 부팅 시 brain FS 가 아직 없어 폴백됐다가 이후 brain 이 붙으면 다음 호출에서 승격된다
    # (일시 오류의 영구 고착 방지 — HIGH). brain yaml 은 작아 매 호출 재읽기 비용도 낮다.
    if use_cache and result[1] == "brain":
        _cache = result
    return result


def _parse_snapshot(text: str) -> dict | None:
    """생성 스냅샷(.agents/CORE-RULES.md)에서 최소 구조를 회수(brain yaml 폴백용).

    START/END 델리미터 사이의 ## 섹션 불릿을 헤더로 분류: §0 정체성·§1 절대규칙·§5 안전·패턴참조.
    """
    mver = re.search(r"CORE-RULES:START\s+v([0-9][0-9.]*)", text)
    version = mver.group(1) if mver else None
    s, e = text.find("CORE-RULES:START"), text.find("CORE-RULES:END")
    body = text[s:e] if (s != -1 and e != -1) else text

    sections: dict[str, list[str]] = {}
    cur: str | None = None
    for line in body.splitlines():
        line = line.strip()
        mh = re.match(r"^##\s+(.*)$", line)
        if mh:
            cur = mh.group(1).strip()
            sections[cur] = []
            continue
        mb = re.match(r"^-\s+(.*)$", line)
        if cur is not None and mb:
            sections[cur].append(mb.group(1).strip())

    def _find(*keys: str) -> list[str]:
        # 헤더 번호(예 "1.")나 키워드(예 "절대") 어느 쪽이든 매칭 — 재번호·개명에 견고.
        for k, v in sections.items():
            if any(key in k for key in keys):
                return v
        return []

    def _bold_bullets_to_dict(bullets: list[str]) -> dict[str, str]:
        # "**key:** value" 형태 불릿을 dict 로 정규화(§0 정체성·§5 안전 동형).
        out: dict[str, str] = {}
        for b in bullets:
            mi = re.match(r"\*\*([^:*]+):\*\*\s*(.*)$", b)
            if mi:
                out[mi.group(1).strip()] = mi.group(2).strip()
        return out

    parsed = {
        "version": version,
        "identity": _bold_bullets_to_dict(_find("0.", "정체")),
        "non_negotiable": _find("1.", "절대"),
        # §5 안전 불릿도 dict 로 정규화 → digest.safety 타입을 brain(dict)과 일치시킨다(shape 안정, C3).
        "safety": _bold_bullets_to_dict(_find("5.", "안전")),
        "pattern_refs": [b.split()[0] for b in _find("패턴") if b.strip()],
    }
    # 최소한 절대규칙이라도 있어야 유효 스냅샷으로 인정
    return parsed if parsed["non_negotiable"] else None


# ── 다이제스트 ──────────────────────────────────────────────────
def build_digest(ruleset: dict | None) -> dict:
    """주입용 토큰-미니멀 다이제스트 — §0 정체성·§1 절대규칙·§5 안전·패턴참조 + version 만.

    §2~4·6~9 전문은 제외(비용 규칙 §6). ruleset 키는 방어적 접근(없으면 빈 값).
    safety 는 brain=dict(named rules) / snapshot=list(불릿) 둘 다 수용.
    """
    if not isinstance(ruleset, dict):
        return {"version": None, "identity": {}, "non_negotiable": [], "safety": {}, "pattern_refs": []}
    safety = ruleset.get("safety", {})
    if not isinstance(safety, (dict, list)):
        safety = {}
    return {
        "version": ruleset.get("version"),
        "identity": ruleset.get("identity") or {},
        "non_negotiable": list(ruleset.get("non_negotiable") or []),
        "safety": safety,
        "pattern_refs": list(ruleset.get("pattern_refs") or []),
    }


# ── 헬퍼 ────────────────────────────────────────────────────────
def is_truthy(v) -> bool:
    """옵트인 값의 일관된 truthy 판정 — 닫힌집합 allowlist 대조(PAT-001).

    bool 은 그대로, 문자열은 소문자·strip 후 {1,true,yes,on} 만 참(대문자·별칭·"false" 문자열 안전).
    """
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _coerce_caps(capabilities) -> set[str]:
    """capabilities 를 도구명 집합으로 안전 강제 — 잘못된 타입이 크래시/오게이팅 못 하게(C2).

    None→빈집합, str→단일원소(문자셋 분해 방지), 이터러블→str 집합, 그 외→빈집합.
    """
    if capabilities is None:
        return set()
    if isinstance(capabilities, str):
        return {capabilities}
    try:
        return {str(c) for c in capabilities}
    except TypeError:
        return set()


# ── 도구 카탈로그 + capability gating ───────────────────────────
def available_tools(capabilities=None) -> list[dict]:
    """도구 카탈로그 + capability gating. capabilities(허용 도구명 집합) 없으면 gated 도구는 locked.

    CORE-RULES §5: "외부 쓰기·배포·삭제 도구는 기본 잠금 → 명시 승인 시에만. 선제 호출 금지."
    잠긴 도구도 목록엔 남긴다(에이전트가 존재·잠금을 인지하도록) — 다만 locked=True 표식.
    """
    caps = _coerce_caps(capabilities)
    out: list[dict] = []
    for t in _TOOL_REGISTRY:
        locked = bool(t["gated"]) and t["name"] not in caps
        out.append({"name": t["name"], "purpose": t["purpose"], "gated": t["gated"], "locked": locked})
    return out


# ── 주입 (옵트인·멱등) ──────────────────────────────────────────
def inject_core_rules(envelope: dict, *, enabled: bool, capabilities=None) -> dict:
    """표준봉투에 core_rules_digest·available_tools 를 부착(제자리 변형). 옵트인·멱등.

    - enabled=False → no-op(원본 그대로). 기본 off라 미옵트인 호출자는 봉투 불변.
    - 이미 두 키가 있으면 재부착 안 함(멱등 — 중복 주입/토큰 낭비 방지).
    """
    if not enabled or not isinstance(envelope, dict):
        return envelope
    if "core_rules_digest" in envelope:  # 멱등 마커 단일키(부분상태 클로버링 방지, C6)
        return envelope
    ruleset, source = load_core_ruleset()
    digest = build_digest(ruleset)
    digest["source"] = source
    envelope["core_rules_digest"] = digest
    envelope["available_tools"] = available_tools(capabilities)
    return envelope
