# -*- coding: utf-8 -*-
"""R1 targeted retrieval: priority corpus, freshness and volatile diagnostics."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from adapters.memory_adapter import MemoryAdapter
from adapters.qdrant_adapter import QdrantAdapter
from core.embeddings import HashEmbedder
from core import paths as path_contract
from core.router import SmartRouter
from core.schema_validator import SchemaValidator
from core.tools import ToolContext, tool_get_context
from scripts.seed_brain_memory import _iter_brain_source_files


def _goal(path: Path, *, id_: int, status: str, marker: str) -> None:
    path.write_text(
        "---\n"
        "vhk_format: 1\n"
        "type: goal\n"
        f"id: {id_}\n"
        f"title: {marker}\n"
        f"status: {status}\n"
        "priority: P0\n"
        "---\n\n"
        f"# {marker}\n",
        encoding="utf-8",
    )


def _brain(tmp_path: Path) -> tuple[Path, Path]:
    brain = tmp_path / "brain"
    memory = brain / "memory"
    (memory / "core").mkdir(parents=True)
    (memory / "wiki").mkdir(parents=True)
    (brain / "goals").mkdir(parents=True)
    (brain / "YOHAN-ECOSYSTEM-SOT.md").write_text(
        "# Ecosystem\nECOSYSTEM_GOLDEN canonical map", encoding="utf-8"
    )
    (brain / "ASSETS.md").write_text(
        "# Assets\nASSET_GOLDEN location contract", encoding="utf-8"
    )
    (brain / "AGENTS.md").write_text("# Agents\nentry contract", encoding="utf-8")
    (memory / "core" / "ecosystem-sot.yaml").write_text(
        "schema: ecosystem\nmarker: CORE_GOLDEN\n", encoding="utf-8"
    )
    (memory / "wiki" / "noise.md").write_text(
        "일반 지식 노트 ECOSYSTEM_GOLDEN", encoding="utf-8"
    )
    _goal(brain / "goals" / "10-active.md", id_=10, status="IN_PROGRESS", marker="ACTIVE_GOAL_GOLDEN")
    _goal(brain / "goals" / "11-done.md", id_=11, status="DONE", marker="DONE_GOAL_GOLDEN")
    return brain, memory


@pytest.mark.parametrize(
    ("query", "expected_path"),
    [
        ("ECOSYSTEM_GOLDEN canonical map", "YOHAN-ECOSYSTEM-SOT.md"),
        ("ASSET_GOLDEN location contract", "ASSETS.md"),
        ("ACTIVE_GOAL_GOLDEN", "goals/10-active.md"),
    ],
)
async def test_priority_corpus_golden_queries_land_in_top_five(
    tmp_path: Path, query: str, expected_path: str
):
    _brain(tmp_path)
    adapter = MemoryAdapter(base_dir=tmp_path / "brain" / "memory")
    adapters = {"memory": adapter}
    ctx = ToolContext(adapters, SmartRouter(adapters), SchemaValidator(), entity_catalog={})

    env = await tool_get_context(
        ctx,
        query,
        {"top_k": 5, "backends": ["memory"], "context_supplemental": False},
    )

    paths = [(record.get("data") or {}).get("_path") for record in env["data"]["matches"]]
    assert expected_path in paths[:5]
    receipt = env["data"]["retrieval_diagnostics"]
    assert receipt["volatile"] is True and receipt["persisted"] is False
    assert receipt["index"]["fresh"] is True
    assert len(receipt["index"]["revision"]) == 64


async def test_index_build_is_deterministic_and_query_does_not_open_or_rglob(
    tmp_path: Path, monkeypatch
):
    _brain(tmp_path)
    memory = tmp_path / "brain" / "memory"
    first = MemoryAdapter(base_dir=memory)
    second = MemoryAdapter(base_dir=memory)
    assert first._index_revision == second._index_revision

    def forbidden(*_args, **_kwargs):
        raise AssertionError("query happy path must not open files or enumerate recursively")

    monkeypatch.setattr(Path, "rglob", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    diagnostics: dict = {}
    results = await first.search(
        "ASSET_GOLDEN location contract", {"_search_diagnostics": diagnostics}
    )

    assert results[0]["data"]["_path"] == "ASSETS.md"
    assert diagnostics["filesystem_traversal"] is False
    assert diagnostics["query_file_opens"] == 0
    assert diagnostics["freshness_reason_code"] == "fresh"
    assert diagnostics["cold_index_build_ms"] >= 0
    assert diagnostics["warm_query_ms"] >= 0


async def test_index_edit_delete_and_add_fail_loud_until_refresh(tmp_path: Path):
    _brain(tmp_path)
    memory = tmp_path / "brain" / "memory"
    wiki = memory / "wiki"
    source = wiki / "noise.md"
    adapter = MemoryAdapter(base_dir=memory)

    old = source.stat().st_mtime_ns
    source.write_text("edited marker", encoding="utf-8")
    os.utime(source, ns=(old + 1_000_000_000, old + 1_000_000_000))
    with pytest.raises(RuntimeError, match="memory_index_stale:source_edited"):
        await adapter.search("edited marker")
    adapter.refresh_search_index()
    assert await adapter.search("edited marker")

    source.unlink()
    with pytest.raises(RuntimeError, match="memory_index_stale:source_deleted"):
        await adapter.search("edited marker")
    adapter.refresh_search_index()

    directory_mtime = wiki.stat().st_mtime_ns
    (wiki / "new.md").write_text("NEW_FILE_GOLDEN", encoding="utf-8")
    os.utime(wiki, ns=(directory_mtime + 1_000_000_000, directory_mtime + 1_000_000_000))
    with pytest.raises(RuntimeError, match="memory_index_stale:directory_membership_changed"):
        await adapter.search("NEW_FILE_GOLDEN")
    adapter.refresh_search_index()
    assert await adapter.search("NEW_FILE_GOLDEN")


def test_vector_seed_source_population_includes_root_and_active_goal_only(tmp_path: Path):
    _brain(tmp_path)
    memory = tmp_path / "brain" / "memory"
    sources = {(kind, path.name) for kind, path in _iter_brain_source_files(memory)}

    assert ("root", "YOHAN-ECOSYSTEM-SOT.md") in sources
    assert ("root", "ASSETS.md") in sources
    assert ("goal", "10-active.md") in sources
    assert ("goal", "11-done.md") not in sources
    assert ("core", "ecosystem-sot.yaml") in sources


async def test_get_context_keeps_vector_default_and_reports_unavailable_collections(tmp_path: Path):
    _brain(tmp_path)
    memory = MemoryAdapter(base_dir=tmp_path / "brain" / "memory")
    qdrant = QdrantAdapter(embedder=HashEmbedder(), collection="present")
    await qdrant.create(
        "resource",
        {"resource_id": "r1", "url": "https://example.com/r1", "title": "vector evidence"},
    )
    adapters = {"memory": memory, "qdrant": qdrant}
    ctx = ToolContext(adapters, SmartRouter(adapters), SchemaValidator(), entity_catalog={})

    env = await tool_get_context(
        ctx, "ASSET_GOLDEN location contract", {"top_k": 5, "context_supplemental": False}
    )

    receipt = env["data"]["retrieval_diagnostics"]
    assert set(receipt["sources"]["attempted"]) == {"memory", "qdrant"}
    assert "present" in receipt["collections"]["available"]
    unavailable = receipt["collections"]["unavailable"]
    assert unavailable
    assert all(item["reason_code"] == "collection_query_failed" for item in unavailable)
    await qdrant.aclose()


def test_qdrant_path_and_url_conflict_fails_loud():
    with pytest.raises(ValueError, match="qdrant_config_conflict:path_and_url"):
        QdrantAdapter(url="http://127.0.0.1:6333", path="ignored-local-path")


def test_local_mcp_memory_is_not_promoted_to_brain_root(tmp_path: Path, monkeypatch):
    local_repo = tmp_path / "yohan-mcp"
    local_memory = local_repo / "memory"
    local_memory.mkdir(parents=True)
    monkeypatch.setattr(path_contract, "ROOT", local_repo)

    assert path_contract.resolve_brain_root(local_memory) is None
