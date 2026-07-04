# -*- coding: utf-8 -*-
"""U3 — payload type 검색 가중(qdrant_adapter) 유닛 테스트.

실측 문제: brain_memory 의미 질의 top-5 를 대량 ingest 청크가 점유 → decisions·rules
(×1.25)/knowledge-hub(×1.2)/wiki(×1.15) 부스트, ingest 감쇠(×0.85)로 재순위화.
:memory: Qdrant + 고정벡터 임베더로 결정적으로 검증한다(실서버/실임베더 불필요).
"""
import logging
import uuid

from qdrant_client import models

from adapters.qdrant_adapter import (
    DEFAULT_TYPE_WEIGHTS,
    QdrantAdapter,
    load_type_weights,
)
from core.embeddings import HashEmbedder


class FixedEmbedder:
    """쿼리를 항상 같은 단위벡터로 임베딩 — 포인트 벡터를 수작업 주입해 score 를 완전 통제."""

    name, dim = "fixed", 8

    def embed(self, texts):
        return [[1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0] for _ in texts]


def _unit_x(sign: float = 1.0) -> list[float]:
    return [sign * 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]


async def _seed_collection(qa: QdrantAdapter, coll: str, points_spec: list[tuple[dict, list[float]]]):
    client = qa._get_client()
    await client.create_collection(
        coll, vectors_config=models.VectorParams(size=qa.embedder.dim, distance=models.Distance.COSINE)
    )
    points = [
        models.PointStruct(id=str(uuid.uuid4()), vector=vec, payload=payload)
        for payload, vec in points_spec
    ]
    await client.upsert(coll, points=points)


# ── load_type_weights — 기본/오버라이드/무효 처리 ─────────────────
def test_load_type_weights_defaults():
    w = load_type_weights()
    assert w == DEFAULT_TYPE_WEIGHTS
    assert w is not DEFAULT_TYPE_WEIGHTS  # 복사본 — 호출자 변형이 기본값을 오염 못 함
    # 골든 쿼리 실측 튜닝 서열 고정: 1차인(결정·규칙) > 허브 > 위키 > 1.0(무가중) > ingest.
    assert w["brain:decisions"] == w["brain:rules"] == 1.25
    assert w["brain:knowledge-hub"] == 1.2
    assert w["brain:wiki"] == 1.15
    assert w["brain:ingest"] == 0.85
    assert w["brain:decisions"] > w["brain:knowledge-hub"] > w["brain:wiki"] > 1.0 > w["brain:ingest"]


def test_load_type_weights_env_merges_not_replaces(monkeypatch):
    monkeypatch.setenv("QDRANT_TYPE_WEIGHTS", '{"brain:ingest": 0.7, "brain:projects": 1.1}')
    w = load_type_weights()
    assert w["brain:ingest"] == 0.7          # 오버라이드 적용
    assert w["brain:projects"] == 1.1        # 신규 키 추가
    assert w["brain:decisions"] == 1.25      # 미지정 키는 기본값 유지(병합)


def test_load_type_weights_invalid_json_falls_back(monkeypatch, caplog):
    monkeypatch.setenv("QDRANT_TYPE_WEIGHTS", "{잘못된 json")
    with caplog.at_level(logging.WARNING):
        w = load_type_weights()
    assert w == DEFAULT_TYPE_WEIGHTS  # env 전체 무시 → 기본값
    assert any("QDRANT_TYPE_WEIGHTS" in r.message for r in caplog.records)


def test_load_type_weights_nonpositive_rejects_whole_env(monkeypatch, caplog):
    # 무효 항목이 하나라도 있으면 부분 적용하지 않고 env 전체를 버린다(설정 착각 방지).
    monkeypatch.setenv("QDRANT_TYPE_WEIGHTS", '{"brain:ingest": 0.5, "brain:wiki": -1}')
    with caplog.at_level(logging.WARNING):
        w = load_type_weights()
    assert w == DEFAULT_TYPE_WEIGHTS
    assert w["brain:ingest"] == 0.85  # 유효해 보이던 0.5 도 적용 안 됨(전체 거부)


def test_load_type_weights_non_dict_falls_back(monkeypatch):
    monkeypatch.setenv("QDRANT_TYPE_WEIGHTS", '["brain:ingest", 0.5]')
    assert load_type_weights() == DEFAULT_TYPE_WEIGHTS


# ── _type_weight — payload 기준 가중 조회 ─────────────────────────
def test_type_weight_lookup():
    qa = QdrantAdapter(url=None, embedder=HashEmbedder())
    assert qa._type_weight({"type": "brain:rules"}) == 1.25
    assert qa._type_weight({"type": "brain:wiki"}) == 1.15
    assert qa._type_weight({"type": "brain:ingest"}) == 0.85
    assert qa._type_weight({"type": "brain:projects"}) == 1.0  # 매핑 없는 brain 폴더
    assert qa._type_weight({"title": "type 키 없음"}) == 1.0    # 관제탑/트리플/레거시
    assert qa._type_weight({"type": 123}) == 1.0                # 비문자열 type 방어


# ── search 재순위화 — 가중이 병합 정렬(=RRF 입력 순위)에 반영 ──────
async def test_search_reranks_by_type_weight():
    # 동일 벡터(원점수 동률) 4점 — 가중만으로 순서 결정:
    # decisions(1.25) > wiki(1.15) > 무타입(1.0) > ingest(0.85).
    qa = QdrantAdapter(url=None, embedder=FixedEmbedder(), collection="brain_memory")
    qa.search_collections = ["brain_memory"]
    await _seed_collection(qa, "brain_memory", [
        ({"type": "brain:ingest", "title": "잉제스트"}, _unit_x()),
        ({"type": "brain:wiki", "title": "위키"}, _unit_x()),
        ({"type": "brain:decisions", "title": "결정"}, _unit_x()),
        ({"title": "무타입"}, _unit_x()),
    ])
    res = await qa.search("질의", {"top_k": 4})
    titles = [r["data"].get("title") for r in res]
    assert titles == ["결정", "위키", "무타입", "잉제스트"]
    scores = [r["score"] for r in res]
    for got, want in zip(scores, (1.25, 1.15, 1.0, 0.85)):
        assert abs(got - want) < 1e-6
    await qa.aclose()


async def test_search_weight_can_flip_raw_score_order():
    # ingest 원점수가 근소 우위여도 감쇠×0.85 로 부스트된 decisions 가 앞선다(실측 문제 재현 축소판).
    # 벡터 각도로 원점수 차 생성: ingest=cos0°(1.0), decisions=cos약간(0.98).
    qa = QdrantAdapter(url=None, embedder=FixedEmbedder(), collection="brain_memory")
    qa.search_collections = ["brain_memory"]
    near = [0.98, (1 - 0.98 ** 2) ** 0.5, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]  # cos=0.98
    await _seed_collection(qa, "brain_memory", [
        ({"type": "brain:ingest", "title": "잉제스트"}, _unit_x()),          # raw 1.0 → 0.85
        ({"type": "brain:decisions", "title": "결정"}, near),               # raw 0.98 → 1.225
    ])
    res = await qa.search("질의", {"top_k": 2})
    assert [r["data"]["title"] for r in res] == ["결정", "잉제스트"]
    await qa.aclose()


async def test_search_negative_scores_not_weighted():
    # COSINE 음수 score 에 가중을 곱하면 감쇠(0.85)가 오히려 순위를 올린다(0.85×-1 > -1)
    # → 음수는 원점수 유지를 단정한다.
    qa = QdrantAdapter(url=None, embedder=FixedEmbedder(), collection="brain_memory")
    qa.search_collections = ["brain_memory"]
    await _seed_collection(qa, "brain_memory", [
        ({"type": "brain:ingest", "title": "음수잉제스트"}, _unit_x(-1.0)),  # raw -1.0
        ({"title": "음수무타입"}, _unit_x(-1.0)),                            # raw -1.0
    ])
    res = await qa.search("질의", {"top_k": 2})
    assert all(abs(r["score"] - (-1.0)) < 1e-6 for r in res)  # 둘 다 -1.0 그대로(가중 미적용)
    await qa.aclose()


async def test_search_env_override_plumbs_into_ranking(monkeypatch):
    # env 오버라이드가 어댑터 생성 → search 재순위화까지 관통하는지(배선 검증).
    monkeypatch.setenv("QDRANT_TYPE_WEIGHTS", '{"brain:ingest": 3.0}')
    qa = QdrantAdapter(url=None, embedder=FixedEmbedder(), collection="brain_memory")
    qa.search_collections = ["brain_memory"]
    await _seed_collection(qa, "brain_memory", [
        ({"type": "brain:ingest", "title": "잉제스트"}, _unit_x()),
        ({"type": "brain:wiki", "title": "위키"}, _unit_x()),
    ])
    res = await qa.search("질의", {"top_k": 2})
    assert [r["data"]["title"] for r in res] == ["잉제스트", "위키"]  # 3.0 > 1.15
    await qa.aclose()
