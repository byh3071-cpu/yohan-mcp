# -*- coding: utf-8 -*-
"""yohan-mcp v2 — Verifiability Engine (P3).

모든 도구 응답을 표준 검증 봉투로 감싼다(SW 3.0 "피부" — 결과에 검증을 동봉):

    {
      "data": ...,
      "diff": {"before": ..., "after": ...},          # create/update 등 변경 도구만
      "verification": {
        "schema_valid": bool|None,
        "rulebook_pass": bool,
        "cross_links_intact": bool,
        "contradiction_detected": bool,
        "quality_score": int|None   # 0~6 (엔티티 도구만; 집계 도구는 None=비적용)
      },
      "provenance": {"sources_used": [...], "reasoning_steps": [...]}
    }

P2 의 최소형 봉투(`{data, verification:{schema_valid}, provenance:{sources_used}}`)를
상위호환으로 확장한다 — 기존 키는 그대로 두고 필드만 더한다.

품질 체크리스트 6항목(quality_score) — 에이전트 결과물 품질 기준:
  1) schema_valid     : 전체 스키마 통과
  2) required_present  : 필수 필드가 전부 비어있지 않게 채워짐
  3) enums_valid       : enum/공유enum 참조 필드가 허용값
  4) provenance_present: 출처/FK 가 1개 이상 존재(또는 sources_used 비어있지 않음)
  5) timestamps_valid  : 날짜시각(*_at, format date-time) 필드가 RFC3339 로 파싱됨
  6) content_nonempty  : 핵심 텍스트 필드(title/본문 등)가 비어있지 않음
"""
from __future__ import annotations

from datetime import datetime

# ── 엔티티 메타 (node = "<backend>:<entity>") ───────────────────────
NODE_PK = {
    "notion:resource": "resource_id", "notion:summary": "summary_id",
    "notion:triple": "triple_id", "notion:ai-dict": "term_id",
    "notion:execution-log": "log_id", "memory:decision": "decision_id",
    "memory:ingest": "ingest_id", "memory:profile": "name",
    "studio:post": "post_id", "studio:product": "product_id",
}
# FK 필드 → 참조 노드 (해당 노드의 PK 로 쓰일 땐 FK 가 아님)
FK_FIELD_NODE = {
    "resource_id": "notion:resource",
    "summary_id": "notion:summary",
    "target_resource_id": "notion:resource",
}
# 출처/추적성 신호 필드
PROV_FIELDS = {"resource_id", "summary_id", "target_resource_id", "source_url", "notion_page_id"}
# 노드별 핵심 텍스트 필드(content_nonempty 검사)
CONTENT_FIELDS = {
    "studio:post": ["title", "body"], "notion:summary": ["title", "summary"],
    "notion:resource": ["title"], "memory:decision": ["decision", "rationale"],
    "memory:ingest": ["raw"], "notion:ai-dict": ["name", "definition"],
    "notion:triple": ["subject"], "notion:execution-log": ["content"],
    "studio:product": ["name"], "memory:profile": ["name"],
}
# 비즈니스 규칙(스키마와 별개) — rulebook_pass
_RULES = {
    "studio:post": [
        ("summary_id 필수(발행물 출처 추적)", lambda d: _nonempty(d.get("summary_id"))),
        ("body 필수(빈 발행 금지)", lambda d: _nonempty(d.get("body"))),
    ],
    "notion:summary": [
        ("resource_id 필수(원본 추적)", lambda d: _nonempty(d.get("resource_id"))),
        ("key_insights 비어있지 않음", lambda d: _nonempty(d.get("key_insights"))),
    ],
    "notion:resource": [
        ("source_url 필수(출처)", lambda d: _nonempty(d.get("source_url"))),
    ],
    "memory:decision": [
        ("rationale 필수(근거)", lambda d: _nonempty(d.get("rationale"))),
    ],
    "memory:ingest": [
        ("target_resource_id 필수(ingested_from)", lambda d: _nonempty(d.get("target_resource_id"))),
    ],
}


def _nonempty(val) -> bool:
    if val is None:
        return False
    if isinstance(val, str):
        return val.strip() != ""
    if isinstance(val, (list, dict)):
        return len(val) > 0
    return True


def _is_datetime(val) -> bool:
    if not isinstance(val, str) or not val.strip():
        return False
    try:
        datetime.fromisoformat(val.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def node_of(type_: str, validator) -> str:
    """타입 → "<backend>:<entity>" 노드. backend_of 로 백엔드 해소."""
    short = type_.split(":", 1)[1] if ":" in type_ else type_
    backend = validator.backend_of(type_)
    return f"{backend}:{short}" if backend else type_


# ── 6항목 품질 체크리스트 ───────────────────────────────────────────
def quality_checklist(type_: str, data: dict, validator, *, sources_used=None) -> dict:
    """6개 불리언 체크 → {항목: bool}. quality_score 는 sum()."""
    data = data or {}
    node = node_of(type_, validator)
    schema = validator.schema_for(type_) or {}
    props = schema.get("properties", {})
    required = schema.get("required", [])
    shared = validator.shared_enums()

    schema_valid, _ = validator.validate(type_, data)
    required_present = all(_nonempty(data.get(k)) for k in required) if required else True

    # enum 필드(인라인 enum + 공유enum $ref) 허용값 검사
    enums_valid = True
    for field, spec in props.items():
        if field not in data or data[field] is None:
            continue
        allowed = None
        if isinstance(spec, dict) and "enum" in spec:
            allowed = spec["enum"]
        elif isinstance(spec, dict) and "$ref" in spec:
            ref = spec["$ref"]
            name = ref.split("/$defs/")[-1] if "/$defs/" in ref else None
            allowed = shared.get(name) if name else None
        if allowed is not None and data[field] not in allowed:
            enums_valid = False
            break

    prov_present = bool(sources_used) or any(_nonempty(data.get(f)) for f in PROV_FIELDS)

    dt_fields = [f for f, sp in props.items()
                 if (isinstance(sp, dict) and sp.get("format") == "date-time") or f.endswith("_at")]
    timestamps_valid = all(_is_datetime(data[f]) for f in dt_fields if f in data and data[f] is not None)

    content = CONTENT_FIELDS.get(node, ["title"])
    present_content = [f for f in content if f in props or f in data]
    content_nonempty = all(_nonempty(data.get(f)) for f in present_content) if present_content else True

    return {
        "schema_valid": schema_valid,
        "required_present": required_present,
        "enums_valid": enums_valid,
        "provenance_present": prov_present,
        "timestamps_valid": timestamps_valid,
        "content_nonempty": content_nonempty,
    }


# ── rulebook / cross-links / contradiction ─────────────────────────
def rulebook_check(type_: str, data: dict, validator) -> tuple[bool, list[str]]:
    node = node_of(type_, validator)
    violations = [msg for msg, ok in _RULES.get(node, []) if not ok(data or {})]
    return (len(violations) == 0), violations


def _link_exists(schema_links: list[dict], a: str, b: str) -> bool:
    """schema _links 에 노드 a↔b 를 잇는 관계가 선언돼 있나(방향 무관, 와일드카드 허용)."""
    def match(pat: str, node: str) -> bool:
        if pat == node:
            return True
        # "notion:*" 와일드카드
        return pat.endswith(":*") and node.split(":", 1)[0] == pat.split(":", 1)[0]

    for link in schema_links:
        s, t = link.get("source", ""), link.get("target", "")
        if (match(s, a) and match(t, b)) or (match(s, b) and match(t, a)):
            return True
    return False


def cross_links_intact(type_: str, data: dict, validator, schema_links: list[dict]) -> bool:
    """data 의 FK 필드마다 schema _links 에 대응 관계가 선언돼 있으면 무결."""
    node = node_of(type_, validator)
    data = data or {}
    for field, ref_node in FK_FIELD_NODE.items():
        if field == NODE_PK.get(node):
            continue  # 자기 PK 는 FK 아님
        if not _nonempty(data.get(field)):
            continue
        if not _link_exists(schema_links, node, ref_node):
            return False
    return True


def detect_contradiction(type_: str, data: dict, validator) -> bool:
    """결정적 모순 규칙. 하나라도 걸리면 True."""
    node = node_of(type_, validator)
    data = data or {}
    status = data.get("status")
    if node == "studio:post" and status == "발행" and not _nonempty(data.get("body")):
        return True
    if node == "notion:summary" and status == "완료" and not _nonempty(data.get("key_insights")):
        return True
    if node == "memory:decision" and status in ("승인", "기각") and not _nonempty(data.get("rationale")):
        return True
    c = data.get("confidence")
    if isinstance(c, (int, float)) and not (0.0 <= float(c) <= 1.0):
        return True
    return False


# ── 표준 봉투 ───────────────────────────────────────────────────────
def verify_entity(type_: str, data: dict, validator, schema_links: list[dict], *, sources_used=None) -> dict:
    """엔티티 1건에 대한 풀 verification 블록(5필드 + 6항목 점수)."""
    checklist = quality_checklist(type_, data, validator, sources_used=sources_used)
    rb_pass, _ = rulebook_check(type_, data, validator)
    return {
        "schema_valid": checklist["schema_valid"],
        "rulebook_pass": rb_pass,
        "cross_links_intact": cross_links_intact(type_, data, validator, schema_links),
        "contradiction_detected": detect_contradiction(type_, data, validator),
        "quality_score": sum(1 for v in checklist.values() if v),  # 0~6
        "quality_checklist": checklist,
    }


def make_envelope(
    data,
    *,
    sources_used,
    schema_valid=None,
    type_: str | None = None,
    validator=None,
    schema_links: list[dict] | None = None,
    entity: dict | None = None,
    reasoning_steps: list[str] | None = None,
    diff: dict | None = None,
    errors: list | None = None,
    **extra,
) -> dict:
    """표준 검증 봉투 생성.

    type_+validator 가 주어지면 풀 verification 을 계산한다. 검증 대상은 `entity`
    (없으면 data 가 dict 일 때 data) — 봉투의 data 가 레코드 래퍼여도 본문을 검증한다.
    type_ 가 없으면(집계/조회 도구) 중립 기본값으로 5필드를 채운다(quality_score=None=비적용).
    """
    verify_target = entity if entity is not None else (data if isinstance(data, dict) else None)
    verification: dict
    if type_ and validator is not None and verify_target is not None:
        verification = verify_entity(
            type_, verify_target, validator, schema_links or [], sources_used=sources_used
        )
    else:
        verification = {
            "schema_valid": schema_valid,
            "rulebook_pass": True,
            "cross_links_intact": True,
            "contradiction_detected": False,
            "quality_score": None,  # 비적용(집계/조회)
        }
    if schema_valid is not None:
        verification["schema_valid"] = schema_valid  # 호출자 명시값 우선

    provenance = {"sources_used": sources_used}
    if reasoning_steps is not None:
        provenance["reasoning_steps"] = reasoning_steps

    env = {"data": data, "verification": verification, "provenance": provenance}
    if diff is not None:
        env["diff"] = diff
    if errors is not None:
        env["errors"] = errors
    env.update(extra)
    return env
