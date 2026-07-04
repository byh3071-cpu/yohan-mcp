# -*- coding: utf-8 -*-
"""scripts/seed_brain_memory.py — 하드페일 가드(BLOCKER D) + 멱등 + payload 형태 검증.

실 ollama/Qdrant 없이 :memory: Qdrant + 주입 HashEmbedder 로 빠르게 검증한다(유닛 격리).
실제 brain memory/ 전수 시딩 + 골든 쿼리는 이 스위트 밖(수동 검증 단계)에서 확인한다.
"""
import pytest

from adapters.qdrant_adapter import QdrantAdapter
from core.embeddings import HashEmbedder
from scripts.seed_brain_memory import HardFailEmbedderError, seed_memory, seed_triples


def _seed_files(base):
    (base / "decisions").mkdir(parents=True, exist_ok=True)
    (base / "wiki").mkdir(parents=True, exist_ok=True)
    (base / "decisions" / "d1.md").write_text(
        "---\nid: dec-1\ntitle: 결정 하나\n---\n\n어텐션 메커니즘 결정 노트 본문입니다.",
        encoding="utf-8",
    )
    (base / "wiki" / "w1.md").write_text("프론트매터 없는 위키 노트 본문.", encoding="utf-8")


# ── 하드페일 가드(BLOCKER D) ─────────────────────────────────────
async def test_seed_memory_hardfail_on_non_ollama_embedder(tmp_path):
    _seed_files(tmp_path)
    qa = QdrantAdapter(url=None, embedder=HashEmbedder(), collection="test_brain_memory_guard")
    with pytest.raises(HardFailEmbedderError):
        await seed_memory(qdrant=qa, base_dir=tmp_path, allow_fallback=False)
    await qa.aclose()


async def test_seed_memory_allow_fallback_bypasses_guard(tmp_path):
    _seed_files(tmp_path)
    qa = QdrantAdapter(url=None, embedder=HashEmbedder(), collection="test_brain_memory_bypass")
    stats = await seed_memory(qdrant=qa, base_dir=tmp_path, allow_fallback=True)
    assert stats["chunks"] >= 2  # 파일 2개 각각 최소 1청크
    assert stats["embedder"] == "hash"
    await qa.aclose()


async def test_seed_triples_hardfail_on_non_ollama_embedder(tmp_path):
    tm = tmp_path / "triple-map.md"
    tm.write_text(
        "| Subject | Relation | Object | 도메인 | 신뢰도 | 출처 | 등록일 |\n"
        "|---|---|---|---|---|---|---|\n"
        "| A | is_a | B | 개발 | 3 | src | 2026-07-04 |\n",
        encoding="utf-8",
    )
    qa = QdrantAdapter(url=None, embedder=HashEmbedder(), collection="test_ontology_guard")
    with pytest.raises(HardFailEmbedderError):
        await seed_triples(qdrant=qa, triple_map_path=tm, allow_fallback=False)
    await qa.aclose()


# ── 멱등 ─────────────────────────────────────────────────────────
async def test_seed_memory_idempotent_point_count(tmp_path):
    _seed_files(tmp_path)
    qa = QdrantAdapter(url=None, embedder=HashEmbedder(), collection="test_brain_memory_idem")
    await seed_memory(qdrant=qa, base_dir=tmp_path, allow_fallback=True)
    cnt1 = (await qa._get_client().count(qa.collection)).count
    await seed_memory(qdrant=qa, base_dir=tmp_path, allow_fallback=True)
    cnt2 = (await qa._get_client().count(qa.collection)).count
    assert cnt1 == cnt2 and cnt1 >= 2
    await qa.aclose()


async def test_seed_triples_allow_fallback_and_idempotent(tmp_path):
    tm = tmp_path / "triple-map.md"
    tm.write_text(
        "| 코드 | 의미 | | 코드 | 의미 |\n"
        "|------|------|-|------|------|\n"
        "| `is_a` | ~의 하위 개념 | | `embodies` | ~의 기관 역할을 한다 |\n\n"
        "| Subject | Relation | Object | 도메인 | 신뢰도 | 출처 | 등록일 |\n"
        "|---|---|---|---|---|---|---|\n"
        "| yohan-voice | embodies | 입 | AI/자동화 | 4 | src | 2026-07-04 |\n"
        "| 요한 OS | is_a | 팔란티어의 축소판 | AI/자동화 | 3 | src | 2026-06-12 |\n",
        encoding="utf-8",
    )
    qa = QdrantAdapter(url=None, embedder=HashEmbedder(), collection="test_ontology_bypass")
    stats = await seed_triples(qdrant=qa, triple_map_path=tm, allow_fallback=True)
    assert stats["triples"] == 2
    cnt1 = (await qa._get_client().count(qa.collection)).count
    await seed_triples(qdrant=qa, triple_map_path=tm, allow_fallback=True)
    cnt2 = (await qa._get_client().count(qa.collection)).count
    assert cnt1 == cnt2 == 2
    await qa.aclose()


async def test_seed_triples_missing_file_returns_zero(tmp_path):
    qa = QdrantAdapter(url=None, embedder=HashEmbedder(), collection="test_ontology_missing")
    stats = await seed_triples(qdrant=qa, triple_map_path=tmp_path / "nope.md", allow_fallback=True)
    assert stats["triples"] == 0
    await qa.aclose()


# ── payload 형태 + 옵션 ──────────────────────────────────────────
async def test_seed_memory_payload_shape(tmp_path):
    _seed_files(tmp_path)
    qa = QdrantAdapter(url=None, embedder=HashEmbedder(), collection="test_brain_memory_payload")
    await seed_memory(qdrant=qa, base_dir=tmp_path, allow_fallback=True)
    client = qa._get_client()
    points, _ = await client.scroll(qa.collection, limit=100, with_payload=True)
    payloads = [p.payload for p in points]
    assert any(p["type"] == "brain:decisions" and p["title"] == "결정 하나" for p in payloads)
    assert any(p["type"] == "brain:wiki" for p in payloads)
    for p in payloads:
        assert set(p) == {"path", "title", "chunk_index", "chunk_start_line", "type", "body"}
        assert p["chunk_start_line"] >= 1
    await qa.aclose()


async def test_seed_memory_limit_option(tmp_path):
    _seed_files(tmp_path)
    (tmp_path / "wiki" / "w2.md").write_text("두 번째 위키 노트 본문 내용.", encoding="utf-8")
    qa = QdrantAdapter(url=None, embedder=HashEmbedder(), collection="test_brain_memory_limit")
    stats = await seed_memory(qdrant=qa, base_dir=tmp_path, allow_fallback=True, limit=1)
    assert stats["files"] == 1
    await qa.aclose()
