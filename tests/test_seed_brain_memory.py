# -*- coding: utf-8 -*-
"""scripts/seed_brain_memory.py — 하드페일 가드(BLOCKER D) + 멱등 + payload 형태 + U8 증분 검증.

실 ollama/Qdrant 없이 :memory: Qdrant + 주입 HashEmbedder 로 빠르게 검증한다(유닛 격리).
실제 brain memory/ 전수 시딩 + 골든 쿼리는 이 스위트 밖(수동 검증 단계)에서 확인한다.
"""
import pytest

from adapters.qdrant_adapter import QdrantAdapter
from core.embeddings import HashEmbedder
from scripts.seed_brain_memory import HardFailEmbedderError, seed_memory, seed_triples


class CountingEmbedder(HashEmbedder):
    """U8 검증용 — 문서 임베딩 호출·텍스트 수를 계수(무변경 재실행 = 0 호출 단정)."""

    def __init__(self):
        super().__init__()
        self.calls = 0
        self.texts = 0

    def embed(self, texts):
        texts = list(texts)
        self.calls += 1
        self.texts += len(texts)
        return super().embed(texts)


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


# ── U8 — 증분 재시딩(매니페스트 hash 스킵) ───────────────────────
def _incremental_env(tmp_path, collection):
    base = tmp_path / "brain"
    base.mkdir()
    _seed_files(base)
    manifest = tmp_path / "cache" / "brain_seed_manifest.json"
    qa = QdrantAdapter(url=None, embedder=CountingEmbedder(), collection=collection)
    return base, manifest, qa


async def test_seed_memory_incremental_skips_unchanged(tmp_path):
    base, manifest, qa = _incremental_env(tmp_path, "test_u8_unchanged")
    s1 = await seed_memory(qdrant=qa, base_dir=base, allow_fallback=True, manifest_path=manifest)
    assert s1["new_files"] == 2 and s1["chunks"] >= 2
    assert manifest.exists()
    calls_after_first = qa.embedder.calls
    cnt1 = (await qa._get_client().count(qa.collection)).count

    # 무변경 재실행 — 문서 임베딩 호출 0, 전 파일 무변경 스킵, 포인트 수 불변.
    s2 = await seed_memory(qdrant=qa, base_dir=base, allow_fallback=True, manifest_path=manifest)
    assert qa.embedder.calls == calls_after_first  # 임베딩 호출 0(U8 핵심 단정)
    assert s2["unchanged_files"] == 2 and s2["files"] == 0 and s2["chunks"] == 0
    cnt2 = (await qa._get_client().count(qa.collection)).count
    assert cnt1 == cnt2
    await qa.aclose()


async def test_seed_memory_incremental_reembeds_only_changed(tmp_path):
    base, manifest, qa = _incremental_env(tmp_path, "test_u8_changed")
    await seed_memory(qdrant=qa, base_dir=base, allow_fallback=True, manifest_path=manifest)
    texts_before = qa.embedder.texts

    # 파일 1개 수정 → 그 파일 청크만 재임베딩(1청크), 나머지는 hash 스킵.
    (base / "wiki" / "w1.md").write_text("수정된 위키 노트 본문(내용 변경).", encoding="utf-8")
    s2 = await seed_memory(qdrant=qa, base_dir=base, allow_fallback=True, manifest_path=manifest)
    assert s2["reembedded_files"] == 1 and s2["unchanged_files"] == 1 and s2["new_files"] == 0
    assert s2["chunks"] == 1  # 수정 파일(1청크)만 갱신
    assert qa.embedder.texts - texts_before == 1  # 임베딩된 텍스트도 그 1청크뿐
    await qa.aclose()


async def test_seed_memory_incremental_new_file_added(tmp_path):
    base, manifest, qa = _incremental_env(tmp_path, "test_u8_new")
    await seed_memory(qdrant=qa, base_dir=base, allow_fallback=True, manifest_path=manifest)
    cnt1 = (await qa._get_client().count(qa.collection)).count

    (base / "wiki" / "w-new.md").write_text("신규 추가된 위키 노트.", encoding="utf-8")
    s2 = await seed_memory(qdrant=qa, base_dir=base, allow_fallback=True, manifest_path=manifest)
    assert s2["new_files"] == 1 and s2["unchanged_files"] == 2
    cnt2 = (await qa._get_client().count(qa.collection)).count
    assert cnt2 == cnt1 + 1  # 신규 파일 청크만 증가
    await qa.aclose()


async def test_seed_memory_incremental_reports_deleted_without_deleting(tmp_path):
    base, manifest, qa = _incremental_env(tmp_path, "test_u8_deleted")
    await seed_memory(qdrant=qa, base_dir=base, allow_fallback=True, manifest_path=manifest)
    cnt1 = (await qa._get_client().count(qa.collection)).count

    (base / "wiki" / "w1.md").unlink()  # 파일 삭제
    s2 = await seed_memory(qdrant=qa, base_dir=base, allow_fallback=True, manifest_path=manifest)
    assert s2["deleted"] == ["wiki/w1.md"]  # 목록 보고
    cnt2 = (await qa._get_client().count(qa.collection)).count
    assert cnt1 == cnt2  # 자동 삭제 금지 — 포인트 수 불변(삭제는 사람)
    await qa.aclose()


async def test_seed_memory_full_flag_reembeds_all(tmp_path):
    base, manifest, qa = _incremental_env(tmp_path, "test_u8_full")
    s1 = await seed_memory(qdrant=qa, base_dir=base, allow_fallback=True, manifest_path=manifest)
    texts_first = qa.embedder.texts

    s2 = await seed_memory(qdrant=qa, base_dir=base, allow_fallback=True, manifest_path=manifest, full=True)
    assert s2["unchanged_files"] == 0 and s2["reembedded_files"] == 2
    assert s2["chunks"] == s1["chunks"]  # 전량 강제 재임베딩
    assert qa.embedder.texts == texts_first * 2
    await qa.aclose()


async def test_seed_memory_manifest_env_mismatch_treated_as_full(tmp_path):
    # 같은 매니페스트라도 컬렉션이 다르면 hash 를 신뢰하지 않는다(그 컬렉션에 벡터가 없으므로).
    base, manifest, qa1 = _incremental_env(tmp_path, "test_u8_mismatch_a")
    await seed_memory(qdrant=qa1, base_dir=base, allow_fallback=True, manifest_path=manifest)
    await qa1.aclose()

    qa2 = QdrantAdapter(url=None, embedder=CountingEmbedder(), collection="test_u8_mismatch_b")
    s2 = await seed_memory(qdrant=qa2, base_dir=base, allow_fallback=True, manifest_path=manifest)
    assert s2["unchanged_files"] == 0 and s2["new_files"] == 2  # 매니페스트 무효 → 전량
    await qa2.aclose()


async def test_seed_memory_rebuild_resets_manifest(tmp_path):
    base, manifest, qa = _incremental_env(tmp_path, "test_u8_rebuild")
    await seed_memory(qdrant=qa, base_dir=base, allow_fallback=True, manifest_path=manifest)

    # --rebuild: 컬렉션 삭제 → 매니페스트도 리셋 → 전량 재임베딩(스킵이 남으면 빈 컬렉션이 됨).
    s2 = await seed_memory(qdrant=qa, base_dir=base, allow_fallback=True, manifest_path=manifest, rebuild=True)
    assert s2["unchanged_files"] == 0 and s2["new_files"] == 2
    cnt = (await qa._get_client().count(qa.collection)).count
    assert cnt == s2["chunks"] >= 2  # 재생성된 컬렉션에 전 청크 존재
    await qa.aclose()


async def test_seed_memory_shrunk_file_reports_stale_chunks(tmp_path):
    # 청크 수가 줄어든 파일 — 꼬리 청크 포인트가 옛 내용으로 잔존함을 보고(자동 삭제 금지).
    base, manifest, qa = _incremental_env(tmp_path, "test_u8_stale")
    long_md = "\n\n".join("가" * 400 for _ in range(3))  # CJK 1글자≈1토큰 → 512 한도로 다청크
    (base / "wiki" / "w-long.md").write_text(long_md, encoding="utf-8")
    s1 = await seed_memory(qdrant=qa, base_dir=base, allow_fallback=True, manifest_path=manifest)
    assert s1["chunks"] > 3  # w-long 이 실제로 2+청크로 쪼개짐(전제 확인)
    cnt1 = (await qa._get_client().count(qa.collection)).count

    (base / "wiki" / "w-long.md").write_text("한 청크로 줄어든 본문.", encoding="utf-8")
    s2 = await seed_memory(qdrant=qa, base_dir=base, allow_fallback=True, manifest_path=manifest)
    assert len(s2["stale_chunks"]) == 1
    rep = s2["stale_chunks"][0]
    assert rep["path"] == "wiki/w-long.md" and rep["new_chunks"] == 1 and rep["old_chunks"] > 1
    cnt2 = (await qa._get_client().count(qa.collection)).count
    assert cnt1 == cnt2  # 잔존 포인트 자동 삭제 안 함
    await qa.aclose()


async def test_seed_memory_no_manifest_keeps_legacy_full_behavior(tmp_path):
    # manifest_path=None(라이브러리 기본) — 증분 비활성, 기존 전량 동작 그대로.
    base = tmp_path / "brain"
    base.mkdir()
    _seed_files(base)
    qa = QdrantAdapter(url=None, embedder=CountingEmbedder(), collection="test_u8_legacy")
    s1 = await seed_memory(qdrant=qa, base_dir=base, allow_fallback=True)
    s2 = await seed_memory(qdrant=qa, base_dir=base, allow_fallback=True)
    assert s1["files"] == s2["files"] == 2  # 매번 전량 처리(스킵 없음)
    assert s2["unchanged_files"] == 0
    assert not (tmp_path / "cache").exists()  # 매니페스트 파일 생성 안 됨
    await qa.aclose()
