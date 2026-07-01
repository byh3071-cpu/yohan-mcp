# -*- coding: utf-8 -*-
"""Smart Router — 백엔드 선택 + RRF 융합 단위테스트."""
import pytest

from adapters.base import BackendAdapter, health, make_record
from core.router import SmartRouter


class FakeAdapter(BackendAdapter):
    def __init__(self, name, records=None, raise_exc=None):
        self.name = name
        self._records = records or []
        self._raise = raise_exc

    async def search(self, query, opts=None):
        if self._raise:
            raise self._raise
        return self._records

    async def create(self, type_, data):
        return make_record("x", type_, self.name, data)

    async def update(self, id_, data, type_=None):
        return make_record(id_, "t", self.name, data)

    async def health_check(self):
        return health(True, 1, "fake")


def recs(name, ids):
    return [make_record(i, "summary", name, {"id": i}) for i in ids]


async def test_rrf_fusion_scores():
    a = FakeAdapter("notion", recs("notion", ["x1", "x2", "x3"]))
    b = FakeAdapter("memory", recs("memory", ["x2", "x4"]))
    r = SmartRouter({"notion": a, "memory": b}, k=60)
    out = await r.search("q")
    by_id = {x["id"]: x for x in out["results"]}

    # x2 는 두 백엔드에서 등장 → 최상위, 점수 = 1/(60+2) + 1/(60+1)
    assert out["results"][0]["id"] == "x2"
    assert by_id["x2"]["rrf_score"] == pytest.approx(1 / 62 + 1 / 61)
    assert by_id["x1"]["rrf_score"] == pytest.approx(1 / 61)
    assert by_id["x3"]["rrf_score"] == pytest.approx(1 / 63)
    assert set(by_id["x2"]["sources"]) == {"notion", "memory"}
    assert out["sources_used"] == ["notion", "memory"]


async def test_rrf_ordering():
    a = FakeAdapter("notion", recs("notion", ["x1", "x2", "x3"]))
    b = FakeAdapter("memory", recs("memory", ["x2", "x4"]))
    r = SmartRouter({"notion": a, "memory": b})
    out = await r.search("q")
    order = [x["id"] for x in out["results"]]
    # x2(중복) > x1 > x4 > x3
    assert order == ["x2", "x1", "x4", "x3"]


def test_select_backends_default_and_opts():
    r = SmartRouter({"notion": FakeAdapter("notion"), "memory": FakeAdapter("memory"), "qdrant": FakeAdapter("qdrant")})
    assert r.select_backends("q") == ["notion", "memory", "qdrant"]  # P2.5 부터 qdrant 활성
    assert r.select_backends("q", {"backends": ["memory"]}) == ["memory"]
    assert r.select_backends("q", {"backends": ["nope"]}) == []


async def test_search_three_source_rrf():
    a = FakeAdapter("notion", recs("notion", ["x1", "x2"]))
    b = FakeAdapter("memory", recs("memory", ["x2"]))
    c = FakeAdapter("qdrant", recs("qdrant", ["x2", "x3"]))
    r = SmartRouter({"notion": a, "memory": b, "qdrant": c})
    out = await r.search("q")
    assert set(out["sources_used"]) == {"notion", "memory", "qdrant"}
    by = {x["id"]: x for x in out["results"]}
    assert set(by["x2"]["sources"]) == {"notion", "memory", "qdrant"}  # 3중 융합
    assert out["results"][0]["id"] == "x2"  # 최다 출처 → 최상위


async def test_search_skips_unimplemented_and_errors():
    a = FakeAdapter("notion", recs("notion", ["x1"]))
    b = FakeAdapter("memory", raise_exc=NotImplementedError("P2.5"))
    r = SmartRouter({"notion": a, "memory": b})
    out = await r.search("q")
    assert out["sources_used"] == ["notion"]
    assert "memory" in out["errors"]
    assert [x["id"] for x in out["results"]] == ["x1"]


async def test_search_isolates_backend_exception():
    a = FakeAdapter("notion", recs("notion", ["x1"]))
    b = FakeAdapter("memory", raise_exc=RuntimeError("boom"))
    r = SmartRouter({"notion": a, "memory": b})
    out = await r.search("q")
    assert out["sources_used"] == ["notion"]
    assert "memory" in out["errors"] and "boom" in out["errors"]["memory"]


async def test_rrf_single_backend_multichunk_no_inflation():
    # 단일 백엔드가 같은 (type,id) 를 여러 청크로 반환해도 점수가 합산되지 않는다.
    # RRF 정의: 각 백엔드는 한 엔티티당 최선(첫) rank 한 번만 기여.
    multichunk = [
        make_record("page1", "knowledge_base", "qdrant", {"chunk": 0}),  # rank 1
        make_record("page1", "knowledge_base", "qdrant", {"chunk": 1}),  # rank 2 (중복 — 무시)
        make_record("page2", "knowledge_base", "qdrant", {"chunk": 0}),  # rank 3
        make_record("page1", "knowledge_base", "qdrant", {"chunk": 2}),  # rank 4 (중복 — 무시)
    ]
    a = FakeAdapter("qdrant", multichunk)
    r = SmartRouter({"qdrant": a}, k=60)
    out = await r.search("q")
    by_id = {x["id"]: x for x in out["results"]}
    # page1 은 청크 3개지만 첫 rank(1) 만 기여 → 1/61, 청크 수 인플레이션 없음
    assert by_id["page1"]["rrf_score"] == pytest.approx(1 / 61)
    # page2 는 원래 리스트 rank 3 유지
    assert by_id["page2"]["rrf_score"] == pytest.approx(1 / 63)
    # 청크 수와 무관하게 결과 엔티티는 2개, 각 단일 출처
    assert len(out["results"]) == 2
    assert by_id["page1"]["sources"] == ["qdrant"]
    # page1(1/61) > page2(1/63) — 청크 수가 아니라 순위가 결정
    assert [x["id"] for x in out["results"]] == ["page1", "page2"]


async def test_rrf_no_cross_type_merge():
    # 서로 다른 타입이 우연히 같은 id 를 가져도 하나로 합쳐지면 안 됨
    a = FakeAdapter("notion", [make_record("x1", "summary", "notion", {"summary_id": "x1"})])
    b = FakeAdapter("memory", [make_record("x1", "decision", "memory", {"decision_id": "x1"})])
    r = SmartRouter({"notion": a, "memory": b})
    out = await r.search("q")
    assert len(out["results"]) == 2
    for res in out["results"]:
        assert len(res["sources"]) == 1  # 각자 단일 출처, provenance 무결성
    types = {res["type"] for res in out["results"]}
    assert types == {"summary", "decision"}
