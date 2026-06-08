# -*- coding: utf-8 -*-
"""MCP Resources/Prompts 노출 확인 (P3).

server 모듈은 import 시 ToolContext.from_env() 로 실어댑터를 만든다 →
memory 쓰기 프로브가 리포를 더럽히지 않게 MEMORY_DIR 를 임시폴더로 지정 후 import.
"""
import os
import tempfile

os.environ.setdefault("MEMORY_DIR", tempfile.mkdtemp(prefix="yohan_mcp_test_"))

import json  # noqa: E402

import pytest  # noqa: E402

import server as S  # noqa: E402


async def _read(uri: str) -> str:
    contents = list(await S.mcp.read_resource(uri))
    return contents[0].content


async def test_resources_listed():
    uris = {str(r.uri) for r in await S.mcp.list_resources()}
    assert "resource://schemas/_links" in uris
    assert "resource://status/current" in uris
    templates = {t.uriTemplate for t in await S.mcp.list_resource_templates()}
    assert "resource://schemas/{backend}/{entity}" in templates


async def test_schema_resource_served_with_examples():
    text = await _read("resource://schemas/notion/summary")
    doc = json.loads(text)
    assert doc["title"].startswith("SUMMARY")
    # P1 description/examples 가 그대로 노출됨(에이전트 few-shot 원천)
    assert "examples" in doc["properties"]["summary_id"]


async def test_links_resource_served():
    text = await _read("resource://schemas/_links")
    doc = json.loads(text)
    rels = {l["relation"] for l in doc["links"]}
    assert "published_as" in rels and "summarized_by" in rels


async def test_status_resource_live():
    text = await _read("resource://status/current")
    doc = json.loads(text)
    assert len(doc["summaries"]) == 5
    assert set(doc["details"]) >= {"notion", "memory", "qdrant", "studio", "n8n"}


async def test_unknown_schema_resource_errors():
    with pytest.raises(Exception):
        await _read("resource://schemas/notion/does-not-exist")


def test_schema_resource_blocks_path_traversal():
    # backend/entity 세그먼트에 경로 탈출 시도 → 차단(임의 파일 읽기 방지)
    for backend, entity in [("..", "x"), ("notion", "../_shared-enums"), ("notion", "..\\.env"), ("notion", "a/b")]:
        with pytest.raises(ValueError):
            S.schema_resource(backend, entity)


async def test_prompts_listed_and_rendered():
    names = {p.name for p in await S.mcp.list_prompts()}
    assert {"create-summary", "run-ingest", "cross-search"} <= names

    gp = await S.mcp.get_prompt("create-summary", {"resource_title": "어텐션", "resource_text": "본문"})
    text = gp.messages[0].content.text
    assert "summary" in text.lower()
    assert "어텐션" in text  # 인자 주입됨
    assert "key_insights" in text  # 스키마 가이드 포함

    gp2 = await S.mcp.get_prompt("run-ingest", {"url": "https://example.com/x"})
    assert "ingest" in gp2.messages[0].content.text
    assert "https://example.com/x" in gp2.messages[0].content.text
