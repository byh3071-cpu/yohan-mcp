# -*- coding: utf-8 -*-
"""Graph-Aware Context Resolver — 외부 Qdrant 없이 계약을 고정한다."""
from __future__ import annotations

from pathlib import Path

from core.context_resolver import (
    GraphAwareContextResolver,
    extract_one_hop_edges,
    load_project_catalog,
    resolve_entities,
)
from core.schema_validator import SchemaValidator
from core.tools import ToolContext, tool_get_context


CATALOG = {
    "mova": ("mova",),
    "yohan-mcp": ("yohan-mcp", "yohan mcp"),
    "요한 생태계": ("요한 생태계", "yohan ecosystem"),
}


def _record(id_: str, type_: str, data: dict) -> dict:
    return {
        "id": id_,
        "type": type_,
        "backend": "fake",
        "data": data,
        "rrf_score": 0.1,
        "sources": ["fake"],
    }


def _edge(id_: str, subject: str, relation: str, obj: str, source: str = "") -> dict:
    return _record(
        id_,
        "ontology_triples",
        {
            "subject": subject,
            "relation": relation,
            "object": obj,
            "source": source,
            "confidence": 4,
            "text": f"{subject} {relation} {obj}",
        },
    )


def _search_result(results: list[dict]) -> dict:
    return {
        "results": results,
        "sources_used": ["fake"],
        "errors": {},
        "diagnostics": {},
    }


def test_load_project_catalog_reads_only_registry(tmp_path: Path):
    registry = tmp_path / "core" / "inheritance-registry.yaml"
    registry.parent.mkdir(parents=True)
    registry.write_text(
        "schema: inheritance-registry\nrepos:\n  mova:\n    tier: B\n  yohan-mcp:\n    tier: S\n",
        encoding="utf-8",
    )

    catalog = load_project_catalog(tmp_path)

    assert catalog["mova"] == ("mova",)
    assert set(catalog["yohan-mcp"]) >= {"yohan-mcp", "yohan mcp"}
    assert "요한 생태계" in catalog


def test_resolve_entities_for_three_golden_queries():
    assert [e.canonical for e in resolve_entities("요한 생태계 구조 알려줘", CATALOG)] == ["요한 생태계"]
    assert [e.canonical for e in resolve_entities("MOVA 프로젝트 맥락", CATALOG)] == ["mova"]
    assert [e.canonical for e in resolve_entities("yohan mcp 검색 배관", CATALOG)] == ["yohan-mcp"]


def test_extract_one_hop_edges_excludes_two_hop_and_unrelated():
    entities = resolve_entities("yohan-mcp 배관", CATALOG)
    records = [
        _edge("e1", "yohan-mcp", "embodies", "신경(배관·전달)", "ecosystem-sot"),
        _edge("e2", "신경(배관·전달)", "depends_on", "Qdrant"),
        _edge("e3", "MOVA", "part_of", "제품군"),
    ]

    edges = extract_one_hop_edges(records, entities, max_edges=8)

    assert [(e["subject"], e["relation"], e["object"]) for e in edges] == [
        ("yohan-mcp", "embodies", "신경(배관·전달)")
    ]


async def test_sufficient_primary_evidence_skips_supplemental_search():
    resolver = GraphAwareContextResolver(CATALOG)
    primary = _search_result(
        [
            _edge("e1", "yohan-mcp", "embodies", "신경(배관·전달)"),
            _record("d1", "brain_memory", {"path": "docs/a.md", "text": "yohan-mcp 배관 설명"}),
            _record("d2", "brain_memory", {"path": "docs/b.md", "text": "신경 역할 근거"}),
        ]
    )

    async def forbidden_search(query, opts):
        raise AssertionError("충분한 근거에서 보충 검색을 호출하면 안 된다")

    result = await resolver.resolve("yohan-mcp 설명", primary, forbidden_search, {"top_k": 5})

    assert result.supplemental_searches == 0
    assert result.entities[0]["canonical"] == "yohan-mcp"
    assert len(result.graph_edges) == 1


async def test_missing_evidence_runs_at_most_one_supplemental_search():
    resolver = GraphAwareContextResolver(CATALOG)
    calls: list[str] = []

    async def supplemental(query, opts):
        calls.append(query)
        return _search_result(
            [_record("d1", "brain_memory", {"path": "YOHAN-ECOSYSTEM-SOT.md", "text": "MOVA 프로젝트 근거"})]
        )

    result = await resolver.resolve("MOVA 프로젝트", _search_result([]), supplemental, {"top_k": 5})

    assert len(calls) == 1
    assert result.supplemental_searches == 1
    assert result.matches[0]["id"] == "d1"


async def test_match_and_character_budgets_are_enforced():
    resolver = GraphAwareContextResolver(CATALOG)
    primary = _search_result(
        [
            _record("d1", "brain_memory", {"text": "MOVA-AAAA"}),
            _record("d2", "brain_memory", {"text": "MOVA-BBBB"}),
            _record("d3", "brain_memory", {"text": "MOVA-CCCC"}),
        ]
    )

    result = await resolver.resolve(
        "MOVA",
        primary,
        None,
        {"context_max_matches": 2, "context_char_budget": 12, "min_evidence": 1},
    )

    assert len(result.matches) <= 2
    assert result.context_chars <= 12


async def test_negative_top_k_keeps_bounded_unlimited_compatibility():
    resolver = GraphAwareContextResolver(CATALOG)
    primary = _search_result(
        [
            _record("d1", "brain_memory", {"text": "MOVA 첫 근거"}),
            _record("d2", "brain_memory", {"text": "MOVA 둘째 근거"}),
        ]
    )

    result = await resolver.resolve("MOVA", primary, None, {"top_k": -1})

    assert [record["id"] for record in result.matches] == ["d1", "d2"]
    assert result.max_matches > 0


async def test_supplemental_failure_preserves_primary_context():
    resolver = GraphAwareContextResolver(CATALOG)
    primary = _search_result([_record("d1", "brain_memory", {"text": "MOVA 단일 근거"})])

    async def broken_search(query, opts):
        raise RuntimeError("offline")

    result = await resolver.resolve("MOVA", primary, broken_search, {"top_k": 5})

    assert result.matches[0]["id"] == "d1"
    assert result.supplemental_result["errors"]["context_resolver"].startswith("RuntimeError")


class _TrackingRouter:
    def __init__(self):
        self.calls: list[str] = []

    async def search(self, query, opts=None):
        self.calls.append(query)
        if len(self.calls) == 1:
            return _search_result([_edge("e1", "요한 생태계", "comprises", "MOVA")])
        return _search_result(
            [_record("d1", "brain_memory", {"path": "ecosystem.md", "text": "MOVA는 요한 생태계 프로젝트"})]
        )


async def test_tool_get_context_wires_entities_graph_and_single_supplement(tmp_path: Path):
    router = _TrackingRouter()
    ctx = ToolContext({}, router, SchemaValidator(), entity_catalog=CATALOG)

    env = await tool_get_context(ctx, "요한 생태계에서 MOVA가 뭐야", {"top_k": 5})

    assert len(router.calls) == 2
    assert [e["canonical"] for e in env["data"]["entities"]] == ["요한 생태계", "mova"]
    assert env["data"]["graph_edges"][0]["relation"] == "comprises"
    assert env["data"]["resolver"]["supplemental_searches"] == 1
    assert env["data"]["matches"][0]["id"] == "d1"
