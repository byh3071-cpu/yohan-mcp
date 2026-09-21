# -*- coding: utf-8 -*-
"""질의 엔티티 → 1-hop 그래프 → 제한된 근거 청크로 컨텍스트를 조립한다.

이 모듈은 검색 저장소를 직접 열지 않는다. 프로젝트 카탈로그는 작은 SoT 레지스트리
한 파일에서 만들고, 검색과 그래프 데이터는 호출자가 주입한다. 따라서 전체 brain 문서
순회나 공유 Qdrant 쓰기 없이 결정론적으로 테스트할 수 있다.
"""
from __future__ import annotations

import copy
import hashlib
import logging
import re
import subprocess
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

import yaml

from core.paths import resolve_memory_dir
from core.paths import resolve_brain_root
from core.task_context import load_manifest_aliases

logger = logging.getLogger(__name__)

ONTOLOGY_TYPE = "ontology_triples"
DEFAULT_CONTEXT_CHAR_BUDGET = 12_000
DEFAULT_CONTEXT_MAX_MATCHES = 8
DEFAULT_MAX_ENTITIES = 3
DEFAULT_MAX_EDGES = 8
DEFAULT_MIN_EVIDENCE = 2
MAX_CONTEXT_CHAR_BUDGET = 100_000
MAX_CONTEXT_MATCHES = 50
MAX_GRAPH_EDGES = 30

_RUNTIME_BUNDLE_PATHS = (
    "adapters/base.py",
    "adapters/memory_adapter.py",
    "core/context_resolver.py",
    "core/task_context.py",
    "core/paths.py",
    "core/router.py",
    "core/tools.py",
    "server.py",
)


def _runtime_bundle_digest() -> str:
    repository_root = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    for relative_path in sorted(_RUNTIME_BUNDLE_PATHS):
        raw = (repository_root / relative_path).read_bytes()
        normalized = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        if not normalized.endswith(b"\n"):
            normalized += b"\n"
        digest.update(relative_path.encode("utf-8") + b"\n" + normalized)
    return digest.hexdigest()


RETRIEVAL_RUNTIME_BUNDLE_DIGEST = _runtime_bundle_digest()


def _runtime_source_revision() -> tuple[str | None, str]:
    """Read the executing checkout's revision without exposing its local path."""
    repository_root = Path(__file__).resolve().parents[1]
    try:
        completed = subprocess.run(
            ["git", "-C", str(repository_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=1,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None, "git_metadata_unavailable"
    revision = completed.stdout.strip()
    if completed.returncode != 0:
        return None, "git_metadata_unavailable"
    if not re.fullmatch(r"[0-9a-fA-F]{40,64}", revision):
        return None, "git_revision_invalid"
    return revision.lower(), "available"


# The checkout cannot change for a running server. Capture once to keep request
# latency deterministic and avoid repeatedly invoking a subprocess.
RETRIEVAL_RUNTIME_SOURCE_REVISION, RETRIEVAL_RUNTIME_SOURCE_REVISION_REASON = _runtime_source_revision()

_RANK_QUERY_STOPWORDS = {
    "그", "무엇", "무엇이고", "무엇인가", "어떤", "어디", "어디에",
    "서로", "현재", "중인", "하는가", "해야", "문서",
}
_RANK_KOREAN_SUFFIXES = (
    "에서는", "에게서", "으로", "에서", "까지", "부터", "하고",
    "해야", "인가", "이며", "와", "과", "은", "는", "이", "가",
    "을", "를", "에", "의", "된",
)

_SPECIAL_ALIASES: dict[str, tuple[str, ...]] = {
    "요한 생태계": ("요한 생태계", "yohan ecosystem"),
    "yohan-brain": ("yohan-brain", "yohan brain", "요한 브레인"),
    "yohan-mcp": ("yohan-mcp", "yohan mcp", "요한 mcp"),
}


@dataclass(frozen=True)
class EntityMatch:
    canonical: str
    matched_alias: str
    aliases: tuple[str, ...]
    kind: str = "project"

    def as_dict(self) -> dict:
        return {
            "canonical": self.canonical,
            "matched_alias": self.matched_alias,
            "kind": self.kind,
        }


@dataclass
class ResolvedContext:
    matches: list[dict]
    entities: list[dict]
    graph_edges: list[dict]
    supplemental_searches: int
    context_chars: int
    max_matches: int
    char_budget: int
    supplemental_result: dict | None = None


@dataclass(frozen=True)
class RetrievalDiagnostics:
    """Volatile, read-only candidate telemetry returned by ``get_context``.

    This is candidate-retrieval telemetry, not a domain decision, evaluation or
    learning record.  The MCP server never persists it.
    """

    index_revision: str | None
    index_generation_id: str | None
    index_observed_at: str | None
    corpus_contract_version: str | None
    index_fresh: bool | None
    freshness_reason_code: str | None
    query_binding_digest: str
    recognized_entities: list[dict]
    entity_catalog: dict
    sources_attempted: list[str]
    sources_used: list[str]
    source_errors: dict[str, str]
    collections_requested: list[str]
    collections_available: list[str]
    collections_unavailable: list[dict]
    evidence: list[dict]
    graph_edge_count: int
    supplemental_searches: int
    budgets: dict
    latency_ms: dict

    @classmethod
    def from_retrieval(
        cls,
        *,
        query: str,
        matches: list[dict],
        entities: list[dict],
        graph_edges: list[dict],
        sources_used: list[str],
        errors: dict[str, str],
        diagnostics: dict,
        supplemental_searches: int,
        char_budget: int,
        max_matches: int,
    ) -> "RetrievalDiagnostics":
        query_binding_digest = hashlib.sha256(query.encode("utf-8")).hexdigest()
        backend_details = diagnostics.get("backend_details") or {}
        memory = backend_details.get("memory") or {}
        qdrant_details: list[dict] = []
        primary_qdrant = backend_details.get("qdrant")
        if isinstance(primary_qdrant, dict):
            qdrant_details.append(primary_qdrant)
        supplemental = (diagnostics.get("context_resolver") or {}).get("supplemental_diagnostics") or {}
        supplemental_qdrant = (supplemental.get("backend_details") or {}).get("qdrant")
        if isinstance(supplemental_qdrant, dict):
            qdrant_details.append(supplemental_qdrant)

        requested: list[str] = []
        available: list[str] = []
        unavailable_by_name: dict[str, dict] = {}
        for detail in qdrant_details:
            for name in detail.get("requested_collections") or []:
                if name not in requested:
                    requested.append(name)
            for name in detail.get("available_collections") or []:
                if name not in available:
                    available.append(name)
            for item in detail.get("unavailable_collections") or []:
                if isinstance(item, dict) and item.get("name"):
                    unavailable_by_name[str(item["name"])] = dict(item)

        evidence = []
        for record in matches:
            data = record.get("data") or {}
            evidence_ref = record.get("evidence_ref") or {}
            evidence.append({
                "type": record.get("type"),
                "id": record.get("id"),
                "backend": record.get("backend"),
                "path": data.get("_path") or data.get("path"),
                "sources": list(record.get("sources") or []),
                "score": record.get("rrf_score", record.get("score")),
                "document_id": evidence_ref.get("document_id"),
                "content_hash": evidence_ref.get("content_hash"),
                "locator": evidence_ref.get("locator"),
            })
        return cls(
            index_revision=memory.get("index_revision"),
            index_generation_id=memory.get("index_generation_id"),
            index_observed_at=memory.get("index_observed_at"),
            corpus_contract_version=memory.get("corpus_contract_version"),
            index_fresh=memory.get("fresh"),
            freshness_reason_code=memory.get("freshness_reason_code"),
            query_binding_digest=query_binding_digest,
            recognized_entities=list(entities),
            entity_catalog=dict(diagnostics.get("entity_catalog") or {}),
            sources_attempted=list((diagnostics.get("backend_timings_ms") or {}).keys()),
            sources_used=list(sources_used),
            source_errors=dict(errors),
            collections_requested=requested,
            collections_available=available,
            collections_unavailable=list(unavailable_by_name.values()),
            evidence=evidence,
            graph_edge_count=len(graph_edges),
            supplemental_searches=supplemental_searches,
            budgets={"character": char_budget, "matches": max_matches},
            latency_ms={
                "backends": dict(diagnostics.get("backend_timings_ms") or {}),
                "index_build": memory.get("index_build_ms"),
                "lexical_query": memory.get("query_latency_ms"),
            },
        )

    def as_dict(self) -> dict:
        return {
            "schema": "retrieval-diagnostics/v1",
            "volatile": True,
            "persisted": False,
            "runtime": {
                "repository": "yohan-mcp",
                "implementation_digest": RETRIEVAL_RUNTIME_BUNDLE_DIGEST,
                "source_revision": RETRIEVAL_RUNTIME_SOURCE_REVISION,
                "source_revision_reason_code": RETRIEVAL_RUNTIME_SOURCE_REVISION_REASON,
            },
            "query_binding": {
                "scheme": "sha256-utf8-v1",
                "digest": self.query_binding_digest,
            },
            "index": {
                "revision": self.index_revision,
                "generation_id": self.index_generation_id,
                "observed_at": self.index_observed_at,
                "corpus_contract_version": self.corpus_contract_version,
                "fresh": self.index_fresh,
                "reason_code": self.freshness_reason_code,
            },
            "recognized_entities": self.recognized_entities,
            "entity_catalog": self.entity_catalog,
            "sources": {
                "attempted": self.sources_attempted,
                "used": self.sources_used,
                "errors": self.source_errors,
            },
            "collections": {
                "requested": self.collections_requested,
                "available": self.collections_available,
                "unavailable": self.collections_unavailable,
            },
            "evidence": self.evidence,
            "graph_edge_count": self.graph_edge_count,
            "supplemental_searches": self.supplemental_searches,
            "budgets": self.budgets,
            "latency_ms": self.latency_ms,
        }


SearchPort = Callable[[str, dict], Awaitable[dict]]


def _unique(values) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        text = str(value or "").strip()
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            out.append(text)
    return tuple(out)


def _aliases_for_project(name: str) -> tuple[str, ...]:
    aliases = [name]
    if "-" in name:
        aliases.append(name.replace("-", " "))
    aliases.extend(_SPECIAL_ALIASES.get(name, ()))
    return _unique(aliases)


class ProjectCatalog(dict[str, tuple[str, ...]]):
    """Mapping-compatible strict catalog with explicit load diagnostics."""

    def __init__(self, *args, diagnostics: dict | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.diagnostics = dict(diagnostics or {})


def load_project_catalog(memory_dir: Path | None = None) -> dict[str, tuple[str, ...]]:
    """Build the strict project catalog from observed + declared Brain SoTs.

    The provider-backed live snapshot contributes observed repositories; the
    inheritance registry contributes declared intent. Neither source is
    inferred from arbitrary document text or directory names.
    """
    base = memory_dir or resolve_memory_dir()
    core = base / "core"
    catalog = ProjectCatalog(
        {name: _unique(aliases) for name, aliases in _SPECIAL_ALIASES.items()},
        diagnostics={
            "source": "memory/core/repository-live-snapshot.yaml+inheritance-registry.yaml",
            "revision": None,
            "degraded": True,
            "reason_code": "catalog_sources_missing",
            "source_states": {},
            "invalid_keys_excluded": 0,
            "observed_count": 0,
            "declared_count": 0,
        },
    )

    def valid_name(name) -> str | None:
        canonical = str(name).strip()
        if (
            not canonical
            or not _normalized(canonical)
            or canonical in {".", ".."}
            or any(char in canonical for char in "/\\\x00")
        ):
            return None
        return canonical

    digests: list[str] = []
    invalid = 0
    states: dict[str, str] = {}

    snapshot = core / "repository-live-snapshot.yaml"
    if snapshot.is_file():
        try:
            raw = snapshot.read_text(encoding="utf-8")
            payload = yaml.safe_load(raw) or {}
            repos = payload.get("repositories")
            if payload.get("schema") != "repository-live-snapshot" or not isinstance(repos, list):
                raise ValueError("snapshot schema/repositories invalid")
            for item in repos:
                if not isinstance(item, dict) or item.get("source_status") != "observed":
                    invalid += 1
                    continue
                canonical = valid_name(item.get("repo_name"))
                if canonical is None:
                    invalid += 1
                    continue
                catalog[canonical] = _aliases_for_project(canonical)
            catalog.diagnostics["observed_count"] = sum(
                1 for item in repos
                if isinstance(item, dict) and item.get("source_status") == "observed"
                and valid_name(item.get("repo_name")) is not None
            )
            digests.append(hashlib.sha256(raw.encode("utf-8")).hexdigest())
            states["live_snapshot"] = "loaded"
        except (OSError, yaml.YAMLError, ValueError) as exc:
            logger.warning("live repository snapshot load failed: %s: %s", type(exc).__name__, exc)
            states["live_snapshot"] = "corrupt_or_invalid"
    else:
        states["live_snapshot"] = "missing"

    registry = core / "inheritance-registry.yaml"
    if registry.is_file():
        try:
            raw = registry.read_text(encoding="utf-8")
            payload = yaml.safe_load(raw) or {}
            repos = payload.get("repos")
            if not isinstance(repos, dict):
                raise ValueError("registry repos invalid")
            declared = 0
            for name in repos:
                canonical = valid_name(name)
                if canonical is None:
                    invalid += 1
                    continue
                catalog[canonical] = _aliases_for_project(canonical)
                declared += 1
            catalog.diagnostics["declared_count"] = declared
            digests.append(hashlib.sha256(raw.encode("utf-8")).hexdigest())
            states["declarative_registry"] = "loaded"
        except (OSError, yaml.YAMLError, ValueError) as exc:
            logger.warning("project registry load failed: %s: %s", type(exc).__name__, exc)
            states["declarative_registry"] = "corrupt_or_invalid"
    else:
        states["declarative_registry"] = "missing"

    # The versioned Public/dev mirror owns repository aliases such as
    # ``yohan-log`` → ``muse``.  It augments the strict catalog; an absent or
    # invalid mirror never removes snapshot/registry identities.
    brain_root = resolve_brain_root(base)
    if brain_root is not None:
        catalog.diagnostics["source"] += "+ops/public-dev-bootstrap/repos.json"
        manifest_aliases, manifest_diagnostics = load_manifest_aliases(brain_root)
        manifest_state = str(manifest_diagnostics.get("status") or "invalid")
        states["repository_manifest"] = manifest_state
        catalog.diagnostics["manifest_reason_code"] = manifest_diagnostics.get("reason_code")
        if manifest_state == "loaded":
            manifest_tokens: list[str] = []
            for name, aliases in sorted(manifest_aliases.items(), key=lambda item: item[0].casefold()):
                manifest_tokens.extend((name, *aliases, ""))
            manifest_material = "\0".join(manifest_tokens)
            digests.append(hashlib.sha256(manifest_material.encode("utf-8")).hexdigest())
            merged = 0
            for name, aliases in manifest_aliases.items():
                canonical = valid_name(name)
                if canonical is None:
                    invalid += 1
                    continue
                catalog[canonical] = _unique((*catalog.get(canonical, ()), canonical, *aliases))
                merged += 1
            catalog.diagnostics["manifest_alias_count"] = merged

    required_states = ("live_snapshot", "declarative_registry")
    loaded_both = all(states.get(name) == "loaded" for name in required_states)
    manifest_degraded = (
        "repository_manifest" in states and states["repository_manifest"] != "loaded"
    )
    catalog.diagnostics.update({
        "revision": hashlib.sha256("\0".join(digests).encode("ascii")).hexdigest() if digests else None,
        "degraded": not loaded_both or manifest_degraded,
        "reason_code": "loaded" if loaded_both and not manifest_degraded else "+".join(
            f"{name}_{state}" for name, state in states.items() if state != "loaded"
        ),
        "source_states": states,
        "invalid_keys_excluded": invalid,
    })
    return catalog


def _normalized(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(char for char in text if char.isalnum())


def _alias_position(query: str, alias: str) -> int | None:
    q = unicodedata.normalize("NFKC", query).casefold()
    a = unicodedata.normalize("NFKC", alias).casefold().strip()
    if not a:
        return None
    if all(ord(char) < 128 for char in a):
        pieces = [re.escape(piece) for piece in re.split(r"[-_\s]+", a) if piece]
        # punctuation-only legacy names (예: registry의 "---")는 검색 엔티티가 아니다.
        # 빈 pattern을 정규식으로 실행하면 모든 문자열의 경계에 매치되어 실제 프로젝트
        # 엔티티 슬롯을 밀어내므로 명시적으로 거부한다.
        if not pieces:
            return None
        pattern = r"[-_\s]+".join(pieces)
        match = re.search(rf"(?<![a-z0-9]){pattern}(?![a-z0-9])", q)
        return match.start() if match else None
    pos = q.find(a)
    return pos if pos >= 0 else None


def _catalog_canonical(value: str, catalog: dict[str, tuple[str, ...]]) -> tuple[str, str] | None:
    key = _normalized(value)
    for canonical, aliases in catalog.items():
        for alias in aliases:
            if _normalized(alias) == key:
                return canonical, alias
    return None


def resolve_entities(
    query: str,
    catalog: dict[str, tuple[str, ...]],
    *,
    project: str | None = None,
    max_entities: int = DEFAULT_MAX_ENTITIES,
) -> list[EntityMatch]:
    """질의와 명시 project에서 대표 프로젝트/생태계 엔티티를 결정론적으로 찾는다."""
    if max_entities <= 0:
        return []
    candidates: list[tuple[int, int, str, str, tuple[str, ...], str]] = []
    explicit = _catalog_canonical(project, catalog) if project else None
    if explicit:
        canonical, alias = explicit
        aliases = catalog.get(canonical, _unique((canonical, alias)))
        kind = "ecosystem" if canonical == "요한 생태계" else "project"
        # 명시 project는 질의에 없는 경우의 폴백이다. 질의에 실제 등장한 엔티티의
        # 위치 순서를 앞질러 컨텍스트 우선순위를 뒤집지 않는다.
        candidates.append((len(query) + 1, -len(alias), canonical, alias, aliases, kind))

    for canonical, aliases in catalog.items():
        best: tuple[int, str] | None = None
        for alias in aliases:
            pos = _alias_position(query, alias)
            if pos is not None and (best is None or (pos, -len(alias)) < (best[0], -len(best[1]))):
                best = (pos, alias)
        if best is not None:
            kind = "ecosystem" if canonical == "요한 생태계" else "project"
            candidates.append((best[0], -len(best[1]), canonical, best[1], aliases, kind))

    found: list[EntityMatch] = []
    seen: set[str] = set()
    for _, _, canonical, alias, aliases, kind in sorted(candidates):
        key = _normalized(canonical)
        if key in seen:
            continue
        seen.add(key)
        found.append(EntityMatch(canonical, alias, aliases, kind))
        if len(found) >= max(0, max_entities):
            return found

    # Strict catalog contract: an unknown slug or uppercase concept is not a
    # project.  In particular, "AGI" may be promoted to a concept only from an
    # evidence-bearing ontology record; it must never become a fake project.
    return found


def _is_ontology_record(record: dict) -> bool:
    data = record.get("data") or {}
    return record.get("type") == ONTOLOGY_TYPE or all(
        isinstance(data.get(key), str) and data.get(key) for key in ("subject", "relation", "object")
    )


def _node_matches_entity(node: str, entity: EntityMatch) -> bool:
    node_key = _normalized(node)
    if not node_key:
        return False
    for candidate in (entity.canonical, *entity.aliases):
        candidate_key = _normalized(candidate)
        if candidate_key and (node_key == candidate_key or candidate_key in node_key or node_key in candidate_key):
            return True
    return False


def _augment_entities_from_graph(
    query: str,
    records: list[dict],
    entities: list[EntityMatch],
    max_entities: int,
) -> list[EntityMatch]:
    if max_entities <= 0:
        return []
    out = list(entities)
    seen = {_normalized(entity.canonical) for entity in out}
    for record in records:
        if not _is_ontology_record(record):
            continue
        data = record.get("data") or {}
        for node in (data.get("subject"), data.get("object")):
            node = str(node or "").strip()
            if not node or _alias_position(query, node) is None:
                continue
            key = _normalized(node)
            if key in seen:
                continue
            seen.add(key)
            out.append(EntityMatch(node, node, (node,), "concept"))
            if len(out) >= max_entities:
                return out
    return out


def extract_one_hop_edges(
    records: list[dict], entities: list[EntityMatch], *, max_edges: int = DEFAULT_MAX_EDGES
) -> list[dict]:
    """검색 결과 중 엔티티가 직접 주어/목적어인 트리플만 반환한다."""
    if max_edges <= 0:
        return []
    edges: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for record in records:
        if not _is_ontology_record(record):
            continue
        data = record.get("data") or {}
        subject = str(data.get("subject") or "").strip()
        relation = str(data.get("relation") or "").strip()
        obj = str(data.get("object") or "").strip()
        if not subject or not relation or not obj:
            continue
        if not any(
            _node_matches_entity(subject, entity) or _node_matches_entity(obj, entity)
            for entity in entities
        ):
            continue
        key = (_normalized(subject), relation.casefold(), _normalized(obj))
        if key in seen:
            continue
        seen.add(key)
        edges.append(
            {
                "subject": subject,
                "relation": relation,
                "object": obj,
                "domain": data.get("domain"),
                "confidence": data.get("confidence"),
                "source": data.get("source"),
                "text": data.get("text"),
                "record_id": record.get("id"),
                "backend": record.get("backend"),
            }
        )
        if len(edges) >= max(0, max_edges):
            break
    return edges


def _bounded_int(value, default: int, *, minimum: int = 0, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return min(max(parsed, minimum), maximum)


def _top_k(value) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 5
    if parsed < 0:
        return -1
    return min(parsed, MAX_CONTEXT_MATCHES)


def _record_key(record: dict) -> tuple[str, str]:
    type_ = str(record.get("type") or "")
    id_ = str(record.get("id") or "")
    if id_:
        return type_, id_
    data = record.get("data") or {}
    return type_, repr(sorted(data.items()))


def _string_values(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        out: list[str] = []
        for item in value.values():
            out.extend(_string_values(item))
        return out
    if isinstance(value, (list, tuple)):
        out = []
        for item in value:
            out.extend(_string_values(item))
        return out
    return []


def _payload_chars(record: dict) -> int:
    return sum(len(value) for value in _string_values(record.get("data") or {}))


def _truncate_value(value, remaining: int):
    if isinstance(value, str):
        kept = value[:remaining]
        return kept, len(kept)
    if isinstance(value, dict):
        out = {}
        used = 0
        for key, item in value.items():
            compact, cost = _truncate_value(item, max(0, remaining - used))
            out[key] = compact
            used += cost
        return out, used
    if isinstance(value, list):
        out = []
        used = 0
        for item in value:
            compact, cost = _truncate_value(item, max(0, remaining - used))
            out.append(compact)
            used += cost
        return out, used
    if isinstance(value, tuple):
        compact, used = _truncate_value(list(value), remaining)
        return tuple(compact), used
    return copy.deepcopy(value), 0


def _compact_record(record: dict, remaining: int) -> tuple[dict, int]:
    clone = copy.deepcopy(record)
    data = clone.get("data") or {}
    locator = data.get("_path") or data.get("path")
    if isinstance(locator, str) and len(locator) > remaining:
        # A truncated path is not an evidence locator. Exclude the record
        # rather than emitting a plausible-looking but unusable partial path.
        return clone, 0
    # Evidence locators must survive truncation. Parsed Markdown appends _path
    # after the body, so generic insertion-order truncation used to return an
    # empty locator exactly when a large body exhausted the budget.
    identity_keys = (
        "_path", "path", "id", "title", "name", "type", "status",
        "_retrieval_tier",
    )
    ordered = {
        key: data[key]
        for key in identity_keys
        if key in data
    }
    ordered.update({key: value for key, value in data.items() if key not in ordered})
    compact, used = _truncate_value(ordered, remaining)
    clone["data"] = compact
    return clone, used


def _ranking_terms(entities: list[EntityMatch], edges: list[dict]) -> tuple[list[str], list[str]]:
    entity_terms = _unique(
        value for entity in entities for value in (entity.canonical, *entity.aliases)
    )
    graph_terms = _unique(
        value
        for edge in edges
        for value in (edge.get("subject"), edge.get("object"), edge.get("source"))
        if value
    )
    return list(entity_terms), list(graph_terms)


def _query_rank_tokens(query: str) -> tuple[str, ...]:
    tokens: list[str] = []
    for raw in re.findall(r"[0-9A-Za-z가-힣_-]+", query or ""):
        token = unicodedata.normalize("NFKC", raw).casefold()
        for suffix in _RANK_KOREAN_SUFFIXES:
            if token.endswith(suffix) and len(token) > len(suffix) + 1:
                token = token[:-len(suffix)]
                break
        if len(token) >= 2 and token not in _RANK_QUERY_STOPWORDS and token not in tokens:
            tokens.append(token)
    return tuple(tokens)


def _rank_records(
    query: str,
    records: list[dict],
    entities: list[EntityMatch],
    edges: list[dict],
) -> list[dict]:
    entity_terms, graph_terms = _ranking_terms(entities, edges)
    query_terms = _query_rank_tokens(query)
    ranked: list[tuple[int, int, int, dict]] = []
    seen: set[tuple[str, str]] = set()
    for index, record in enumerate(records):
        key = _record_key(record)
        if key in seen:
            continue
        seen.add(key)
        haystack = _normalized(" ".join(_string_values(record.get("data") or {})))
        # Query evidence outranks a broad entity mention. Previously a generic
        # "ecosystem" reference could push the Personal AGI evidence out of a
        # five-item context even though the lexical backend ranked it highly.
        query_matches = sum(1 for term in query_terms if _normalized(term) in haystack)
        score = query_matches * 4
        score += sum(3 for term in entity_terms if _normalized(term) in haystack)
        score += sum(2 for term in graph_terms if _normalized(term) in haystack)
        if _is_ontology_record(record):
            score -= 1  # 관계는 graph_edges로 이미 보존하므로 근거 문서를 먼저 표출한다.
        data = record.get("data") or {}
        sources = set(record.get("sources") or [])
        protected_lexical = (
            "memory" in sources
            and str(record.get("type") or "").startswith("brain:")
            and bool(data.get("_retrieval_tier"))
        )
        # Brain contract documents are the required first-stage evidence.
        # Optional vector/notion candidates may expand the pool and graph, but
        # cannot displace the lexical ordering inside a bounded final context.
        # Preserve their upstream memory rank verbatim; rerank only expansion
        # candidates by query/entity/graph evidence.
        group = 0 if protected_lexical else 1
        rank_score = 0 if protected_lexical else -score
        ranked.append((group, rank_score, index, record))
    ranked.sort(key=lambda item: (item[0], item[1], item[2]))
    return [record for _, _, _, record in ranked]


def _apply_budgets(records: list[dict], *, max_matches: int, char_budget: int) -> tuple[list[dict], int]:
    if max_matches <= 0 or char_budget <= 0:
        return [], 0
    selected: list[dict] = []
    used = 0
    slot_count = min(max_matches, len(records))
    per_record_floor = max(1, char_budget // max(1, slot_count))
    for index, record in enumerate(records):
        if len(selected) >= max_matches or used >= char_budget:
            break
        cost = _payload_chars(record)
        remaining = char_budget - used
        slots_after = max(0, slot_count - index - 1)
        allocation = min(
            remaining,
            max(per_record_floor, remaining - per_record_floor * slots_after),
        )
        if cost <= allocation:
            selected.append(copy.deepcopy(record))
            used += cost
            continue
        compact, compact_cost = _compact_record(record, allocation)
        if compact_cost > 0:
            selected.append(compact)
            used += compact_cost
    return selected, used


def _supplemental_query(entities: list[EntityMatch], edges: list[dict]) -> str:
    terms: list[str] = [entity.canonical for entity in entities]
    for edge in edges:
        terms.extend([edge.get("subject"), edge.get("object"), edge.get("source")])
    return " ".join(_unique(value for value in terms if value)[:8])


class GraphAwareContextResolver:
    """검색 포트를 최대 두 번 사용해 그래프 인지 컨텍스트를 조립한다."""

    def __init__(self, catalog: dict[str, tuple[str, ...]] | None = None) -> None:
        self.catalog = catalog if catalog is not None else load_project_catalog()

    async def resolve(
        self,
        query: str,
        primary: dict,
        search: SearchPort | None,
        opts: dict | None = None,
    ) -> ResolvedContext:
        opts = dict(opts or {})
        max_entities = _bounded_int(
            opts.get("context_max_entities"), DEFAULT_MAX_ENTITIES, maximum=10
        )
        max_edges = _bounded_int(opts.get("context_max_edges"), DEFAULT_MAX_EDGES, maximum=MAX_GRAPH_EDGES)
        char_budget = _bounded_int(
            opts.get("context_char_budget"),
            DEFAULT_CONTEXT_CHAR_BUDGET,
            maximum=MAX_CONTEXT_CHAR_BUDGET,
        )
        top_k = _top_k(opts.get("top_k", 5))
        default_max_matches = top_k if top_k >= 0 else DEFAULT_CONTEXT_MAX_MATCHES
        max_matches = _bounded_int(
            opts.get("context_max_matches"), default_max_matches, maximum=MAX_CONTEXT_MATCHES
        )
        min_evidence = _bounded_int(
            opts.get("min_evidence"), DEFAULT_MIN_EVIDENCE, maximum=20
        )

        primary_records = list(primary.get("results") or [])
        entities = resolve_entities(
            query,
            self.catalog,
            project=opts.get("project"),
            max_entities=max_entities,
        )
        entities = _augment_entities_from_graph(query, primary_records, entities, max_entities)
        edges = extract_one_hop_edges(primary_records, entities, max_edges=max_edges)
        evidence_count = sum(not _is_ontology_record(record) for record in primary_records)
        sufficient = evidence_count >= min_evidence

        supplemental_result: dict | None = None
        supplemental_searches = 0
        allow_supplemental = opts.get("context_supplemental", True) is not False
        if (
            search is not None
            and entities
            and top_k != 0
            and allow_supplemental
            and not sufficient
        ):
            supplemental_query = _supplemental_query(entities, edges)
            if supplemental_query:
                supplemental_searches = 1
                try:
                    supplemental_result = await search(supplemental_query, dict(opts))
                except Exception as exc:  # 보충 회수 실패가 1차 근거를 파괴하지 않게 격리한다.
                    supplemental_result = {
                        "results": [],
                        "sources_used": [],
                        "errors": {"context_resolver": f"{type(exc).__name__}: {exc}"},
                        "diagnostics": {},
                    }

        all_records = list(primary_records)
        if supplemental_result:
            all_records.extend(supplemental_result.get("results") or [])
            entities = _augment_entities_from_graph(query, all_records, entities, max_entities)
            edges = extract_one_hop_edges(all_records, entities, max_edges=max_edges)

        ranked = _rank_records(query, all_records, entities, edges)
        matches, context_chars = _apply_budgets(
            ranked,
            max_matches=max_matches,
            char_budget=char_budget,
        )
        return ResolvedContext(
            matches=matches,
            entities=[entity.as_dict() for entity in entities],
            graph_edges=edges,
            supplemental_searches=supplemental_searches,
            context_chars=context_chars,
            max_matches=max_matches,
            char_budget=char_budget,
            supplemental_result=supplemental_result,
        )
