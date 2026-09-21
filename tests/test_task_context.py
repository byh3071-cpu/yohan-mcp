"""P1 task-scoped get_context projection."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.context_resolver import RetrievalDiagnostics, load_project_catalog, resolve_entities
from core.task_context import _PACKET_CHAR_BUDGET, build_task_context
from core.schema_validator import SchemaValidator
from core.tools import ToolContext, tool_get_context


class _Router:
    def __init__(self, records: list[dict], *, fresh: bool | None = True, reason: str | None = "fresh") -> None:
        self.records = records
        self.fresh = fresh
        self.reason = reason

    async def search(self, query: str, opts: dict | None = None) -> dict:
        return {
            "results": self.records,
            "errors": {},
            "sources_used": ["memory"],
            "diagnostics": {
                "backend_timings_ms": {"memory": 1},
                "backend_details": {"memory": {
                    "index_revision": "index-rev",
                    "index_generation_id": "generation",
                    "corpus_contract_version": "r1",
                    "fresh": self.fresh,
                    "freshness_reason_code": self.reason,
                }},
            },
        }


def _record(id_: str, type_: str, data: dict) -> dict:
    return {"id": id_, "type": type_, "backend": "memory", "sources": ["memory"], "data": data}


@pytest.fixture
def brain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "brain"
    (root / "memory").mkdir(parents=True)
    (root / "YOHAN-ECOSYSTEM-SOT.md").write_text("# fixture", encoding="utf-8")
    manifest = root / "ops" / "public-dev-bootstrap"
    manifest.mkdir(parents=True)
    (manifest / "repos.json").write_text(json.dumps({
        "schema": "dev-repo-registry",
        "repos": [
            {"name": "muse", "group": "products", "url": "https://example.test/muse.git", "aliases": ["Muse", "yohan-log"]},
            {"name": "other", "group": "products", "url": "https://example.test/other.git"},
            {"name": "seloa", "group": "products", "url": "https://example.test/seloa.git", "aliases": ["Seloa"]},
            {"name": "skills", "group": "products", "url": "https://example.test/skills.git"},
        ],
    }), encoding="utf-8")
    monkeypatch.setenv("YOHAN_BRAIN_ROOT", str(root))
    monkeypatch.delenv("MEMORY_DIR", raising=False)
    return root


def _ctx(records: list[dict], **router_kwargs) -> ToolContext:
    catalog = {"muse": ("muse", "Muse"), "other": ("other",), "seloa": ("seloa", "Seloa"), "skills": ("skills",)}
    return ToolContext({}, _Router(records, **router_kwargs), SchemaValidator(), entity_catalog=catalog)


@pytest.mark.asyncio
async def test_muse_resume_query_returns_task_context_golden(brain: Path):
    env = await tool_get_context(_ctx([
        _record("goal-muse", "brain:goal", {"project": "muse", "title": "Muse launch", "status": "ACTIVE", "next_action": "Ship the onboarding flow", "body": "full document must not escape" * 80, "_path": "goals/muse.md"}),
        _record("adr-muse", "brain:adr", {"repository": "Muse", "title": "Muse ADR", "status": "accepted", "_path": "docs/adr/muse.md"}),
        _record("d-muse", "brain:decisions", {"repo": "muse", "title": "Muse decision", "_path": "memory/decisions/muse.md"}),
    ]), "Muse 이어서 해", {"context_supplemental": False})

    packet = env["data"]["task_context"]
    assert packet["schema"] == "task-context/v1"
    assert packet["status"] == "complete"
    assert packet["repository"] == {"id": "muse", "name": "muse", "canonical_path": "products/muse", "remote": "https://example.test/muse.git"}
    assert packet["goals"][0]["id"] == "goal-muse"
    assert packet["next_actions"] == ["Ship the onboarding flow"]
    assert len(packet["goals"][0]["snippet"]) <= 280


@pytest.mark.asyncio
async def test_task_scope_is_opt_in_for_non_resume_query(brain: Path):
    records = [_record("goal", "brain:goal", {"project": "muse", "status": "ACTIVE", "next_action": "next", "_path": "goals/muse.md"})]
    plain = await tool_get_context(_ctx(records), "Muse status", {"context_supplemental": False})
    scoped = await tool_get_context(_ctx(records), "Muse status", {"task_scope": True, "context_supplemental": False})
    assert "task_context" not in plain["data"]
    assert scoped["data"]["task_context"]["repository"]["id"] == "muse"


@pytest.mark.asyncio
async def test_other_repository_contamination_is_excluded(brain: Path):
    env = await tool_get_context(_ctx([
        _record("goal", "brain:goal", {"project": "muse", "status": "ACTIVE", "next_action": "next", "_path": "goals/muse.md"}),
        _record("other-decision", "brain:decisions", {"project": "other", "title": "other secret", "_path": "memory/decisions/other.md"}),
    ]), "Muse", {"task_scope": True, "context_supplemental": False})
    packet = env["data"]["task_context"]
    assert "other_repository_evidence_excluded" in packet["reason_codes"]
    assert [item["id"] for item in packet["decisions"]] == []
    assert all(ref["id"] != "other-decision" for ref in packet["evidence_refs"])


@pytest.mark.asyncio
async def test_unregistered_repository_is_explicit(brain: Path):
    ctx = ToolContext({}, _Router([]), SchemaValidator(), entity_catalog={"unknown": ("unknown",)})
    env = await tool_get_context(ctx, "unknown", {"task_scope": True, "context_supplemental": False})
    packet = env["data"]["task_context"]
    assert packet["status"] == "unresolved"
    assert packet["reason_codes"] == ["repository_not_registered"]


@pytest.mark.asyncio
async def test_missing_goal_and_next_action_are_explicit(brain: Path):
    env = await tool_get_context(_ctx([
        _record("adr", "brain:adr", {"project": "muse", "title": "ADR only", "_path": "docs/adr/muse.md"}),
    ]), "Muse", {"task_scope": True, "context_supplemental": False})
    packet = env["data"]["task_context"]
    assert packet["status"] == "partial"
    assert {"missing_project_goal", "missing_next_action"} <= set(packet["reason_codes"])


@pytest.mark.asyncio
async def test_stale_index_freshness_is_propagated(brain: Path):
    env = await tool_get_context(_ctx([
        _record("goal", "brain:goal", {"project": "muse", "status": "ACTIVE", "next_action": "next", "_path": "goals/muse.md"}),
    ], fresh=False, reason="source_edited"), "Muse", {"task_scope": True, "context_supplemental": False})
    packet = env["data"]["task_context"]
    assert packet["freshness"]["index"]["fresh"] is False
    assert "stale_index:source_edited" in packet["reason_codes"]


@pytest.mark.asyncio
async def test_conflicting_recognized_repository_evidence_fails_loud(brain: Path):
    env = await tool_get_context(_ctx([
        _record("both", "brain:decisions", {"project": ["muse", "other"], "_path": "memory/decisions/muse.md"}),
    ]), "Muse", {"task_scope": True, "context_supplemental": False})
    packet = env["data"]["task_context"]
    assert packet["status"] == "conflict"
    assert "conflicting_structured_repository_identity" in packet["reason_codes"]
    assert packet["evidence_refs"] == []


def _diagnostics() -> RetrievalDiagnostics:
    return RetrievalDiagnostics.from_retrieval(
        query="Muse", matches=[], entities=[], graph_edges=[], sources_used=[], errors={}, diagnostics={},
        supplemental_searches=0, char_budget=1, max_matches=1,
    )


def test_runtime_source_revision_shape_and_safe_fallback(monkeypatch: pytest.MonkeyPatch):
    from core import context_resolver as resolver

    monkeypatch.setattr(resolver, "RETRIEVAL_RUNTIME_SOURCE_REVISION", "a" * 40)
    monkeypatch.setattr(resolver, "RETRIEVAL_RUNTIME_SOURCE_REVISION_REASON", "available")
    runtime = _diagnostics().as_dict()["runtime"]
    assert runtime["source_revision"] == "a" * 40
    assert runtime["source_revision_reason_code"] == "available"

    monkeypatch.setattr(resolver, "RETRIEVAL_RUNTIME_SOURCE_REVISION", None)
    monkeypatch.setattr(resolver, "RETRIEVAL_RUNTIME_SOURCE_REVISION_REASON", "git_metadata_unavailable")
    runtime = _diagnostics().as_dict()["runtime"]
    assert runtime["source_revision"] is None
    assert runtime["source_revision_reason_code"] == "git_metadata_unavailable"


@pytest.mark.asyncio
async def test_shared_path_evidence_is_not_a_packet_conflict(brain: Path):
    env = await tool_get_context(_ctx([
        _record("goal", "brain:goal", {"project": "muse", "status": "ACTIVE", "next_action": "ship", "_path": "goals/muse.md"}),
        _record("joint", "brain:decisions", {"title": "Seloa Muse decision", "_path": "memory/decisions/seloa-muse.md"}),
    ]), "Muse", {"task_scope": True, "context_supplemental": False})
    packet = env["data"]["task_context"]
    assert packet["status"] == "complete"
    assert "shared_repository_evidence_included" in packet["reason_codes"]
    assert [item["id"] for item in packet["decisions"]] == ["joint"]


@pytest.mark.asyncio
async def test_body_repo_mentions_and_generic_skills_alias_do_not_bind(brain: Path):
    env = await tool_get_context(_ctx([
        _record("goal", "brain:goal", {"project": "muse", "status": "ACTIVE", "next_action": "ship", "_path": "goals/muse.md"}),
        _record("muse-decision", "brain:decisions", {"project": "muse", "body": "The skills repository was considered.", "_path": "memory/decisions/muse.md"}),
        _record("body-only", "brain:decisions", {"body": "Muse and skills are named here only."}),
    ]), "Muse", {"task_scope": True, "context_supplemental": False})
    packet = env["data"]["task_context"]
    assert packet["status"] == "complete"
    assert [item["id"] for item in packet["decisions"]] == ["muse-decision"]


@pytest.mark.asyncio
async def test_only_selected_active_goal_can_supply_actions(brain: Path):
    env = await tool_get_context(_ctx([
        _record("active", "brain:goal", {"project": "muse", "status": "ACTIVE", "_path": "goals/muse-active.md"}),
        _record("backlog", "brain:goal", {"project": "muse", "status": "BACKLOG", "next_action": "must not leak", "_path": "goals/muse-backlog.md"}),
    ]), "Muse", {"task_scope": True, "context_supplemental": False})
    packet = env["data"]["task_context"]
    assert packet["status"] == "partial"
    assert packet["goals"][0]["id"] == "active"
    assert packet["next_actions"] == []
    assert "missing_next_action" in packet["reason_codes"]


@pytest.mark.asyncio
async def test_multiple_active_goals_are_conflict_and_linked_decision_is_only_allowed_source(brain: Path):
    conflict = await tool_get_context(_ctx([
        _record("a", "brain:goal", {"project": "muse", "status": "ACTIVE", "next_action": "a", "_path": "goals/a-muse.md"}),
        _record("b", "brain:goal", {"project": "muse", "status": "IN_PROGRESS", "next_action": "b", "_path": "goals/b-muse.md"}),
    ]), "Muse", {"task_scope": True, "context_supplemental": False})
    assert conflict["data"]["task_context"]["status"] == "conflict"
    assert "multiple_active_project_goals" in conflict["data"]["task_context"]["reason_codes"]

    linked = await tool_get_context(_ctx([
        _record("a", "brain:goal", {"project": "muse", "status": "ACTIVE", "_path": "goals/a-muse.md"}),
        _record("d", "brain:decisions", {"project": "muse", "goal_id": "a", "next_action": "linked action", "_path": "memory/decisions/muse.md"}),
    ]), "Muse", {"task_scope": True, "context_supplemental": False})
    assert linked["data"]["task_context"]["status"] == "complete"
    assert linked["data"]["task_context"]["next_actions"] == ["linked action"]


def test_manifest_alias_catalog_and_manifest_fail_loud_cases(brain: Path):
    catalog = load_project_catalog(brain / "memory")
    assert "muse" in catalog
    assert resolve_entities("yohan-log", catalog)[0].canonical == "muse"
    assert "ops/public-dev-bootstrap/repos.json" in catalog.diagnostics["source"]
    first_revision = catalog.diagnostics["revision"]

    manifest = brain / "ops" / "public-dev-bootstrap" / "repos.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["repos"][0]["aliases"].append("muse-new-alias")
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    assert load_project_catalog(brain / "memory").diagnostics["revision"] != first_revision

    manifest.write_text(json.dumps({"schema": "dev-repo-registry", "repos": [
        {"name": "muse", "group": "products", "local_path": "C:/unsafe"},
    ]}), encoding="utf-8")
    invalid = build_task_context(query="Muse", opts={}, matches=[], entities=[{"canonical": "muse", "kind": "project"}], catalog={"muse": ("muse",)}, retrieval_diagnostics={}, brain_root=brain)
    assert invalid["status"] == "unresolved"
    assert invalid["reason_codes"] == ["repository_manifest_entry_invalid"]

    manifest.write_text(json.dumps({"schema": "dev-repo-registry", "repos": [
        {"name": "muse", "group": "products", "aliases": ["shared"]},
        {"name": "other", "group": "products", "aliases": ["shared"]},
    ]}), encoding="utf-8")
    conflict = build_task_context(query="shared", opts={}, matches=[], entities=[{"canonical": "shared", "kind": "project"}], catalog={"shared": ("shared",)}, retrieval_diagnostics={}, brain_root=brain)
    assert conflict["status"] == "unresolved"
    assert conflict["reason_codes"] == ["repository_manifest_conflict"]


@pytest.mark.asyncio
async def test_manifest_alias_opts_project_resolves_to_muse_task_context(brain: Path):
    catalog = load_project_catalog(brain / "memory")
    ctx = ToolContext({}, _Router([
        _record("muse-goal", "brain:goal", {"project": "muse", "status": "ACTIVE", "next_action": "ship", "_path": "goals/muse.md"}),
    ]), SchemaValidator(), entity_catalog=catalog)
    env = await tool_get_context(ctx, "continue", {"project": "yohan-log", "task_scope": True, "context_supplemental": False})
    packet = env["data"]["task_context"]
    assert packet["repository"]["id"] == "muse"
    assert packet["status"] == "complete"


@pytest.mark.parametrize("payload", [
    [],
    {"schema": "dev-repo-registry", "repos": [{"name": "muse", "group": "products", "local_path": r"\\\\server\\share"}]},
    {"schema": "dev-repo-registry", "repos": [{"name": "muse", "group": "products", "local_path": "/rooted/repo"}]},
])
def test_malformed_or_rooted_manifest_fails_loud(brain: Path, payload: object):
    manifest = brain / "ops" / "public-dev-bootstrap" / "repos.json"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    packet = build_task_context(query="Muse", opts={}, matches=[], entities=[{"canonical": "muse", "kind": "project"}], catalog={"muse": ("muse",)}, retrieval_diagnostics={}, brain_root=brain)
    assert packet["status"] == "unresolved"
    assert packet["reason_codes"] in (["repository_manifest_invalid"], ["repository_manifest_entry_invalid"])


def test_duplicate_canonical_manifest_fails_catalog_and_packet_consistently(brain: Path):
    manifest = brain / "ops" / "public-dev-bootstrap" / "repos.json"
    manifest.write_text(json.dumps({"schema": "dev-repo-registry", "repos": [
        {"name": "muse", "group": "products", "aliases": ["first"]},
        {"name": "Muse", "group": "products", "aliases": ["second"]},
    ]}), encoding="utf-8")

    catalog = load_project_catalog(brain / "memory")
    assert catalog.diagnostics["degraded"] is True
    assert catalog.diagnostics["manifest_reason_code"] == "repository_manifest_duplicate_canonical"
    assert catalog.diagnostics["source_states"]["repository_manifest"] == "conflict"

    packet = build_task_context(
        query="Muse",
        opts={},
        matches=[],
        entities=[{"canonical": "muse", "kind": "project"}],
        catalog={"muse": ("muse",)},
        retrieval_diagnostics={},
        brain_root=brain,
    )
    assert packet["status"] == "unresolved"
    assert packet["reason_codes"] == ["repository_manifest_duplicate_canonical"]


@pytest.mark.asyncio
async def test_packet_output_is_bounded(brain: Path):
    huge = "x" * 20_000
    records = [_record(huge, "brain:goal", {"project": "muse", "status": "ACTIVE", "title": huge, "next_action": huge, "_path": "goals/" + huge})]
    records.extend(_record(f"adr-{index}-{huge}", "brain:adr", {"project": "muse", "title": huge, "_path": "docs/" + huge}) for index in range(8))
    packet = (await tool_get_context(_ctx(records[:1]), "Muse", {"task_scope": True, "top_k": 1, "context_char_budget": 100000, "context_supplemental": False}))["data"]["task_context"]

    def chars(value) -> int:
        if isinstance(value, str):
            return len(value)
        if isinstance(value, dict):
            return sum(chars(item) for item in value.values())
        if isinstance(value, list):
            return sum(chars(item) for item in value)
        return 0

    assert chars(packet) <= _PACKET_CHAR_BUDGET
    assert len(packet["goals"][0]["id"]) <= 128
    assert len(packet["goals"][0]["source_ref"]["locator"]) <= 240
    assert len(packet["next_actions"][0]) <= 280
