# -*- coding: utf-8 -*-
"""PHASE 3 — 역방향 회수: NotionAdapter.devlog_query / pattern_query + get_context 통합.

ADR-008: get_context 통일 타깃 = yohan-mcp(Python). Dev Log DB(f8e79e6c)·패턴 DB(19509808)를
쿼리해 과거 경험/재사용 패턴을 컨텍스트에 주입한다(양방향 루프의 회수 측).
"""
import json as _json
import logging

import httpx

from adapters.memory_adapter import MemoryAdapter
from adapters.notion_adapter import NotionAdapter
from core.router import SmartRouter
from core.schema_validator import SchemaValidator
from core import tools as T
from core.tools import ToolContext


def _devlog_page() -> dict:
    return {
        "id": "pg_dl_1",
        "properties": {
            "이름": {"type": "title", "title": [{"plain_text": "(2026-06-11) VHK - 거버넌스 T1~T5"}]},
            "유형": {"type": "select", "select": {"name": "세션로그"}},
            "교훈": {"type": "rich_text", "rich_text": [{"plain_text": "적대검증이 Windows CI 전멸을 잡음"}]},
            "관련 파일": {"type": "rich_text", "rich_text": [{"plain_text": "scripts/check-records.mjs"}]},
            "SoT Key": {"type": "rich_text", "rich_text": [{"plain_text": "vhk/2026-06-11/gov"}]},
            "프로젝트": {"type": "select", "select": {"name": "VHK"}},
        },
    }


def _pattern_page() -> dict:
    return {
        "id": "pg_pat_1",
        "properties": {
            "패턴명": {"type": "title", "title": [{"plain_text": "LLM JSON raw parse 금지"}]},
            "증상": {"type": "rich_text", "rich_text": [{"plain_text": "json.loads(content) 크래시"}]},
            "원인": {"type": "rich_text", "rich_text": [{"plain_text": "LLM 출력에 코드펜스/잡설 혼입"}]},
            "해결": {"type": "rich_text", "rich_text": [{"plain_text": "추출기+검증 거쳐 파싱"}]},
            "적용 조건": {"type": "rich_text", "rich_text": [{"plain_text": "LLM이 JSON 반환 시"}]},
            "카테고리": {"type": "select", "select": {"name": "MCP/연동"}},
        },
    }


# ── devlog_query ────────────────────────────────────────────────
async def test_devlog_query_mocked_filters_and_maps():
    captured = {}

    def handler(request):
        captured["path"] = request.url.path
        captured["body"] = _json.loads(request.content)
        return httpx.Response(200, json={"results": [_devlog_page()]})

    client = httpx.AsyncClient(base_url="https://api.notion.com/v1", transport=httpx.MockTransport(handler))
    a = NotionAdapter(client=client, token="t")
    a.devlog_db_id = "devlogdb"
    rows = await a.devlog_query("VHK")
    await client.aclose()

    # 매핑 — 5개 필드만 추출
    assert len(rows) == 1
    r = rows[0]
    assert set(r) == {"이름", "유형", "교훈", "관련 파일", "SoT Key"}
    assert "VHK" in r["이름"] and r["유형"] == "세션로그"
    assert r["SoT Key"] == "vhk/2026-06-11/gov"
    # 요청 — Dev Log DB 경로 + 프로젝트/유형 필터 + 실행일 DESC + limit 10
    assert captured["path"].endswith("/databases/devlogdb/query")
    body = captured["body"]
    assert body["page_size"] == 10
    assert body["sorts"] == [{"property": "실행일", "direction": "descending"}]
    conds = body["filter"]["and"]
    assert {"property": "프로젝트", "select": {"equals": "VHK"}} in conds
    type_or = next(c["or"] for c in conds if "or" in c)
    type_names = {t["select"]["equals"] for t in type_or}
    assert type_names == {"세션로그", "상태요약", "핸드오프", "ADR"}


async def test_devlog_query_graceful_when_missing():
    # 토큰 없음 → []
    assert await NotionAdapter(token="").devlog_query("VHK") == []
    # DB ID 없음 → []
    a = NotionAdapter(token="t")
    a.devlog_db_id = None
    assert await a.devlog_query("VHK") == []
    # 프로젝트명 없음 → [] (에러 아님)
    a.devlog_db_id = "db"
    assert await a.devlog_query("") == []
    assert await a.devlog_query(None) == []


# ── pattern_query ───────────────────────────────────────────────
async def test_pattern_query_mocked_filters_and_maps():
    captured = {}

    def handler(request):
        captured["body"] = _json.loads(request.content)
        captured["path"] = request.url.path
        return httpx.Response(200, json={"results": [_pattern_page()]})

    client = httpx.AsyncClient(base_url="https://api.notion.com/v1", transport=httpx.MockTransport(handler))
    a = NotionAdapter(client=client, token="t")
    a.pattern_db_id = "patterndb"
    rows = await a.pattern_query(category="MCP/연동", tags=["MCP"])
    await client.aclose()

    assert len(rows) == 1
    r = rows[0]
    assert set(r) == {"패턴명", "증상", "원인", "해결", "적용 조건"}
    assert r["패턴명"] == "LLM JSON raw parse 금지"
    assert captured["path"].endswith("/databases/patterndb/query")
    conds = captured["body"]["filter"]["and"]
    assert {"property": "카테고리", "select": {"equals": "MCP/연동"}} in conds
    tag_or = next(c["or"] for c in conds if "or" in c)
    assert tag_or == [{"property": "태그", "multi_select": {"contains": "MCP"}}]


async def test_pattern_query_translates_code_category_to_db_option():
    """코드값(git 등)은 실DB select 옵션(Git/배포)으로 번역돼 나가야 한다(400 방지)."""
    captured = {}

    def handler(request):
        captured["body"] = _json.loads(request.content)
        return httpx.Response(200, json={"results": []})

    client = httpx.AsyncClient(base_url="https://api.notion.com/v1", transport=httpx.MockTransport(handler))
    a = NotionAdapter(client=client, token="t")
    a.pattern_db_id = "patterndb"
    await a.pattern_query(category="git")
    await client.aclose()
    assert captured["body"]["filter"] == {"property": "카테고리", "select": {"equals": "Git/배포"}}


async def test_pattern_query_unknown_category_raises():
    """코드값도 실DB 옵션도 아니면 명시 실패(무검증 주입 → Notion 400 무음 0건 방지)."""
    import pytest

    a = NotionAdapter(token="t")
    a.pattern_db_id = "patterndb"
    with pytest.raises(ValueError):
        await a.pattern_query(category="존재하지않는카테고리")


async def test_pattern_query_no_filter_omits_filter_key():
    captured = {}

    def handler(request):
        captured["body"] = _json.loads(request.content)
        return httpx.Response(200, json={"results": []})

    client = httpx.AsyncClient(base_url="https://api.notion.com/v1", transport=httpx.MockTransport(handler))
    a = NotionAdapter(client=client, token="t")
    a.pattern_db_id = "patterndb"
    rows = await a.pattern_query()
    await client.aclose()
    assert rows == []
    assert "filter" not in captured["body"]  # 필터 없으면 최근순만


async def test_pattern_query_graceful_when_missing():
    assert await NotionAdapter(token="").pattern_query(category="MCP/연동") == []
    a = NotionAdapter(token="t")
    a.pattern_db_id = None
    assert await a.pattern_query(tags=["MCP"]) == []


# ── get_context 통합 ────────────────────────────────────────────
def _ctx_with_notion(tmp_path, notion):
    adapters = {"notion": notion, "memory": MemoryAdapter(base_dir=tmp_path)}
    return ToolContext(adapters, SmartRouter(adapters), SchemaValidator())


def _routed_handler(request):
    p = request.url.path
    if p.endswith("/databases/devlogdb/query"):
        return httpx.Response(200, json={"results": [_devlog_page()]})
    if p.endswith("/databases/patterndb/query"):
        return httpx.Response(200, json={"results": [_pattern_page()]})
    return httpx.Response(200, json={"results": []})


async def test_get_context_includes_devlog_and_patterns(tmp_path):
    client = httpx.AsyncClient(base_url="https://api.notion.com/v1", transport=httpx.MockTransport(_routed_handler))
    notion = NotionAdapter(client=client, token="t")
    notion.db_ids = {}  # 엔티티 검색 비활성(라우터 search 격리)
    notion.devlog_db_id = "devlogdb"
    notion.pattern_db_id = "patterndb"
    ctx = _ctx_with_notion(tmp_path, notion)

    env = await T.tool_get_context(ctx, "거버넌스", {"project": "VHK", "tags": ["MCP"]})
    await client.aclose()

    assert set(env) >= {"data", "verification", "provenance"}
    d = env["data"]
    assert len(d["devlog"]) == 1 and d["devlog"][0]["유형"] == "세션로그"
    assert len(d["patterns"]) == 1 and d["patterns"][0]["패턴명"] == "LLM JSON raw parse 금지"
    assert "notion:devlog" in env["provenance"]["sources_used"]
    assert "notion:pattern" in env["provenance"]["sources_used"]


async def test_get_context_no_project_empty_devlog(tmp_path):
    client = httpx.AsyncClient(base_url="https://api.notion.com/v1", transport=httpx.MockTransport(_routed_handler))
    notion = NotionAdapter(client=client, token="t")
    notion.db_ids = {}
    notion.devlog_db_id = "devlogdb"
    notion.pattern_db_id = "patterndb"
    ctx = _ctx_with_notion(tmp_path, notion)

    env = await T.tool_get_context(ctx, "아무거나", {})  # project/tags 없음
    await client.aclose()

    assert env["data"]["devlog"] == []      # 프로젝트 없으면 빈 배열(에러 아님)
    assert env["data"]["patterns"] == []    # category/tags 없으면 패턴 조회 안 함
    assert "notion:devlog" not in env["provenance"]["sources_used"]


# ── 회수 실패 침묵 방지: 진단 로깅 (devlog/pattern except 경로) ──────────
async def test_get_context_logs_warning_when_devlog_and_pattern_query_fail(tmp_path, caplog):
    """devlog_query/pattern_query 가 예외를 던지면 진단 WARNING 이 남아야 한다.
    제어흐름은 유지 — 회수는 여전히 빈 배열로 graceful 폴백한다."""
    client = httpx.AsyncClient(base_url="https://api.notion.com/v1", transport=httpx.MockTransport(_routed_handler))
    notion = NotionAdapter(client=client, token="t")
    notion.db_ids = {}
    notion.devlog_db_id = "devlogdb"
    notion.pattern_db_id = "patterndb"

    async def _boom_devlog(project):
        raise RuntimeError("devlog 회수 폭발")

    async def _boom_pattern(category=None, tags=None):
        raise RuntimeError("pattern 회수 폭발")

    notion.devlog_query = _boom_devlog
    notion.pattern_query = _boom_pattern

    ctx = _ctx_with_notion(tmp_path, notion)
    with caplog.at_level(logging.WARNING):
        env = await T.tool_get_context(ctx, "거버넌스", {"project": "VHK", "tags": ["MCP"]})
    await client.aclose()

    # 제어흐름 유지 — 실패해도 빈 배열 폴백 (예외 전파/크래시 없음)
    assert env["data"]["devlog"] == []
    assert env["data"]["patterns"] == []
    # 진단 로그 — WARNING 레코드가 남아야 함
    assert any("실패" in r.message or r.levelno == logging.WARNING for r in caplog.records)
    # 두 경로(devlog/pattern) 각각 한 줄씩
    warn_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("devlog" in m.lower() for m in warn_msgs)
    assert any("pattern" in m.lower() for m in warn_msgs)
    # 회수 전멸은 봉투 errors 에도 실려야 함 — '패턴 0건'과 구분 가능
    assert "notion:devlog" in env["errors"]
    assert "notion:pattern" in env["errors"]
