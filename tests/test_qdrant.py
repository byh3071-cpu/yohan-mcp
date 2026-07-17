# -*- coding: utf-8 -*-
"""QdrantAdapter — 임베디드(:memory:) 모드 upsert/search/health/멱등 테스트."""
import logging

import pytest

from adapters.qdrant_adapter import QdrantAdapter
from core.embeddings import HashEmbedder

RESOURCE = {
    "resource_id": "r1",
    "title": "트랜스포머 어텐션 메커니즘 정리",
    "source_url": "https://example.com/attention",
    "resource_type": "아티클",
    "domain": "AI",
    "status": "신규",
    "raw_content": "쿼리 키 값 내적으로 토큰 관련도를 계산한다",
}


def _adapter():
    # url 미지정 → :memory:, hash 임베더로 결정적
    return QdrantAdapter(url=None, embedder=HashEmbedder())


async def test_qdrant_create_search_health():
    qa = _adapter()
    rec = await qa.create("resource", RESOURCE)
    assert rec["backend"] == "qdrant" and rec["type"] == "resource" and rec["id"] == "r1"

    res = await qa.search("어텐션 메커니즘", {"top_k": 5})
    assert res and res[0]["data"]["resource_id"] == "r1"
    assert res[0]["score"] is not None

    h = await qa.health_check()
    assert h["ok"] is True and "points" in h["detail"]
    await qa.aclose()


async def test_qdrant_idempotent_upsert():
    qa = _adapter()
    await qa.create("resource", RESOURCE)
    await qa.create("resource", RESOURCE)  # 동일 url → 동일 point id
    cnt = (await qa._get_client().count(qa.collection)).count
    assert cnt == 1  # 멱등: 중복 적재 안 됨
    await qa.aclose()


async def test_qdrant_health_no_collection():
    qa = _adapter()
    # ensure_collection 호출 전이라도 health 는 예외 없이 ok=False
    h = await qa.health_check()
    assert set(h) == {"ok", "latency_ms", "detail"}
    await qa.aclose()


def test_point_id_deterministic():
    a = QdrantAdapter.point_id("https://example.com/x")
    b = QdrantAdapter.point_id("https://example.com/x")
    c = QdrantAdapter.point_id("https://example.com/y")
    assert a == b and a != c


def test_hash_embedder_dim_and_norm():
    e = HashEmbedder()
    assert e.dim == 384
    v = e.embed(["어텐션 메커니즘"])[0]
    assert len(v) == 384
    norm = sum(x * x for x in v) ** 0.5
    assert abs(norm - 1.0) < 1e-6  # L2 정규화


def test_hash_embedder_empty_is_unit():
    # 토큰 0개(빈/문장부호/공백)도 영벡터 아닌 단위벡터 (실 Qdrant COSINE 거부 방지)
    e = HashEmbedder()
    for t in ["", "!!! ???", "   "]:
        v = e.embed([t])[0]
        assert abs(sum(x * x for x in v) ** 0.5 - 1.0) < 1e-6


async def test_qdrant_payload_schema_compatible():
    from core.schema_validator import SchemaValidator
    qa = _adapter()
    await qa.create("resource", RESOURCE)
    data = (await qa.search("어텐션"))[0]["data"]
    assert "source_url" in data and "resource_url" not in data  # 스키마 필드명 일치
    ok, errs = SchemaValidator().validate_partial("resource", data)
    assert ok, errs  # search 결과가 resource 스키마 부분검증 통과 → schema_valid 오염 없음
    await qa.aclose()


async def test_qdrant_empty_key_rejected():
    qa = _adapter()
    with pytest.raises(ValueError):
        await qa.create("resource", {"title": "url·id 둘 다 없음"})
    await qa.aclose()


async def test_qdrant_search_all_collections_failed_raises():
    """전 컬렉션 회수 실패는 빈 결과가 아니라 예외 — 거짓 성공 봉투 방지.

    컬렉션 생성 전 검색 → query_points 가 전부 컬렉션 미존재로 예외 → 성공 컬렉션 0.
    []를 돌려주면 router 가 qdrant 를 sources_used 에 등재하고 errors 를 비운 채
    ok=True/count=0 을 만들어, 소비자가 '지식 없음'과 '백엔드 붕괴'를 구분할 수 없다.
    (부분 실패는 test_qdrant_search_partial_missing_collections_no_warning 이 담당.)
    """
    qa = _adapter()
    with pytest.raises(RuntimeError, match="회수 전면 실패"):
        await qa.search("어텐션", {"top_k": 5})
    await qa.aclose()


async def test_qdrant_search_all_collections_failed_surfaces_in_router_envelope():
    """어댑터 예외가 router 봉투에서 errors 로 표면화되고 sources_used 에서 빠진다."""
    from core.router import SmartRouter

    qa = _adapter()
    router = SmartRouter({"qdrant": qa})
    res = await router.search("어텐션", {"backends": ["qdrant"], "top_k": 5})
    assert res["results"] == []
    assert "qdrant" not in res["sources_used"]  # 붕괴한 백엔드를 '사용됨'으로 등재하지 않음
    assert "회수 전면 실패" in res["errors"]["qdrant"]
    await qa.aclose()


async def test_qdrant_search_partial_missing_collections_no_warning(caplog):
    """일부 컬렉션만 없을 때(부분 성공) — WARNING 없이 조용히 skip(노이즈 제거).

    실사용 재현(#qdrant-legacy-skip 배경): 레거시 쓰기 컬렉션(yohan_resources 등)이 드롭돼도
    brain_memory 등 다른 컬렉션이 살아있으면 검색 자체는 정상 성공한다. 이때 개별 컬렉션의
    404 는 노이즈일 뿐이므로 WARNING 을 내지 않아야 한다 — 전체가 회수 실패했을 때만 집계
    경고를 내는 test_qdrant_search_missing_collection_logs_warning 과 대비되는 케이스.
    """
    qa = _adapter()
    await qa.create("resource", RESOURCE)  # self.collection(yohan_resources)만 존재하게 시딩
    with caplog.at_level(logging.WARNING):
        res = await qa.search("어텐션", {"top_k": 5})
    assert res and res[0]["data"]["resource_id"] == "r1"  # 존재하는 컬렉션에서 부분 성공 회수
    # 나머지 6개(관제탑4 + brain_memory + ontology_triples)는 이 :memory: 인스턴스에 미존재하지만
    # 전체 실패가 아니므로(위 결과 존재) WARNING 로그가 전혀 없어야 한다(노이즈 제거 확인).
    assert not any(r.levelno == logging.WARNING for r in caplog.records)
    await qa.aclose()


async def test_qdrant_collection_dim_missing_logs_warning(caplog):
    # 존재하지 않는 컬렉션 차원 조회 → get_collection 예외 → _collection_dim except(return None) 경로.
    qa = _adapter()
    client = qa._get_client()
    with caplog.at_level(logging.WARNING):
        dim = await qa._collection_dim(client, "definitely_missing_collection")
    assert dim is None  # graceful 폴백(return None) 유지
    assert any("실패" in r.message or r.levelno == logging.WARNING for r in caplog.records)
    await qa.aclose()


async def test_qdrant_aclose_failure_logs_warning(caplog):
    # client.close() 가 예외를 던져도 aclose 는 삼키고 정리(제어흐름 유지) — monkeypatch 로 트리거.
    qa = _adapter()
    qa._get_client()

    class _BoomClient:
        async def close(self):
            raise RuntimeError("close 폭발")

    qa._client = _BoomClient()
    qa._owns_client = True
    with caplog.at_level(logging.WARNING):
        await qa.aclose()
    assert qa._client is None  # 예외 삼키고 client 정리(graceful) 유지
    assert any("실패" in r.message or r.levelno == logging.WARNING for r in caplog.records)


async def test_qdrant_multichunk_same_page_not_collapsed():
    # 같은 notion_page_id 를 공유하는 서로 다른 청크(고유 point_id)들이 하나로 붕괴되지 않고
    # 전부 별개 레코드로 회수되는지 + 어댑터 단계에서 top_k 로 선절단하지 않는지(융합 후 절단) 검증.
    import uuid as _uuid

    from qdrant_client import models

    e = HashEmbedder()
    qa = QdrantAdapter(url=None, embedder=e, collection="yohan_resources")
    client = qa._get_client()
    await client.create_collection(
        "yohan_resources",
        vectors_config=models.VectorParams(size=e.dim, distance=models.Distance.COSINE),
    )
    # 한 페이지의 6개 청크(같은 notion_page_id, 서로 다른 본문/청크인덱스/고유 point_id)
    n_chunks = 6
    vecs = e.embed([f"어텐션 메커니즘 청크 {i} 쿼리 키 값" for i in range(n_chunks)])
    points = [
        models.PointStruct(
            id=str(_uuid.uuid4()),
            vector=vecs[i],
            payload={"notion_page_id": "PAGE-A", "chunk_index": i, "title": "트랜스포머"},
        )
        for i in range(n_chunks)
    ]
    await client.upsert("yohan_resources", points=points)

    # top_k=3 이지만 어댑터는 후보를 top_k 로 선절단하지 않는다(fetch_k 로만 상한).
    res = await qa.search("어텐션 메커니즘", {"top_k": 3})
    ids = [r["id"] for r in res]
    # 붕괴 0: 6개 청크 모두 별개 레코드(고유 point_id id)로 회수
    assert len(ids) == n_chunks, f"청크 붕괴/선절단 발생: {len(ids)} != {n_chunks}"
    assert len(set(ids)) == n_chunks  # id 가 청크 고유(point_id) — notion_page_id 로 접히지 않음
    # 페이지 메타(notion_page_id)는 data(payload)에 보존 — provenance/역참조용
    assert all(r["data"]["notion_page_id"] == "PAGE-A" for r in res)
    assert {r["data"]["chunk_index"] for r in res} == set(range(n_chunks))
    await qa.aclose()


async def test_qdrant_dim_mismatch_detected():
    from qdrant_client import AsyncQdrantClient

    class Tiny:
        name, dim = "tiny", 8

        def embed(self, texts):
            return [[0.1] * 8 for _ in texts]

    c = AsyncQdrantClient(location=":memory:")
    a1 = QdrantAdapter(client=c, embedder=HashEmbedder())  # dim 384 컬렉션 생성
    await a1.create("resource", RESOURCE)
    a2 = QdrantAdapter(client=c, embedder=Tiny())  # dim 8 ≠ 384
    with pytest.raises(RuntimeError):
        await a2.ensure_collection()
    await c.close()


async def test_qdrant_search_empty_query_on_live_collection():
    # 빈 query("")·None 도 실컬렉션에서 크래시 없이 결과를 반환한다.
    # (query or "" → HashEmbedder 단위벡터 → COSINE 검색 성립. 영벡터 거부 회피.)
    # 기존 missing-collection 테스트(query="어텐션")와 상보 — 여기선 컬렉션이 존재.
    qa = _adapter()
    await qa.create("resource", RESOURCE)  # yohan_resources 생성 + 1점 적재
    for q in ["", None]:
        res = await qa.search(q, {"top_k": 5})
        assert isinstance(res, list) and len(res) >= 1  # 예외 없이 후보 반환
        assert res[0]["data"]["resource_id"] == "r1"
    await qa.aclose()


async def test_qdrant_health_no_collection_says_seeding_needed():
    # 컬렉션 미존재 → health 는 ok=False + detail 에 "시딩 필요" 안내(진단 표면화).
    qa = _adapter()
    h = await qa.health_check()
    assert h["ok"] is False
    assert "시딩 필요" in h["detail"]
    await qa.aclose()


async def test_qdrant_health_dim_mismatch_branch():
    # health_check 의 차원불일치 분기(집계된 컬렉션 차원 ≠ 임베더 차원) → ok=False + "차원 불일치".
    # ensure_collection RuntimeError 와 별개 경로(읽기측 진단).
    from qdrant_client import AsyncQdrantClient

    class Tiny:
        name, dim = "tiny", 8

        def embed(self, texts):
            return [[0.1] * 8 for _ in texts]

    c = AsyncQdrantClient(location=":memory:")
    a1 = QdrantAdapter(client=c, embedder=HashEmbedder())  # dim 384 컬렉션 + 1점
    await a1.create("resource", RESOURCE)
    a2 = QdrantAdapter(client=c, embedder=Tiny())  # dim 8 임베더로 같은 컬렉션 헬스 조회
    h = await a2.health_check()
    assert h["ok"] is False
    assert "차원 불일치" in h["detail"]
    await c.close()


# ── search_collections 기본 목록 (MINOR I 풋건 수정) ─────────────────────
def test_search_collections_default_includes_control_tower_and_brain():
    from adapters.qdrant_adapter import (
        BRAIN_MEMORY_COLLECTION,
        CONTROL_TOWER_COLLECTIONS,
        ONTOLOGY_TRIPLES_COLLECTION,
    )
    qa = QdrantAdapter(url=None, embedder=HashEmbedder())
    for c in CONTROL_TOWER_COLLECTIONS:
        assert c in qa.search_collections
    assert BRAIN_MEMORY_COLLECTION in qa.search_collections
    assert ONTOLOGY_TRIPLES_COLLECTION in qa.search_collections
    assert qa.search_collections[0] == qa.collection  # 쓰기 컬렉션이 최우선


def test_search_collections_env_appends_not_replaces(monkeypatch):
    # 과거 풋건: QDRANT_SEARCH_COLLECTIONS 를 하나라도 설정하면 기본 컬렉션이 통째로
    # 사라져 get_context 회수가 조용히 줄었다(MINOR I). env 는 대체가 아니라 추가여야 한다.
    from adapters.qdrant_adapter import BRAIN_MEMORY_COLLECTION, CONTROL_TOWER_COLLECTIONS

    monkeypatch.setenv("QDRANT_SEARCH_COLLECTIONS", "extra_demo_collection")
    qa = QdrantAdapter(url=None, embedder=HashEmbedder())
    assert "extra_demo_collection" in qa.search_collections
    for c in CONTROL_TOWER_COLLECTIONS:
        assert c in qa.search_collections
    assert BRAIN_MEMORY_COLLECTION in qa.search_collections


def test_search_collections_dedup_no_duplicates():
    qa = QdrantAdapter(url=None, embedder=HashEmbedder(), collection="knowledge_base")
    # collection 이 우연히 관제탑 컬렉션명과 겹쳐도 중복 없이 1회만 들어간다.
    assert qa.search_collections.count("knowledge_base") == 1
