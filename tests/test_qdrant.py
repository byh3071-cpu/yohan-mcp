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


async def test_qdrant_search_missing_collection_logs_warning(caplog):
    # 컬렉션 생성 전 검색 → query_points 가 컬렉션 미존재로 예외 → search() except(continue) 경로.
    qa = _adapter()
    with caplog.at_level(logging.WARNING):
        res = await qa.search("어텐션", {"top_k": 5})
    assert res == []  # graceful 폴백(continue) 유지 — 빈 결과
    assert any("실패" in r.message or r.levelno == logging.WARNING for r in caplog.records)
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
