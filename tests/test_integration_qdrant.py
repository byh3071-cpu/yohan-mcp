# -*- coding: utf-8 -*-
"""실 Qdrant/ollama 통합 스모크 (env-gated).

:memory: 가 못 잡는 실서버 경로(실 query_points·차원 실측·실 임베딩)를 최소 검증한다.
기본 실행에서 제외: `pytest -m "not integration"`. 실행하려면 QDRANT_URL 설정 + 서버 기동.

격리: 실 관제탑 컬렉션을 건드리지 않도록 임시 컬렉션 1개만 만들고 그것만 검색·회수한 뒤
teardown 에서 삭제한다(실데이터 오염 0). 임베더 차원은 실측(bge-m3 1024 / hash 384 무관 통과).
"""
import os
import uuid

import pytest

from adapters.memory_adapter import MemoryAdapter
from adapters.qdrant_adapter import QdrantAdapter
from core.router import SmartRouter
from core.schema_validator import SchemaValidator
from core import tools as T
from core.tools import ToolContext

pytestmark = pytest.mark.integration

PAGE_A, PAGE_B = "IT-PAGE-A", "IT-PAGE-B"
N_A, N_B = 6, 3
QUERY = "어텐션 청크 크기 재현율"


def _require_server():
    # 실 서버 URL 은 .env 에 있으므로 명시 로드(pytest 프로세스는 server.py 를 안 거침).
    # conftest 는 integration 마커일 때 QDRANT_URL 을 보존한다.
    from dotenv import load_dotenv

    load_dotenv()
    if not os.getenv("QDRANT_URL"):
        pytest.skip("QDRANT_URL 미설정 — 실 Qdrant 통합 스킵")


async def _seed_isolated_collection(qa: QdrantAdapter, coll: str) -> None:
    from qdrant_client import models

    client = qa._get_client()
    dim = qa.embedder.dim  # 실측 — 하드코딩 금지
    await client.create_collection(
        coll, vectors_config=models.VectorParams(size=dim, distance=models.Distance.COSINE)
    )
    points = []
    for page, n in [(PAGE_A, N_A), (PAGE_B, N_B)]:
        texts = [f"{page} 어텐션 메커니즘 청크 {i} 쿼리 키 값 재현율" for i in range(n)]
        vecs = qa.embedder.embed(texts)
        for i, vec in enumerate(vecs):
            points.append(models.PointStruct(
                id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{coll}/{page}#chunk{i}")),
                vector=vec,
                payload={"notion_page_id": page, "chunk_index": i, "title": page, "text": texts[i]},
            ))
    await client.upsert(coll, points=points)


async def test_integration_get_context_multichunk_on_real_server(tmp_path):
    _require_server()
    coll = f"_it_getctx_{uuid.uuid4().hex[:8]}"  # 격리 임시 컬렉션
    qa = QdrantAdapter(collection=coll)           # 실 url(env) + 실 임베더
    qa.search_collections = [coll]                # 실 관제탑 컬렉션 미검색(오염 방지)
    try:
        await _seed_isolated_collection(qa, coll)

        adapters = {"memory": MemoryAdapter(base_dir=tmp_path), "qdrant": qa}
        ctx = ToolContext(adapters, SmartRouter(adapters), SchemaValidator())
        env = await T.tool_get_context(ctx, QUERY, {"top_k": 20})

        matches = env["data"]["matches"]
        ids = [m["id"] for m in matches]
        assert len(ids) == N_A + N_B == len(set(ids))  # 붕괴 0(청크 고유 point_id)
        page_counts = {}
        for m in matches:
            k = m["data"]["notion_page_id"]
            page_counts[k] = page_counts.get(k, 0) + 1
        assert page_counts[PAGE_A] == N_A and page_counts[PAGE_B] == N_B

        # 실서버 health — 컬렉션 존재 + 포인트 카운트 + 임베더 정보 표면화
        h = await qa.health_check()
        assert h["ok"] is True and "points" in h["detail"] and "embed=" in h["detail"]
    finally:
        try:
            await qa._get_client().delete_collection(coll)  # 임시 컬렉션 정리
        finally:
            await qa.aclose()
