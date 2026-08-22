# -*- coding: utf-8 -*-
"""질의 엔티티 → 1-hop 그래프 → 제한된 근거 청크로 컨텍스트를 조립한다.

이 모듈은 검색 저장소를 직접 열지 않는다. 프로젝트 카탈로그는 작은 SoT 레지스트리
한 파일에서 만들고, 검색과 그래프 데이터는 호출자가 주입한다. 따라서 전체 brain 문서
순회나 공유 Qdrant 쓰기 없이 결정론적으로 테스트할 수 있다.
"""
from __future__ import annotations

import copy
import logging
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

import yaml

from core.paths import resolve_memory_dir

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

_SPECIAL_ALIASES: dict[str, tuple[str, ...]] = {
    "요한 생태계": ("요한 생태계", "yohan ecosystem"),
    "yohan-brain": ("yohan-brain", "yohan brain", "요한 브레인"),
    "yohan-mcp": ("yohan-mcp", "yohan mcp", "요한 mcp"),
}
_ASCII_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)+")
_UPPER_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])[A-Z][A-Z0-9]{2,}(?![A-Za-z0-9])")


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


def load_project_catalog(memory_dir: Path | None = None) -> dict[str, tuple[str, ...]]:
    """inheritance-registry.yaml의 repos 키만 읽어 프로젝트 별칭 카탈로그를 만든다.

    레지스트리가 없거나 손상되어도 특수 생태계 별칭으로 안전하게 폴백한다. 문서 본문이나
    디렉터리는 순회하지 않는다.
    """
    catalog = {name: _unique(aliases) for name, aliases in _SPECIAL_ALIASES.items()}
    registry = (memory_dir or resolve_memory_dir()) / "core" / "inheritance-registry.yaml"
    if not registry.is_file():
        return catalog
    try:
        payload = yaml.safe_load(registry.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("프로젝트 레지스트리 로드 실패: %s: %s", type(exc).__name__, exc)
        return catalog
    repos = payload.get("repos")
    if not isinstance(repos, dict):
        return catalog
    for name in repos:
        canonical = str(name).strip()
        if canonical:
            catalog[canonical] = _aliases_for_project(canonical)
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
    if project:
        canonical, alias = explicit or (str(project).strip(), str(project).strip())
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

    # 레지스트리에 아직 없는 명시적 슬러그·대문자 프로젝트도 제한적으로 인식한다.
    # 일반 자연어 단어까지 엔티티로 승격하지 않아 보충 검색 폭주를 막는다.
    for match in [*_ASCII_WORD_RE.finditer(query), *_UPPER_TOKEN_RE.finditer(query)]:
        alias = match.group(0)
        key = _normalized(alias)
        if key in seen:
            continue
        seen.add(key)
        canonical = alias.casefold()
        found.append(EntityMatch(canonical, alias, _unique((canonical, alias)), "project"))
        if len(found) >= max(0, max_entities):
            break
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
    compact, used = _truncate_value(clone.get("data") or {}, remaining)
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


def _rank_records(records: list[dict], entities: list[EntityMatch], edges: list[dict]) -> list[dict]:
    entity_terms, graph_terms = _ranking_terms(entities, edges)
    ranked: list[tuple[int, int, dict]] = []
    seen: set[tuple[str, str]] = set()
    for index, record in enumerate(records):
        key = _record_key(record)
        if key in seen:
            continue
        seen.add(key)
        haystack = _normalized(" ".join(_string_values(record.get("data") or {})))
        score = sum(5 for term in entity_terms if _normalized(term) in haystack)
        score += sum(2 for term in graph_terms if _normalized(term) in haystack)
        if _is_ontology_record(record):
            score -= 1  # 관계는 graph_edges로 이미 보존하므로 근거 문서를 먼저 표출한다.
        ranked.append((-score, index, record))
    ranked.sort(key=lambda item: (item[0], item[1]))
    return [record for _, _, record in ranked]


def _apply_budgets(records: list[dict], *, max_matches: int, char_budget: int) -> tuple[list[dict], int]:
    if max_matches <= 0 or char_budget <= 0:
        return [], 0
    selected: list[dict] = []
    used = 0
    for record in records:
        if len(selected) >= max_matches or used >= char_budget:
            break
        cost = _payload_chars(record)
        remaining = char_budget - used
        if cost <= remaining:
            selected.append(copy.deepcopy(record))
            used += cost
            continue
        compact, compact_cost = _compact_record(record, remaining)
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

        ranked = _rank_records(all_records, entities, edges)
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
