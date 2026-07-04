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


# ── 적대리뷰 반영 회귀 ──────────────────────────────────────────
async def test_nested_same_stem_not_collapsed(tmp_path):
    # 중첩 폴더 동명 파일(frontmatter id 없음)이 stem 충돌로 RRF dedup 에서 소실되면 안 됨(HIGH).
    (tmp_path / "wiki" / "2024").mkdir(parents=True)
    (tmp_path / "wiki" / "2025").mkdir(parents=True)
    (tmp_path / "wiki" / "2024" / "intro.md").write_text("어텐션 2024 버전 노트", encoding="utf-8")
    (tmp_path / "wiki" / "2025" / "intro.md").write_text("어텐션 2025 버전 노트", encoding="utf-8")
    adapters = {"memory": MemoryAdapter(base_dir=tmp_path)}
    ctx = ToolContext(adapters, SmartRouter(adapters), SchemaValidator())
    env = await T.tool_get_context(ctx, "어텐션", {"top_k": 20})
    ids = {m["id"] for m in env["data"]["matches"] if str(m["type"]).startswith("brain:")}
    assert len(ids) == 2  # 경로 기반 id 라 둘 다 보존(stem 이면 'intro' 하나로 접혔을 것)


def test_read_md_body_starting_with_hr_not_lost(tmp_path):
    # 본문 첫 줄이 수평선 '---' 이고 frontmatter 가 아닌 md → 본문 손실/필드 오주입 없어야 함(MED-HIGH).
    p = tmp_path / "x.md"
    p.write_text("---\n제목 아닌 그냥 텍스트\n---\n실제 본문 어텐션", encoding="utf-8")
    rec = MemoryAdapter._read_md(p)
    assert "제목 아닌 그냥 텍스트" in rec["body"]  # 앞부분 안 잘림
    assert "실제 본문 어텐션" in rec["body"]
    assert set(rec) == {"body"}  # 스칼라를 frontmatter 필드로 주입 안 함


async def test_mtime_cache_invalidates_on_change(tmp_path):
    import os
    (tmp_path / "wiki").mkdir(parents=True)
    p = tmp_path / "wiki" / "c.md"
    p.write_text("---\nid: c\n---\n원본 어텐션", encoding="utf-8")
    qa = MemoryAdapter(base_dir=tmp_path)
    r1 = qa._read_md_cached(p)
    assert "원본" in r1["body"]
    p.write_text("---\nid: c\n---\n수정 어텐션", encoding="utf-8")
    mt = p.stat().st_mtime + 10
    os.utime(p, (mt, mt))  # mtime 강제 변경(동초 stale 회피)
    r2 = qa._read_md_cached(p)
    assert "수정" in r2["body"] and r2 is not r1  # 변경 반영(무효화)


async def test_more_excluded_folders(tmp_path):
    # ops·inbox·templates·core 도 노출 안 됨(주석 allowlist 와 정합).
    for d in ("ops", "inbox", "templates", "core"):
        (tmp_path / d).mkdir(parents=True)
        (tmp_path / d / "n.md").write_text(f"어텐션 {d} 잡음", encoding="utf-8")
    qa = MemoryAdapter(base_dir=tmp_path)
    res = await qa.search("어텐션")
    assert res == []  # 지식 폴더 아님 → 하나도 안 잡힘


async def test_multiword_substring_limitation_documented(tmp_path):
    # memory search 는 전체쿼리 연속 substring — 다어절(비연속)은 0건이 될 수 있음(의미검색은 Qdrant).
    (tmp_path / "wiki").mkdir(parents=True)
    (tmp_path / "wiki" / "a.md").write_text("알파 중간말 베타", encoding="utf-8")
    qa = MemoryAdapter(base_dir=tmp_path)
    assert len(await qa.search("알파")) == 1          # 단일 토큰 매칭
    assert await qa.search("알파 베타") == []          # 비연속 다어절 → 0(한계 명시)


async def test_count_ranking(tmp_path):
    # blob 매칭 빈도(count) 내림차순 = 백엔드 내 순위.
    (tmp_path / "wiki").mkdir(parents=True)
    (tmp_path / "wiki" / "hi.md").write_text("어텐션 어텐션 어텐션 많음", encoding="utf-8")
    (tmp_path / "wiki" / "lo.md").write_text("어텐션 적음", encoding="utf-8")
    qa = MemoryAdapter(base_dir=tmp_path)
    res = await qa.search("어텐션")
    assert res[0]["data"]["_path"].endswith("hi.md")  # 빈도 높은 노트가 상위


# ── brain_memory 벡터 시딩용 _read_md_uncapped (스프린트 필수 코어 ②) ──────
def test_read_md_uncapped_no_truncation(tmp_path):
    long_body = "가나다라" * 2000  # 8000자 > _MD_BODY_MAX(4000)
    p = tmp_path / "long.md"
    p.write_text(f"---\nid: long\n---\n{long_body}", encoding="utf-8")
    capped = MemoryAdapter._read_md(p)
    uncapped = MemoryAdapter._read_md_uncapped(p)
    assert len(capped["body"]) == 4000
    assert uncapped["body"] == long_body
    assert "_body_start_line" not in capped  # _read_md 계약 불변(엑스트라 키 없음)


def test_read_md_uncapped_body_start_line_after_frontmatter(tmp_path):
    p = tmp_path / "fm.md"
    p.write_text("---\nid: x\ntitle: T\n---\n\n첫 본문 줄", encoding="utf-8")
    rec = MemoryAdapter._read_md_uncapped(p)
    # frontmatter 4줄(1~4) + 빈줄(5번째, strip 으로 사라짐) → 첫 비공백 본문줄은 파일 6번째 줄
    assert rec["_body_start_line"] == 6
    assert rec["body"] == "첫 본문 줄"


def test_read_md_uncapped_no_frontmatter_starts_at_line_1(tmp_path):
    p = tmp_path / "plain.md"
    p.write_text("바로 본문 시작", encoding="utf-8")
    rec = MemoryAdapter._read_md_uncapped(p)
    assert rec["_body_start_line"] == 1


def test_read_md_uncapped_broken_frontmatter_starts_at_line_1(tmp_path):
    p = tmp_path / "broken.md"
    p.write_text("---\n제목 아닌 텍스트\n---\n실제 본문", encoding="utf-8")
    rec = MemoryAdapter._read_md_uncapped(p)
    assert rec["_body_start_line"] == 1  # 파싱 실패 → 전문이 body, 1번째 줄부터
    assert "실제 본문" in rec["body"]
