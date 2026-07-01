# -*- coding: utf-8 -*-
"""ADR-008 B.3 — MemoryAdapter 가 brain .md+frontmatter 를 읽어 회수에 노출하는지 검증.

읽기 전용: 지식 폴더 allowlist 의 .md 만 순회, type=brain:<folder>(스키마 없음). 쓰기 무변경.
"""
from adapters.memory_adapter import MemoryAdapter
from core.router import SmartRouter
from core.schema_validator import SchemaValidator
from core import tools as T
from core.tools import ToolContext


def _seed_brain(base):
    """tmp 에 brain 지식 폴더 + 제외 폴더 + 레거시 yaml 을 깐다."""
    (base / "decisions").mkdir(parents=True, exist_ok=True)
    (base / "wiki" / "concepts").mkdir(parents=True, exist_ok=True)
    (base / "ingest").mkdir(parents=True, exist_ok=True)
    (base / "logs" / "sessions").mkdir(parents=True, exist_ok=True)
    (base / "metrics").mkdir(parents=True, exist_ok=True)

    # 지식 md (frontmatter 있음)
    (base / "decisions" / "d1.md").write_text(
        "---\nid: dec-attn\ntitle: 어텐션 결정\ntags: [ai, attention]\n---\n\n어텐션 메커니즘 파이프라인 검증.",
        encoding="utf-8")
    # 지식 md (frontmatter 없음 — 전문이 body)
    (base / "wiki" / "concepts" / "w1.md").write_text(
        "어텐션은 쿼리 키 값 내적으로 관련도를 계산한다.", encoding="utf-8")
    # 지식 md (frontmatter 깨짐 — graceful, body 유지)
    (base / "ingest" / "i1.md").write_text(
        "---\n: : : 깨진 yaml [\n---\n\n어텐션 청크 크기 재현율 실험 노트.", encoding="utf-8")
    # 제외 폴더 md (노출되면 안 됨)
    (base / "logs" / "sessions" / "s1.md").write_text(
        "---\ntitle: 세션로그\n---\n어텐션 세션 잡음.", encoding="utf-8")
    (base / "metrics" / "m1.md").write_text("어텐션 지표 잡음.", encoding="utf-8")
    # 레거시 yaml 레이아웃 (기존 동작 회귀 보존용)
    (base / "decisions" / "legacy.yaml").write_text(
        "decision_id: dec-legacy\ntitle: 레거시 결정\ndecision: 어텐션 유지\n", encoding="utf-8")


async def test_brain_md_surfaces_in_search(tmp_path):
    _seed_brain(tmp_path)
    qa = MemoryAdapter(base_dir=tmp_path)
    res = await qa.search("어텐션")
    types = {r["type"] for r in res}
    ids = {r["id"] for r in res}
    # 지식 md 3종 회수(frontmatter 있음/없음/깨짐 전부)
    assert "brain:decisions" in types
    assert "dec-attn" in ids               # frontmatter id 사용
    assert any(r["type"] == "brain:wiki" for r in res)     # frontmatter 없어도 body 검색
    assert any(r["type"] == "brain:ingest" for r in res)   # 깨진 frontmatter graceful
    # 레거시 yaml 도 여전히 회수(회귀 보존)
    assert "decision" in types and "dec-legacy" in ids


async def test_excluded_folders_not_surfaced(tmp_path):
    _seed_brain(tmp_path)
    qa = MemoryAdapter(base_dir=tmp_path)
    res = await qa.search("어텐션")  # logs/metrics 에도 "어텐션" 있음
    paths = {r["data"].get("_path", "") for r in res}
    assert not any(p.startswith("logs/") for p in paths)     # 세션로그 제외
    assert not any(p.startswith("metrics/") for p in paths)  # 지표 제외
    assert all(not str(r["type"]).startswith("brain:logs") for r in res)


async def test_frontmatter_parse_variants(tmp_path):
    (tmp_path / "wiki").mkdir(parents=True)
    (tmp_path / "wiki" / "a.md").write_text(
        "---\nid: x\ntitle: T\n---\n본문 내용", encoding="utf-8")
    (tmp_path / "wiki" / "b.md").write_text("프론트매터 없는 순수 본문", encoding="utf-8")
    qa = MemoryAdapter(base_dir=tmp_path)
    a = qa._read_md(tmp_path / "wiki" / "a.md")
    assert a["id"] == "x" and a["title"] == "T" and a["body"] == "본문 내용"
    b = qa._read_md(tmp_path / "wiki" / "b.md")
    assert b == {"body": "프론트매터 없는 순수 본문"}  # frontmatter 없으면 body 만


async def test_brain_md_type_is_schemaless_no_validation_pollution(tmp_path):
    _seed_brain(tmp_path)
    adapters = {"memory": MemoryAdapter(base_dir=tmp_path)}
    ctx = ToolContext(adapters, SmartRouter(adapters), SchemaValidator())
    env = await T.tool_search(ctx, "어텐션")
    # brain:* 타입은 스키마 없음 → 검증 대상 아님 → schema_valid 오염 없음
    assert env["verification"]["schema_valid"] is True
    assert any(str(r["type"]).startswith("brain:") for r in env["data"]["results"])
    # 스키마 검증기가 brain:* 타입엔 스키마를 안 준다
    assert ctx.validator.schema_for("brain:decisions") is None


async def test_mtime_cache_avoids_reparse(tmp_path):
    (tmp_path / "wiki").mkdir(parents=True)
    p = tmp_path / "wiki" / "c.md"
    p.write_text("---\nid: c\n---\n본문", encoding="utf-8")
    qa = MemoryAdapter(base_dir=tmp_path)
    first = qa._read_md_cached(p)
    second = qa._read_md_cached(p)
    assert first is second                    # mtime 동일 → 동일 캐시 객체(재파싱 회피)
    assert str(p) in qa._md_cache


async def test_get_context_includes_brain_md(tmp_path):
    _seed_brain(tmp_path)
    adapters = {"memory": MemoryAdapter(base_dir=tmp_path)}
    ctx = ToolContext(adapters, SmartRouter(adapters), SchemaValidator())
    env = await T.tool_get_context(ctx, "어텐션", {"top_k": 20})
    brain_matches = [m for m in env["data"]["matches"] if str(m["type"]).startswith("brain:")]
    assert brain_matches                       # router 경유로 brain 지식노트가 matches 에 포함
    assert any(m["data"].get("_path") for m in brain_matches)  # provenance(_path) 보존
