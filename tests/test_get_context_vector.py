# -*- coding: utf-8 -*-
"""get_context 벡터 멀티청크 회수 — router→get_context 전체 경로 통합테스트.

#20(청크 고유 point_id 식별) + #13(RRF 동일청크 중복가산 방지)의 동시 회귀.
어댑터 단독(test_qdrant.py) 이나 router 단독(test_router.py) 이 아니라, 실제 도구 진입점
tool_get_context 가 router 를 경유해 같은 페이지의 여러 청크를 붕괴 없이 회수하는지 검증한다.
:memory: Qdrant + HashEmbedder(결정적, dim 384) 로 인라인 시드 → 외부 의존 0.
"""
import uuid as _uuid

import httpx
from qdrant_client import models

from adapters.memory_adapter import MemoryAdapter
from adapters.notion_adapter import NotionAdapter
from adapters.qdrant_adapter import QdrantAdapter
from core.embeddings import HashEmbedder
from core.router import SmartRouter
from core.schema_validator import SchemaValidator
from core import tools as T
from core.tools import ToolContext

PAGE_A = "PAGE-A-ATTENTION"   # 같은 페이지 6청크
PAGE_B = "PAGE-B-RRF"         # 다른 페이지 3청크
N_A, N_B = 6, 3
QUERY = "어텐션 청크 크기 재현율"


async def _seed_qdrant(collection: str = "knowledge_base") -> QdrantAdapter:
    """관제탑 컬렉션(knowledge_base)에 같은 notion_page_id 다중 청크를 고유 point_id 로 시드."""
    e = HashEmbedder()
    qa = QdrantAdapter(url=None, embedder=e)  # :memory:
    client = qa._get_client()
    await client.create_collection(
        collection, vectors_config=models.VectorParams(size=e.dim, distance=models.Distance.COSINE)
    )
    points = []
    for page, n in [(PAGE_A, N_A), (PAGE_B, N_B)]:
        texts = [f"{page} 어텐션 메커니즘 청크 {i} 쿼리 키 값 재현율" for i in range(n)]
        vecs = e.embed(texts)
        for i, vec in enumerate(vecs):
            # 청크 고유 point_id — 페이지 공유키(notion_page_id)로 접히지 않는 게 핵심(#20)
            pid = str(_uuid.uuid5(_uuid.NAMESPACE_URL, f"{page}#chunk{i}"))
            points.append(models.PointStruct(
                id=pid, vector=vec,
                payload={"notion_page_id": page, "chunk_index": i, "title": page, "text": texts[i]},
            ))
    await client.upsert(collection, points=points)
    return qa


async def test_get_context_vector_multichunk_not_collapsed(tmp_path):
    # router→get_context 전체 경로에서 같은 페이지 6청크가 붕괴되지 않고 전부 회수되는지.
    qa = await _seed_qdrant()
    adapters = {"memory": MemoryAdapter(base_dir=tmp_path), "qdrant": qa}
    ctx = ToolContext(adapters, SmartRouter(adapters), SchemaValidator())

    # per_page_cap:0 — 이 테스트의 보장 대상은 회수(붕괴 0, 청크 전량)라 표출 다양화(#21)를 끈다
    env = await T.tool_get_context(ctx, QUERY, {"top_k": 20, "per_page_cap": 0})

    # 표준봉투 형태
    assert set(env) >= {"data", "verification", "provenance"}
    matches = env["data"]["matches"]
    ids = [m["id"] for m in matches]
    # 붕괴 0: 9청크 전부 별개(point_id 고유) 회수
    assert len(ids) == N_A + N_B == len(set(ids))
    # 페이지 메타(notion_page_id)는 data(payload)에 보존 — provenance/역참조용
    page_counts = {}
    for m in matches:
        page_counts[m["data"]["notion_page_id"]] = page_counts.get(m["data"]["notion_page_id"], 0) + 1
    assert page_counts[PAGE_A] == N_A  # 같은 페이지 6청크 모두 보존(접힘 없음)
    assert page_counts[PAGE_B] == N_B
    # 관제탑 컬렉션 → type = 컬렉션명(스키마 없는 벡터청크)
    assert all(m["type"] == "knowledge_base" for m in matches)
    assert "qdrant" in env["provenance"]["sources_used"]
    await qa.aclose()


async def test_get_context_default_per_page_cap_injected(tmp_path):
    # tool_get_context 는 opts 미지정 시 per_page_cap 기본(3)을 주입한다(#21 배선 핀).
    # 같은 페이지 6청크 → 표출 3개로 cap, 타 페이지 3청크는 그대로. 배선이 끊기면 6이 돼 실패.
    qa = await _seed_qdrant()
    adapters = {"memory": MemoryAdapter(base_dir=tmp_path), "qdrant": qa}
    ctx = ToolContext(adapters, SmartRouter(adapters), SchemaValidator())

    env = await T.tool_get_context(ctx, QUERY, {"top_k": 20})

    page_counts = {}
    for m in env["data"]["matches"]:
        page_counts[m["data"]["notion_page_id"]] = page_counts.get(m["data"]["notion_page_id"], 0) + 1
    assert page_counts[PAGE_A] == 3  # 6청크 → 기본 cap 3 (독식 방지)
    assert page_counts[PAGE_B] == N_B  # cap 이하 페이지는 영향 없음
    await qa.aclose()


async def test_get_context_top_k_zero_still_runs_reverse_recall(tmp_path):
    # top_k=0 이면 벡터 matches 는 절단되어 0 이지만, 역방향 회수(devlog/patterns)는 top_k 와
    # 무관한 별도 경로라 여전히 채워져야 한다. notion mock 을 붙여 '실제로 회수됐는지'를 검증한다
    # (키 존재만 보면 tool_get_context 가 devlog/patterns 를 항상 []로 초기화해 동어반복이 됨).
    qa = await _seed_qdrant()
    client = httpx.AsyncClient(base_url="https://api.notion.com/v1", transport=httpx.MockTransport(_routed_handler))
    notion = NotionAdapter(client=client, token="t")
    notion.db_ids = {}
    notion.devlog_db_id = "devlogdb"
    notion.pattern_db_id = "patterndb"
    adapters = {"notion": notion, "memory": MemoryAdapter(base_dir=tmp_path), "qdrant": qa}
    ctx = ToolContext(adapters, SmartRouter(adapters), SchemaValidator())

    env = await T.tool_get_context(ctx, QUERY, {"top_k": 0, "project": "yohan-mcp", "tags": ["MCP"]})
    await client.aclose()

    assert env["data"]["matches"] == []       # 벡터 매치는 융합후 top_k=0 절단
    assert env["data"]["count"] == 0
    # 역방향 회수는 top_k 절단과 독립 — 실제로 1건씩 채워졌는지 검증(동어반복 아님)
    assert len(env["data"]["devlog"]) == 1
    assert len(env["data"]["patterns"]) == 1
    await qa.aclose()


# ── 벡터 matches + 역방향 회수(devlog/patterns) 병존 ─────────────────
def _devlog_page() -> dict:
    return {
        "id": "pg_dl_1",
        "properties": {
            "이름": {"type": "title", "title": [{"plain_text": "(2026-06-11) 어텐션 청크 실험"}]},
            "유형": {"type": "select", "select": {"name": "세션로그"}},
            "교훈": {"type": "rich_text", "rich_text": [{"plain_text": "청크 붕괴는 회수 재현율을 죽인다"}]},
            "관련 파일": {"type": "rich_text", "rich_text": [{"plain_text": "adapters/qdrant_adapter.py"}]},
            "SoT Key": {"type": "rich_text", "rich_text": [{"plain_text": "vhk/2026-06-11/chunk"}]},
            "프로젝트": {"type": "select", "select": {"name": "yohan-mcp"}},
        },
    }


def _pattern_page() -> dict:
    return {
        "id": "pg_pat_1",
        "properties": {
            "패턴명": {"type": "title", "title": [{"plain_text": "청크 식별은 point_id 로"}]},
            "증상": {"type": "rich_text", "rich_text": [{"plain_text": "같은 페이지 청크가 1개로 접힘"}]},
            "원인": {"type": "rich_text", "rich_text": [{"plain_text": "record id 를 공유키로 발급"}]},
            "해결": {"type": "rich_text", "rich_text": [{"plain_text": "청크 고유 point_id 로 식별"}]},
            "적용 조건": {"type": "rich_text", "rich_text": [{"plain_text": "멀티청크 벡터 회수 시"}]},
            "카테고리": {"type": "select", "select": {"name": "MCP/연동"}},
        },
    }


def _routed_handler(request):
    p = request.url.path
    if p.endswith("/databases/devlogdb/query"):
        return httpx.Response(200, json={"results": [_devlog_page()]})
    if p.endswith("/databases/patterndb/query"):
        return httpx.Response(200, json={"results": [_pattern_page()]})
    return httpx.Response(200, json={"results": []})  # 엔티티 검색은 빈 결과


async def test_get_context_vector_and_reverse_recall_coexist(tmp_path):
    # 한 봉투에서 벡터 matches(qdrant) + devlog + patterns(notion 역회수)가 함께 채워지는지.
    qa = await _seed_qdrant()
    client = httpx.AsyncClient(base_url="https://api.notion.com/v1", transport=httpx.MockTransport(_routed_handler))
    notion = NotionAdapter(client=client, token="t")
    notion.db_ids = {}  # 엔티티 검색 비활성(라우터 search 격리 — 벡터/역회수만 관찰)
    notion.devlog_db_id = "devlogdb"
    notion.pattern_db_id = "patterndb"
    adapters = {"notion": notion, "memory": MemoryAdapter(base_dir=tmp_path), "qdrant": qa}
    ctx = ToolContext(adapters, SmartRouter(adapters), SchemaValidator())

    env = await T.tool_get_context(
        ctx, QUERY, {"top_k": 20, "per_page_cap": 0, "project": "yohan-mcp", "tags": ["MCP"]})
    await client.aclose()

    d = env["data"]
    # 벡터 회수 보존
    assert len([m for m in d["matches"] if m["type"] == "knowledge_base"]) == N_A + N_B
    # 역방향 회수 병존
    assert len(d["devlog"]) == 1 and d["devlog"][0]["유형"] == "세션로그"
    assert len(d["patterns"]) == 1 and d["patterns"][0]["패턴명"] == "청크 식별은 point_id 로"
    src = env["provenance"]["sources_used"]
    assert "qdrant" in src and "notion:devlog" in src and "notion:pattern" in src
    await qa.aclose()
