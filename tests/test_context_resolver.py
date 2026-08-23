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


def test_load_project_catalog_reports_registry_only_as_degraded(tmp_path: Path):
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
    assert catalog.diagnostics["degraded"] is True
    assert catalog.diagnostics["reason_code"] == "live_snapshot_missing"
    assert len(catalog.diagnostics["revision"]) == 64


def test_project_catalog_reports_missing_corrupt_and_invalid_keys(tmp_path: Path):
    missing = load_project_catalog(tmp_path)
    assert missing.diagnostics["degraded"] is True
    assert missing.diagnostics["reason_code"] == (
        "live_snapshot_missing+declarative_registry_missing"
    )

    registry = tmp_path / "core" / "inheritance-registry.yaml"
    registry.parent.mkdir(parents=True)
    registry.write_text("repos: [unterminated", encoding="utf-8")
    corrupt = load_project_catalog(tmp_path)
    assert corrupt.diagnostics["degraded"] is True
    assert corrupt.diagnostics["reason_code"] == (
        "live_snapshot_missing+declarative_registry_corrupt_or_invalid"
    )

    registry.write_text(
        'repos:\n  mova: {}\n  "---": {}\n  "../escape": {}\n', encoding="utf-8"
    )
    loaded = load_project_catalog(tmp_path)
    assert set(loaded) >= {"mova", "요한 생태계"}
    assert "---" not in loaded and "../escape" not in loaded
    assert loaded.diagnostics["invalid_keys_excluded"] == 2
    assert resolve_entities("UNKNOWN-PROJECT", loaded, project="UNKNOWN-PROJECT") == []


def test_project_catalog_includes_provider_observed_only_repository(tmp_path: Path):
    core = tmp_path / "core"
    core.mkdir(parents=True)
    (core / "repository-live-snapshot.yaml").write_text(
        "schema: repository-live-snapshot\n"
        "repositories:\n"
        "  - {repo_name: Birthday_app, source_status: observed}\n"
        "  - {repo_name: '---', source_status: observed}\n",
        encoding="utf-8",
    )
    (core / "inheritance-registry.yaml").write_text(
        "repos:\n  mova: {}\n  ai-router: {}\n",
        encoding="utf-8",
    )

    catalog = load_project_catalog(tmp_path)

    assert catalog.diagnostics["degraded"] is False
    assert catalog.diagnostics["observed_count"] == 1
    assert resolve_entities("Birthday_app 진행 상황", catalog)[0].canonical == "Birthday_app"
    assert "ai-router" in catalog
    assert "---" not in catalog


def test_resolve_entities_for_three_golden_queries():
    assert [e.canonical for e in resolve_entities("요한 생태계 구조 알려줘", CATALOG)] == ["요한 생태계"]
    assert [e.canonical for e in resolve_entities("MOVA 프로젝트 맥락", CATALOG)] == ["mova"]
    assert [e.canonical for e in resolve_entities("yohan mcp 검색 배관", CATALOG)] == ["yohan-mcp"]


def test_unknown_uppercase_concept_is_not_promoted_to_project():
    assert resolve_entities("퍼스널 AGI 경험 복리", CATALOG) == []
    assert resolve_entities("퍼스널 AGI 경험 복리", CATALOG, project="AGI") == []


def test_punctuation_only_registry_name_never_matches_hyphenated_query():
    catalog = {**CATALOG, "yohan-brain": ("yohan-brain",), "---": ("---",)}

    entities = resolve_entities(
        "요한 생태계에서 yohan-mcp와 yohan-brain은 어떤 관계야?",
        catalog,
        max_entities=3,
    )

    assert [entity.canonical for entity in entities] == [
        "요한 생태계",
        "yohan-mcp",
        "yohan-brain",
    ]


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


async def test_query_evidence_outranks_broad_entity_mention():
    resolver = GraphAwareContextResolver(CATALOG)
    primary = _search_result(
        [
            _record("broad", "brain_memory", {"text": "요한 생태계 일반 소개"}),
            _record("target", "brain_memory", {"text": "퍼스널 AGI 관계와 정의"}),
        ]
    )

    result = await resolver.resolve(
        "요한 생태계와 퍼스널 AGI 관계",
        primary,
        None,
        {"top_k": 2, "min_evidence": 1},
    )

    assert [record["id"] for record in result.matches] == ["target", "broad"]


async def test_budget_preserves_locators_and_multiple_evidence_slots():
    resolver = GraphAwareContextResolver(CATALOG)
    primary = _search_result(
        [
            _record("d1", "brain_memory", {"body": "A" * 500, "_path": "docs/one.md"}),
            _record("d2", "brain_memory", {"body": "B" * 500, "_path": "docs/two.md"}),
            _record("d3", "brain_memory", {"body": "C" * 500, "_path": "docs/three.md"}),
        ]
    )

    result = await resolver.resolve(
        "evidence",
        primary,
        None,
        {
            "context_max_matches": 3,
            "context_char_budget": 120,
            "min_evidence": 1,
        },
    )

    assert [record["data"]["_path"] for record in result.matches] == [
        "docs/one.md",
        "docs/two.md",
        "docs/three.md",
    ]
    assert result.context_chars <= 120


async def test_budget_excludes_record_instead_of_truncating_locator():
    resolver = GraphAwareContextResolver(CATALOG)
    primary = _search_result(
        [_record("d1", "brain_memory", {"_path": "memory/wiki/evidence.md", "body": "A" * 100})]
    )

    result = await resolver.resolve(
        "evidence",
        primary,
        None,
        {"context_max_matches": 1, "context_char_budget": 8, "min_evidence": 1},
    )

    assert result.matches == []


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
        self.call_opts: list[dict] = []

    async def search(self, query, opts=None):
        self.calls.append(query)
        self.call_opts.append(dict(opts or {}))
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
    assert router.call_opts[0]["top_k"] > 5
    assert [e["canonical"] for e in env["data"]["entities"]] == ["요한 생태계", "mova"]
    assert env["data"]["graph_edges"][0]["relation"] == "comprises"
    assert env["data"]["resolver"]["supplemental_searches"] == 1
    assert env["data"]["resolver"]["primary_candidate_limit"] == router.call_opts[0]["top_k"]
    assert env["data"]["matches"][0]["id"] == "d1"


class _GraphBelowOutputCutoffRouter:
    def __init__(self):
        self.seen_top_k = 0

    async def search(self, query, opts=None):
        self.seen_top_k = int((opts or {}).get("top_k", 0))
        candidates = [
            _record(f"d{i}", "brain_memory", {"path": f"docs/{i}.md", "text": f"yohan-mcp 근거 {i}"})
            for i in range(8)
        ]
        candidates.append(_edge("e1", "yohan-mcp", "depends_on", "yohan-brain"))
        return _search_result(candidates[: self.seen_top_k])


async def test_tool_get_context_reserves_candidates_but_keeps_output_budget(tmp_path: Path):
    catalog = {**CATALOG, "yohan-brain": ("yohan-brain",)}
    router = _GraphBelowOutputCutoffRouter()
    ctx = ToolContext({}, router, SchemaValidator(), entity_catalog=catalog)

    env = await tool_get_context(ctx, "yohan-mcp와 yohan-brain 관계", {"top_k": 5})

    assert router.seen_top_k >= 9
    assert len(env["data"]["matches"]) <= 5
    assert env["data"]["graph_edges"][0]["relation"] == "depends_on"
