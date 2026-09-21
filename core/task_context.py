"""Bounded project task projection for ``get_context``.

The projection consumes the normal resolver output. It never searches again,
and does not use rank as project provenance.
"""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path, PurePosixPath
from typing import Iterable

from core.paths import resolve_brain_root, resolve_memory_dir

SCHEMA = "task-context/v1"
_RESUME_QUERY_RE = re.compile(r"^\s*.+?\s+이어(?:서)?\s+해\s*$")
_ACTIVE_GOAL_STATUSES = {"ACTIVE", "IN_PROGRESS"}
_MAX_ITEMS = {"adrs": 4, "decisions": 4, "evidence_refs": 6, "blockers": 4, "next_actions": 4}
_PROJECT_METADATA_KEYS = (
    "project", "project_id", "project_slug", "repository", "repository_id",
    "repo", "repo_id", "canonical_repository", "canonical_repo",
)
_GOAL_LINK_KEYS = ("goal_id", "related_goal_id", "parent_goal_id")
_TEXT_LIMIT = 280
_ID_LIMIT = 128
_LOCATOR_LIMIT = 240
_ACTION_LIMIT = 280
_PACKET_CHAR_BUDGET = 8_000


def _normal(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(char for char in text if char.isalnum())


def _bounded(value: object, limit: int) -> str | None:
    if value is None:
        return None
    return " ".join(str(value).split())[:limit]


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _strings(item)]
    if isinstance(value, (list, tuple, set)):
        return [text for item in value for text in _strings(item)]
    return []


def _contains_alias(value: str, alias: str) -> bool:
    """Recognize an explicit alias in a path or a title/name fallback."""
    text = unicodedata.normalize("NFKC", value or "").casefold()
    candidate = unicodedata.normalize("NFKC", alias or "").casefold().strip()
    if not candidate:
        return False
    if candidate.isascii():
        pieces = [re.escape(piece) for piece in re.split(r"[-_\s]+", candidate) if piece]
        return bool(pieces) and re.search(
            r"(?<![a-z0-9])" + r"[-_\s]+".join(pieces) + r"(?![a-z0-9])", text
        ) is not None
    return candidate in text


def is_resume_query(query: str) -> bool:
    return bool(_RESUME_QUERY_RE.fullmatch(query or ""))


def _safe_repo_path(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    raw = value.strip()
    # Reject rooted, drive-qualified and UNC forms before normalization.
    if not raw or raw.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", raw):
        return None
    normalized = raw.replace("\\", "/")
    try:
        path = PurePosixPath(normalized)
    except TypeError:
        return None
    if not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        return None
    return path.as_posix()


def _valid_repo_name(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    name = value.strip()
    if not name or name in {".", ".."} or any(char in name for char in "/\\\x00"):
        return None
    return name


def _manifest_entries(brain_root: Path | None) -> tuple[list[dict] | None, dict]:
    source = {"source": "ops/public-dev-bootstrap/repos.json", "status": "unavailable", "reason_code": "brain_root_unavailable"}
    if brain_root is None:
        return None, source
    manifest = brain_root / "ops" / "public-dev-bootstrap" / "repos.json"
    if not manifest.is_file():
        source.update(status="missing", reason_code="repository_manifest_missing")
        return None, source
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        source.update(status="invalid", reason_code="repository_manifest_invalid")
        return None, source
    if not isinstance(payload, dict) or payload.get("schema") != "dev-repo-registry":
        source.update(status="invalid", reason_code="repository_manifest_invalid")
        return None, source
    repos = payload.get("repos")
    if not isinstance(repos, list):
        source.update(status="invalid", reason_code="repository_manifest_invalid")
        return None, source
    entries: list[dict] = []
    canonical_keys: set[str] = set()
    for item in repos:
        if not isinstance(item, dict) or _valid_repo_name(item.get("name")) is None:
            source.update(status="invalid", reason_code="repository_manifest_entry_invalid")
            return None, source
        canonical_key = _normal(item["name"])
        if not canonical_key:
            source.update(status="invalid", reason_code="repository_manifest_entry_invalid")
            return None, source
        if canonical_key in canonical_keys:
            source.update(status="conflict", reason_code="repository_manifest_duplicate_canonical")
            return None, source
        canonical_keys.add(canonical_key)
        aliases = item.get("aliases", [])
        if not isinstance(aliases, list) or any(not isinstance(alias, str) or not alias.strip() for alias in aliases):
            source.update(status="invalid", reason_code="repository_manifest_entry_invalid")
            return None, source
        if "group" in item and not isinstance(item["group"], str):
            source.update(status="invalid", reason_code="repository_manifest_entry_invalid")
            return None, source
        if "local_path" in item and not isinstance(item["local_path"], str):
            source.update(status="invalid", reason_code="repository_manifest_entry_invalid")
            return None, source
        if "url" in item and item["url"] is not None and not isinstance(item["url"], str):
            source.update(status="invalid", reason_code="repository_manifest_entry_invalid")
            return None, source
        if item.get("local_path") is not None and _safe_repo_path(item["local_path"]) is None:
            source.update(status="invalid", reason_code="repository_manifest_entry_invalid")
            return None, source
        if item.get("local_path") is None and _valid_repo_name(item.get("group")) is None:
            source.update(status="invalid", reason_code="repository_manifest_entry_invalid")
            return None, source
        entries.append(item)
    source.update(status="loaded", reason_code="loaded")
    return entries, source


def load_manifest_aliases(brain_root: Path | None) -> tuple[dict[str, tuple[str, ...]], dict]:
    """Return strict canonical→aliases from the versioned Brain mirror."""
    entries, source = _manifest_entries(brain_root)
    if entries is None:
        return {}, source
    aliases: dict[str, tuple[str, ...]] = {}
    owners: dict[str, str] = {}
    for item in entries:
        canonical = item["name"].strip()
        names = tuple(dict.fromkeys((canonical, *(alias.strip() for alias in item.get("aliases", [])))))
        for name in names:
            key = _normal(name)
            if key in owners and owners[key] != canonical:
                source.update(status="invalid", reason_code="repository_manifest_alias_conflict")
                return {}, source
            owners[key] = canonical
        aliases[canonical] = names
    return aliases, source


def _manifest_repository(project: str, brain_root: Path | None) -> tuple[dict | None, dict]:
    entries, source = _manifest_entries(brain_root)
    if entries is None:
        return None, source
    target = _normal(project)
    matches = [item for item in entries if target in {_normal(name) for name in (item["name"], *item.get("aliases", []))}]
    if not matches:
        source.update(status="unregistered", reason_code="repository_not_registered")
        return None, source
    if len(matches) != 1:
        source.update(status="conflict", reason_code="repository_manifest_conflict")
        return None, source
    item = matches[0]
    location = item.get("local_path") or "/".join((str(item.get("group") or ""), item["name"]))
    canonical_path = _safe_repo_path(location)
    if canonical_path is None:
        source.update(status="invalid", reason_code="repository_manifest_entry_invalid")
        return None, source
    return {"id": _bounded(item["name"], _ID_LIMIT), "name": _bounded(item["name"], _ID_LIMIT), "canonical_path": _bounded(canonical_path, _LOCATOR_LIMIT), "remote": _bounded(item.get("url"), _LOCATOR_LIMIT)}, source


def _catalog_aliases(canonical: str, catalog: dict[str, tuple[str, ...]]) -> tuple[str, ...]:
    return tuple(dict.fromkeys((canonical, *(catalog.get(canonical) or ()))))


def _exact_metadata_bindings(data: dict, catalog: dict[str, tuple[str, ...]]) -> set[str]:
    values = [value for key in _PROJECT_METADATA_KEYS for value in _strings(data.get(key))]
    return {canonical for canonical in catalog if any(_normal(value) in {_normal(alias) for alias in _catalog_aliases(canonical, catalog)} for value in values if _normal(value))}


def _record_binding(record: dict, catalog: dict[str, tuple[str, ...]]) -> tuple[set[str], str]:
    """Binding precedence: structured exact metadata → path → title/name."""
    data = record.get("data") if isinstance(record.get("data"), dict) else {}
    structured = _exact_metadata_bindings(data, catalog)
    if structured:
        return structured, "structured"
    paths = _strings(data.get("_path")) + _strings(data.get("path"))
    titles = _strings(data.get("title")) + _strings(data.get("name"))
    for values, provenance in ((paths, "path"), (titles, "title")):
        found = {canonical for canonical in catalog if any(_contains_alias(value, alias) for value in values for alias in _catalog_aliases(canonical, catalog))}
        if found:
            return found, provenance
    return set(), "none"


def _source_ref(record: dict) -> dict:
    data = record.get("data") if isinstance(record.get("data"), dict) else {}
    evidence = record.get("evidence_ref") if isinstance(record.get("evidence_ref"), dict) else {}
    return {"type": _bounded(record.get("type"), _ID_LIMIT), "id": _bounded(record.get("id"), _ID_LIMIT), "backend": _bounded(record.get("backend"), _ID_LIMIT), "locator": _bounded(data.get("_path") or data.get("path") or evidence.get("locator"), _LOCATOR_LIMIT), "document_id": _bounded(evidence.get("document_id") or data.get("_retrieval_document_id"), _ID_LIMIT), "content_hash": _bounded(evidence.get("content_hash") or data.get("_retrieval_content_hash"), _ID_LIMIT)}


def _snippet(record: dict) -> str | None:
    data = record.get("data") if isinstance(record.get("data"), dict) else {}
    for key in ("summary", "title", "name", "description"):
        if isinstance(data.get(key), str) and data[key].strip():
            return _bounded(data[key], _TEXT_LIMIT)
    return None


def _item(record: dict) -> dict:
    data = record.get("data") if isinstance(record.get("data"), dict) else {}
    return {"id": _bounded(record.get("id"), _ID_LIMIT), "type": _bounded(record.get("type"), _ID_LIMIT), "title": _bounded(data.get("title") or data.get("name"), _TEXT_LIMIT), "status": _bounded(data.get("status"), _ID_LIMIT), "snippet": _snippet(record), "source_ref": _source_ref(record)}


def _explicit_values(record: dict, keys: Iterable[str], labels: Iterable[str]) -> list[str]:
    data = record.get("data") if isinstance(record.get("data"), dict) else {}
    values = [_bounded(value, _ACTION_LIMIT) for key in keys for value in _strings(data.get(key))]
    body = data.get("body") or data.get("text") or data.get("content")
    if isinstance(body, str):
        pattern = "|".join(re.escape(label) for label in labels)
        values.extend(_bounded(match.group(1), _ACTION_LIMIT) for match in re.finditer(rf"(?im)^\s*(?:{pattern})\s*[:：-]\s*(.+?)\s*$", body))
    return list(dict.fromkeys(value for value in values if value))


def _decision_links_goal(record: dict, goal_id: object) -> bool:
    data = record.get("data") if isinstance(record.get("data"), dict) else {}
    expected = _normal(goal_id)
    return bool(expected) and any(_normal(value) == expected for key in _GOAL_LINK_KEYS for value in _strings(data.get(key)))


def _apply_total_budget(value, remaining: list[int]):
    if isinstance(value, str):
        text = value[:max(0, remaining[0])]
        remaining[0] -= len(text)
        return text
    if isinstance(value, list):
        return [_apply_total_budget(item, remaining) for item in value]
    if isinstance(value, dict):
        return {key: _apply_total_budget(item, remaining) for key, item in value.items()}
    return value


def _packet(packet: dict) -> dict:
    return _apply_total_budget(packet, [_PACKET_CHAR_BUDGET])


def _index_ref(index: object) -> dict:
    value = index if isinstance(index, dict) else {}
    return {
        "revision": _bounded(value.get("revision"), _ID_LIMIT),
        "generation_id": _bounded(value.get("generation_id"), _ID_LIMIT),
        "observed_at": _bounded(value.get("observed_at"), _ID_LIMIT),
        "fresh": value.get("fresh") if isinstance(value.get("fresh"), bool) else None,
        "reason_code": _bounded(value.get("reason_code"), _ID_LIMIT),
    }


def _manifest_ref(manifest: dict) -> dict:
    return {
        "source": _bounded(manifest.get("source"), _LOCATOR_LIMIT),
        "status": _bounded(manifest.get("status"), _ID_LIMIT),
        "reason_code": _bounded(manifest.get("reason_code"), _ID_LIMIT),
    }


def _empty(status: str, reason_codes: list[str], diagnostics: dict, manifest: dict) -> dict:
    return _packet({"schema": SCHEMA, "status": status, "repository": None, "reason_codes": reason_codes, "goals": [], "adrs": [], "decisions": [], "evidence_refs": [], "blockers": [], "next_actions": [], "freshness": {"index": _index_ref(diagnostics.get("index")), "sources": []}, "source_refs": {"repository_manifest": _manifest_ref(manifest)}})


def build_task_context(*, query: str, opts: dict, matches: list[dict], entities: list[dict], catalog: dict[str, tuple[str, ...]], retrieval_diagnostics: dict, brain_root: Path | None = None) -> dict:
    """Return a fail-loud, bounded projection from an existing context result."""
    projects = list(dict.fromkeys(str(entity.get("canonical")) for entity in entities if entity.get("kind") == "project" and entity.get("canonical")))
    explicit = str(opts.get("project") or "").strip()
    if not projects and explicit:
        projects = [canonical for canonical, aliases in catalog.items() if _normal(explicit) in {_normal(value) for value in (canonical, *aliases)}][:1]
    root = brain_root if brain_root is not None else resolve_brain_root(resolve_memory_dir())
    if len(projects) != 1:
        reason = "multiple_project_entities_recognized" if len(projects) > 1 else "repository_not_recognized"
        return _empty("conflict" if len(projects) > 1 else "unresolved", [reason], retrieval_diagnostics, {"source": "ops/public-dev-bootstrap/repos.json", "status": "not_checked", "reason_code": reason})
    project = projects[0]
    repository, manifest_source = _manifest_repository(project, root)
    if repository is None:
        return _empty("unresolved", [manifest_source["reason_code"]], retrieval_diagnostics, manifest_source)

    bound: list[dict] = []
    reason_codes: list[str] = []
    structured_conflict = False
    for record in matches:
        bindings, provenance = _record_binding(record, catalog)
        if project not in bindings:
            if bindings:
                reason_codes.append("other_repository_evidence_excluded")
            continue
        if provenance == "structured" and len(bindings) > 1:
            structured_conflict = True
            continue
        if len(bindings) > 1:
            reason_codes.append("shared_repository_evidence_included")
        bound.append(record)
    if structured_conflict:
        reason_codes.append("conflicting_structured_repository_identity")

    active_goals = [record for record in bound if record.get("type") in {"brain:goal", "goal"} and str((record.get("data") or {}).get("status") or "").upper() in _ACTIVE_GOAL_STATUSES]
    selected_goal = active_goals[0] if len(active_goals) == 1 else None
    if not active_goals:
        reason_codes.append("missing_project_goal")
    elif len(active_goals) > 1:
        reason_codes.append("multiple_active_project_goals")
    adrs = [record for record in bound if record.get("type") in {"brain:adr", "adr"}]
    decisions = [record for record in bound if record.get("type") in {"brain:decisions", "decision"}]
    blockers: list[str] = []
    next_actions: list[str] = []
    if selected_goal is not None:
        action_records = [selected_goal, *[record for record in decisions if _decision_links_goal(record, selected_goal.get("id"))]]
        for record in action_records:
            blockers.extend(_explicit_values(record, ("blocker", "blockers"), ("blocker", "blockers", "차단", "블로커")))
            next_actions.extend(_explicit_values(record, ("next_action", "next_actions"), ("next action", "next actions", "다음 작업", "다음 행동")))
    blockers = list(dict.fromkeys(blockers))[:_MAX_ITEMS["blockers"]]
    next_actions = list(dict.fromkeys(next_actions))[:_MAX_ITEMS["next_actions"]]
    if (selected_goal is not None or not active_goals) and not next_actions:
        reason_codes.append("missing_next_action")

    index = dict(retrieval_diagnostics.get("index") or {})
    source_errors = ((retrieval_diagnostics.get("sources") or {}).get("errors") or {})
    if index.get("fresh") is None:
        for message in source_errors.values():
            stale = re.search(r"memory_index_stale:([^;\s]+)", str(message))
            if stale:
                index.update(fresh=False, reason_code=stale.group(1))
                break
    if index.get("fresh") is False:
        reason_codes.append("stale_index:" + str(index.get("reason_code") or "unknown"))
    reason_codes = list(dict.fromkeys(_bounded(code, _ID_LIMIT) for code in reason_codes if code))
    status = "conflict" if structured_conflict or len(active_goals) > 1 else "complete"
    if status != "conflict" and any(code.startswith(("missing_", "stale_index:", "other_repository_evidence_excluded")) for code in reason_codes):
        status = "partial"
    evidence_refs = [_source_ref(record) for record in bound[:_MAX_ITEMS["evidence_refs"]]]
    return _packet({"schema": SCHEMA, "status": status, "repository": repository, "reason_codes": reason_codes, "goals": [_item(selected_goal)] if selected_goal else [], "adrs": [_item(record) for record in adrs[:_MAX_ITEMS["adrs"]]], "decisions": [_item(record) for record in decisions[:_MAX_ITEMS["decisions"]]], "evidence_refs": evidence_refs, "blockers": blockers, "next_actions": next_actions, "freshness": {"index": _index_ref(index), "sources": evidence_refs}, "source_refs": {"repository_manifest": _manifest_ref(manifest_source)}})
