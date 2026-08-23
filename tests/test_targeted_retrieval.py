# -*- coding: utf-8 -*-
"""R1 targeted retrieval: priority corpus, freshness and volatile diagnostics."""
from __future__ import annotations

import hashlib
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest
import yaml

from adapters.memory_adapter import (
    MemoryAdapter,
    _BRAIN_CORE_FILES,
    _BRAIN_ROOT_SOT_FILES,
    _BRAIN_STATE_FILES,
)
from adapters.qdrant_adapter import QdrantAdapter, _safe_error_detail
from core.context_resolver import load_project_catalog
from core.embeddings import HashEmbedder
from core import paths as path_contract
from core.router import SmartRouter
from core.schema_validator import SchemaValidator
from core.tools import ToolContext, tool_get_context
from scripts.seed_brain_memory import (
    _iter_brain_source_files,
    _rel_of,
    _safe_embedding_text,
    _validate_yaml_for_embedding,
)


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
    (memory / "rules").mkdir(parents=True)
    (memory / "logs" / "sessions").mkdir(parents=True)
    (memory / "ingest" / "insights").mkdir(parents=True)
    (memory / "ingest" / "url").mkdir(parents=True)
    (brain / "goals").mkdir(parents=True)
    (brain / "docs" / "adr").mkdir(parents=True)
    (brain / "docs" / "specs").mkdir(parents=True)
    (brain / "docs" / "troubleshooting").mkdir(parents=True)
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
    (memory / "soul.yaml").write_text("identity: yohan\n", encoding="utf-8")
    (memory / "active-project.yaml").write_text("project: active\n", encoding="utf-8")
    (memory / "wiki" / "noise.md").write_text(
        "일반 지식 노트 ECOSYSTEM_GOLDEN", encoding="utf-8"
    )
    _goal(brain / "goals" / "10-active.md", id_=10, status="IN_PROGRESS", marker="ACTIVE_GOAL_GOLDEN")
    _goal(brain / "goals" / "11-done.md", id_=11, status="DONE", marker="DONE_GOAL_GOLDEN")
    _goal(brain / "goals" / "12-cancelled.md", id_=12, status="CANCELLED", marker="CANCELLED_GOAL")
    _goal(brain / "goals" / "13-archived.md", id_=13, status="ARCHIVED", marker="ARCHIVED_GOAL")
    _goal(brain / "goals" / "14-superseded.md", id_=14, status="SUPERSEDED", marker="SUPERSEDED_GOAL")
    for number, status in ((1, "Accepted"), (2, "Proposed"), (3, "Rejected")):
        (brain / "docs" / "adr" / f"ADR-{number:03d}-fixture.md").write_text(
            f"---\nid: ADR-{number:03d}\ntype: adr\ntitle: Fixture {number}\nstatus: {status}\n---\n# Fixture {number}\n",
            encoding="utf-8",
        )
    (brain / "docs" / "specs" / "spec.md").write_text("SPEC_CORPUS", encoding="utf-8")
    (brain / "docs" / "troubleshooting" / "fix.md").write_text("TROUBLE_CORPUS", encoding="utf-8")
    (memory / "rules" / "rule.md").write_text("RULE_CORPUS", encoding="utf-8")
    (memory / "logs" / "sessions" / "session.md").write_text("LOG_CORPUS", encoding="utf-8")
    (memory / "ingest" / "insights" / "insight.md").write_text("INSIGHT_CORPUS", encoding="utf-8")
    (memory / "ingest" / "url" / "raw.md").write_text("URL_EXCLUDED", encoding="utf-8")
    return brain, memory


def _write_minimal_contract(brain: Path) -> Path:
    exact = [
        *_BRAIN_ROOT_SOT_FILES,
        *(f"memory/{name}" for name in _BRAIN_STATE_FILES),
        *(f"memory/core/{name}" for name in _BRAIN_CORE_FILES),
    ]
    path = brain / "memory" / "core" / "retrieval-contract.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "schema": "brain-retrieval-contract",
                "version": "fixture-1",
                "status": "active",
                "retrieval": {
                    "lexical": {
                        "required": True,
                        "request_time_recursive_scan": "forbidden",
                    }
                },
                "corpus": {"P0": {"exact_files": exact}},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


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
    assert diagnostics["recursive_traversals"] == 0
    assert diagnostics["source_open_count"] == 0
    assert diagnostics["stat_checks"] > 0
    assert diagnostics["freshness_reason_code"] == "fresh"
    assert diagnostics["index_build_ms"] >= 0
    assert diagnostics["query_latency_ms"] >= 0


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


async def test_same_size_edit_with_restored_mtime_is_stale(tmp_path: Path):
    _brain(tmp_path)
    memory = tmp_path / "brain" / "memory"
    source = memory / "wiki" / "noise.md"
    adapter = MemoryAdapter(base_dir=memory)
    original = source.read_text(encoding="utf-8")
    old_stat = source.stat()
    replacement = "X" * len(original)
    source.write_text(replacement, encoding="utf-8")
    os.utime(source, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))

    with pytest.raises(RuntimeError, match="memory_index_stale:source_edited"):
        await adapter.search(replacement)


def test_brain_contract_and_p0_omissions_fail_loud(tmp_path: Path):
    brain, memory = _brain(tmp_path)
    (memory / "core" / "ecosystem-contract.yaml").write_text(
        "schema: ecosystem-contract\n", encoding="utf-8"
    )
    with pytest.raises(RuntimeError, match="memory_contract_invalid:missing_manifest"):
        MemoryAdapter(base_dir=memory)

    _write_minimal_contract(brain)
    with pytest.raises(RuntimeError, match="memory_contract_invalid:missing_p0"):
        MemoryAdapter(base_dir=memory)


def test_vector_seed_source_population_matches_retrieval_contract_tiers(tmp_path: Path):
    brain, _ = _brain(tmp_path)
    memory = tmp_path / "brain" / "memory"
    sources = {(kind, path.relative_to(brain).as_posix()) for kind, path in _iter_brain_source_files(memory)}

    assert ("root", "YOHAN-ECOSYSTEM-SOT.md") in sources
    assert ("root", "ASSETS.md") in sources
    assert ("state", "memory/soul.yaml") in sources
    assert ("state", "memory/active-project.yaml") in sources
    assert ("goal", "goals/10-active.md") in sources
    assert not any(path.endswith(("11-done.md", "12-cancelled.md", "13-archived.md")) for _kind, path in sources)
    assert ("goal", "goals/14-superseded.md") in sources
    assert ("adr", "docs/adr/ADR-001-fixture.md") in sources
    assert ("adr", "docs/adr/ADR-002-fixture.md") in sources
    assert ("adr", "docs/adr/ADR-003-fixture.md") not in sources
    assert ("specs", "docs/specs/spec.md") in sources
    assert ("troubleshooting", "docs/troubleshooting/fix.md") in sources
    assert ("rules", "memory/rules/rule.md") in sources
    assert ("logs", "memory/logs/sessions/session.md") in sources
    assert ("ingest", "memory/ingest/insights/insight.md") in sources
    assert not any(path.endswith("memory/ingest/url/raw.md") for _kind, path in sources)


async def test_corrupt_priority_source_fails_loud_then_recovers_after_refresh(tmp_path: Path):
    _brain(tmp_path)
    memory = tmp_path / "brain" / "memory"
    soul = memory / "soul.yaml"
    soul.write_text("identity: [unterminated", encoding="utf-8")
    adapter = MemoryAdapter(base_dir=memory)

    with pytest.raises(RuntimeError, match="memory_index_stale:priority_source_invalid"):
        await adapter.search("identity")

    soul.write_text("identity: restored\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="memory_index_stale:source_edited"):
        await adapter.search("restored")
    adapter.refresh_search_index()
    assert await adapter.search("restored")


async def test_refresh_swaps_one_atomic_generation_under_concurrent_search(tmp_path: Path):
    _brain(tmp_path)
    adapter = MemoryAdapter(base_dir=tmp_path / "brain" / "memory")
    errors: list[BaseException] = []

    def refresh_loop() -> None:
        try:
            for _ in range(25):
                adapter.refresh_search_index()
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    worker = threading.Thread(target=refresh_loop)
    worker.start()
    for _ in range(75):
        rows = await adapter.search("ASSET_GOLDEN location contract")
        assert rows and rows[0]["data"]["_path"] == "ASSETS.md"
    worker.join(timeout=10)
    assert not worker.is_alive()
    assert errors == []


def test_refresh_rejects_source_changed_during_build_and_keeps_active_generation(
    tmp_path: Path, monkeypatch
):
    _brain(tmp_path)
    memory = tmp_path / "brain" / "memory"
    source = memory / "wiki" / "noise.md"
    adapter = MemoryAdapter(base_dir=memory)
    active_revision = adapter._index_revision
    original_iter = adapter._iter_source_records

    def racing_iter(statuses=None, observed_stats=None):
        yield from original_iter(statuses, observed_stats)
        old = source.stat().st_mtime_ns
        source.write_text("changed while refresh was building", encoding="utf-8")
        os.utime(source, ns=(old + 1_000_000_000, old + 1_000_000_000))

    monkeypatch.setattr(adapter, "_iter_source_records", racing_iter)

    with pytest.raises(RuntimeError, match="memory_index_build_race:source_changed"):
        adapter.refresh_search_index()

    assert adapter._index_revision == active_revision


def test_overlapping_refresh_builds_are_serialized(tmp_path: Path, monkeypatch):
    _brain(tmp_path)
    adapter = MemoryAdapter(base_dir=tmp_path / "brain" / "memory")
    original_build = adapter._build_search_generation
    state_lock = threading.Lock()
    active = 0
    maximum = 0

    def observed_build():
        nonlocal active, maximum
        with state_lock:
            active += 1
            maximum = max(maximum, active)
        try:
            time.sleep(0.03)
            return original_build()
        finally:
            with state_lock:
                active -= 1

    monkeypatch.setattr(adapter, "_build_search_generation", observed_build)
    workers = [threading.Thread(target=adapter.refresh_search_index) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=5)

    assert all(not worker.is_alive() for worker in workers)
    assert maximum == 1


@pytest.mark.skipif(not os.getenv("YOHAN_BRAIN_GOLDEN_ROOT"), reason="cross-repo Brain fixture not configured")
async def test_real_brain_contract_golden_queries_return_all_required_evidence():
    brain = Path(os.environ["YOHAN_BRAIN_GOLDEN_ROOT"])
    contract = yaml.safe_load(
        (brain / "memory" / "core" / "retrieval-contract.yaml").read_text(encoding="utf-8")
    )
    adapter = MemoryAdapter(base_dir=brain / "memory")
    manifest = adapter._index_manifest
    assert set(manifest) == {
        "generation_id",
        "generated_at",
        "source_revision",
        "corpus_contract_version",
        "documents",
    }
    assert manifest["corpus_contract_version"] == contract["version"]
    assert len(manifest["generation_id"]) == len(manifest["source_revision"]) == 64
    assert manifest["documents"]
    assert len({item["document_id"] for item in manifest["documents"]}) == len(
        manifest["documents"]
    )
    for item in manifest["documents"]:
        assert item["document_id"] == f"brain:{item['path']}"
        assert not Path(item["path"]).is_absolute()
        assert len(item["content_hash"]) == 64
        raw = (brain / item["path"]).read_bytes()
        normalized = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        assert item["content_hash"] == hashlib.sha256(normalized).hexdigest()
    manifest_paths = {item["path"] for item in manifest["documents"]}
    vector_paths = {
        _rel_of(path, brain / "memory")
        for _kind, path in _iter_brain_source_files(brain / "memory")
    }
    assert vector_paths <= manifest_paths

    for golden in contract["golden_queries"]:
        rows = await adapter.search(
            golden["query"], {"exclude_evaluation_contract": True}
        )
        paths = {row["data"].get("_path") for row in rows[:5]}
        assert set(golden["required_evidence"]) <= paths, (golden["id"], paths)
        assert "memory/core/retrieval-contract.yaml" not in paths


def _tracked_hashes(repo: Path) -> dict[str, str]:
    raw = subprocess.run(
        ["git", "-c", f"safe.directory={repo.as_posix()}", "-C", str(repo), "ls-files", "-z"],
        check=True,
        capture_output=True,
    ).stdout
    paths = [item.decode("utf-8") for item in raw.split(b"\0") if item]
    return {
        rel: hashlib.sha256((repo / rel).read_bytes()).hexdigest()
        for rel in paths
        if (repo / rel).is_file()
    }


@pytest.mark.skipif(not os.getenv("YOHAN_BRAIN_GOLDEN_ROOT"), reason="cross-repo Brain fixture not configured")
async def test_real_brain_get_context_keeps_goldens_with_vector_and_is_read_only():
    brain = Path(os.environ["YOHAN_BRAIN_GOLDEN_ROOT"])
    contract = yaml.safe_load(
        (brain / "memory" / "core" / "retrieval-contract.yaml").read_text(encoding="utf-8")
    )
    before = _tracked_hashes(brain)
    memory = MemoryAdapter(base_dir=brain / "memory")
    qdrant = QdrantAdapter(embedder=HashEmbedder(), collection="yohan_resources")
    query_echo = " ".join(golden["query"] for golden in contract["golden_queries"])
    await qdrant.create(
        "resource",
        {"resource_id": "noise", "title": "query echo vector candidate", "text": query_echo},
    )
    adapters = {"memory": memory, "qdrant": qdrant}
    ctx = ToolContext(
        adapters,
        SmartRouter(adapters),
        SchemaValidator(),
        entity_catalog=load_project_catalog(brain / "memory"),
    )

    try:
        for golden in contract["golden_queries"]:
            env = await tool_get_context(
                ctx,
                golden["query"],
                {
                    "top_k": 5,
                    "context_supplemental": False,
                    "per_page_cap": 0,
                    "skip_roster": True,
                    "exclude_evaluation_contract": True,
                },
            )
            paths = {row["data"].get("_path") for row in env["data"]["matches"]}
            assert set(golden["required_evidence"]) <= paths, (golden["id"], paths)
            diagnostics = env["data"]["retrieval_diagnostics"]
            assert "yohan_resources" in diagnostics["collections"]["available"]
    finally:
        await qdrant.aclose()

    assert _tracked_hashes(brain) == before


@pytest.mark.skipif(not os.getenv("YOHAN_BRAIN_GOLDEN_ROOT"), reason="cross-repo Brain fixture not configured")
async def test_retrieval_contract_is_searchable_outside_evaluation_mode():
    brain = Path(os.environ["YOHAN_BRAIN_GOLDEN_ROOT"])
    adapter = MemoryAdapter(base_dir=brain / "memory")

    rows = await adapter.search("brain retrieval contract golden queries")
    assert rows[0]["data"]["_path"] == "memory/core/retrieval-contract.yaml"
    excluded = await adapter.search(
        "brain retrieval contract golden queries",
        {"exclude_evaluation_contract": True},
    )
    assert all(
        row["data"].get("_path") != "memory/core/retrieval-contract.yaml"
        for row in excluded
    )


def test_vector_embedding_text_rejects_secrets_and_redacts_local_paths():
    secrets = [
        "github_" + "pat_" + "A" * 82,
        "secret_" + "B" * 40,
        "sk-" + "proj-" + "C" * 24,
        "api_" + "key=" + "D" * 24,
        "Authorization: Bearer " + "E" * 24,
        "password: " + "F" * 24,
        "credential=" + "G" * 24,
    ]
    for secret in secrets:
        with pytest.raises(RuntimeError, match="brain_vector_secret_detected"):
            _safe_embedding_text(secret, rel="memory/soul.yaml")

    local_paths = [
        "C:\\Users\\user\\private\\repo",
        "\\\\server\\share\\private",
        "/tmp/private/repo",
        "/root/private/repo",
    ]
    for local_path in local_paths:
        safe = _safe_embedding_text(
            f"workspace: {local_path}",
            rel="memory/active-project.yaml",
        )
        assert local_path not in safe
        assert "[LOCAL_PATH]" in safe

    for secret in secrets:
        assert secret not in _safe_error_detail(RuntimeError(secret))
    for local_path in local_paths:
        assert local_path not in _safe_error_detail(RuntimeError(local_path))


@pytest.mark.parametrize("body", ["- item\n", "scalar\n", "null\n"])
def test_vector_priority_yaml_requires_mapping(body: str):
    with pytest.raises(RuntimeError, match="brain_vector_invalid_priority_yaml"):
        _validate_yaml_for_embedding(body, kind="core", rel="memory/core/example.yaml")

    assert _validate_yaml_for_embedding("schema: fixture\n", kind="core", rel="x") == {
        "schema": "fixture"
    }


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


def test_qdrant_environment_selects_exactly_one_mode(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("QDRANT_URL", raising=False)
    monkeypatch.setenv("QDRANT_PATH", str(tmp_path / "qdrant"))
    path_only = QdrantAdapter()
    assert path_only.path == str(tmp_path / "qdrant") and not path_only.url

    monkeypatch.delenv("QDRANT_PATH", raising=False)
    monkeypatch.setenv("QDRANT_URL", "http://127.0.0.1:6333")
    url_only = QdrantAdapter()
    assert url_only.url == "http://127.0.0.1:6333" and not url_only.path

    monkeypatch.setenv("QDRANT_PATH", str(tmp_path / "qdrant"))
    with pytest.raises(ValueError, match="qdrant_config_conflict:path_and_url"):
        QdrantAdapter()


async def test_qdrant_backend_down_reports_every_collection_unavailable():
    class DownClient:
        async def query_points(self, *_args, **_kwargs):
            fake_token = "github_" + "pat_" + "A" * 82
            password_label = "pass" + "word:"
            fake_password = password_label + " " + "B" * 24
            fake_credential = "credential=" + "C" * 24
            raise ConnectionError(
                f"C:\\Users\\user\\private\\qdrant {fake_token} "
                f"{fake_password} {fake_credential}"
            )

    adapter = QdrantAdapter(client=DownClient(), embedder=HashEmbedder())
    diagnostics: dict = {}
    with pytest.raises(RuntimeError, match="전부 회수 실패"):
        await adapter.search("evidence", {"_search_diagnostics": diagnostics})
    assert diagnostics["collections_attempted"] == len(adapter.search_collections)
    assert len(diagnostics["unavailable_collections"]) == len(adapter.search_collections)
    assert {item["reason_code"] for item in diagnostics["unavailable_collections"]} == {
        "collection_query_failed"
    }
    assert all("[LOCAL_PATH]" in item["detail"] for item in diagnostics["unavailable_collections"])
    assert all("github_pat_" not in item["detail"] for item in diagnostics["unavailable_collections"])
    assert all(("pass" + "word:") not in item["detail"] for item in diagnostics["unavailable_collections"])
    assert all("credential=" not in item["detail"] for item in diagnostics["unavailable_collections"])


def test_local_mcp_memory_is_not_promoted_to_brain_root(tmp_path: Path, monkeypatch):
    local_repo = tmp_path / "yohan-mcp"
    local_memory = local_repo / "memory"
    local_memory.mkdir(parents=True)
    monkeypatch.setattr(path_contract, "ROOT", local_repo)

    assert path_contract.resolve_brain_root(local_memory) is None
