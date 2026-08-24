# -*- coding: utf-8 -*-
"""memory 백엔드 어댑터 (P2 실동작 + ADR-008 B.3 brain .md 읽기).

로컬 파일시스템 memory/ 디렉토리를 읽고 쓴다.
- profile  → memory/profile.yaml           (단일)
- decision → memory/decisions/<id>.yaml
- ingest   → memory/ingest/<id>.yaml

환경변수 MEMORY_DIR / YOHAN_BRAIN_ROOT 로 베이스 경로 변경 (기본: 리포 루트/memory, deprecated).

ADR-008 B.3 — brain(yohan-brain/memory/)은 지식을 `.md`+frontmatter 로 쌓는다(수백 건). yaml 만
읽으면 그 지식이 search/get_context 에 안 잡혀 memory 백엔드가 사실상 빈다. 지식 폴더 allowlist 의
`.md` 를 **읽기 전용**으로 순회해 frontmatter+본문을 레코드로 노출한다(type=`brain:<folder>`,
스키마 없는 회수용). 쓰기 경로는 무변경 — brain 파일을 건드리지 않는다.
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import re
import threading
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from types import MappingProxyType
from typing import Mapping

import yaml

from adapters.base import BackendAdapter, _Timer, health, make_record
from core.paths import resolve_brain_root, resolve_memory_dir

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent

# 타입 → (서브경로, 단일파일 여부)
_LAYOUT = {
    "profile": ("profile.yaml", True),
    "decision": ("decisions", False),
    "ingest": ("ingest", False),
}
# 타입별 ID 필드명 (스키마 PK)
_ID_FIELD = {"profile": "name", "decision": "decision_id", "ingest": "ingest_id"}

# ADR-008 B.3 — brain memory 에서 읽어올 지식 폴더 allowlist(읽기 전용).
# 제외: logs·metrics·ops·inbox·templates·core (회수 노이즈·저가치·config 중복).
_BRAIN_KNOWLEDGE_DIRS = (
    "decisions",
    "wiki",
    "knowledge-hub",
    "logs",
    "rules",
)
_BRAIN_NESTED_DOC_DIRS = (
    ("ingest/insights", "ingest", "P2"),
)
_BRAIN_REPO_DOC_DIRS = (
    ("docs/specs", "specs", "P1"),
    ("docs/troubleshooting", "troubleshooting", "P1"),
)
_BRAIN_EXACT_TIER_OVERRIDES = {
    "wiki/concepts/personal-agi.md": "P1",
    "knowledge-hub/triple-map.md": "P1",
}
# brain 지식노트 본문 상한 — 회수 봉투 토큰 폭증 방지(전문은 data._path 로 접근).
_MD_BODY_MAX = 4000

# Repository-level sources whose omission made exact retrieval disagree with
# the Brain's own entry contract.  The same iterator is reused by the vector
# seeder so lexical and semantic retrieval do not drift apart.
_BRAIN_ROOT_SOT_FILES = (
    "YOHAN-ECOSYSTEM-SOT.md",
    "ASSETS.md",
)
_BRAIN_STATE_FILES = ("soul.yaml", "active-project.yaml")
_BRAIN_CORE_FILES = (
    "ecosystem-sot.yaml",
    "ecosystem-contract.yaml",
    "projects.yaml",
    "inheritance-registry.yaml",
    "repository-live-snapshot.yaml",
    "retrieval-contract.yaml",
    "work-item-id-reservations.yaml",
    "agent-roster.yaml",
    "core-ruleset.yaml",
)
_ACTIVE_GOAL_STATUSES_EXCLUDED = {
    "DONE",
    "CANCELLED",
    "ARCHIVED",
}
_LIVE_ADR_STATUSES = {"accepted", "proposed"}
_EVALUATION_CONTRACT_PATH = "memory/core/retrieval-contract.yaml"
_TOKEN_RE = re.compile(r"[0-9A-Za-z가-힣_-]+")
_QUERY_STOPWORDS = {
    "그",
    "무엇",
    "무엇이고",
    "무엇인가",
    "어떤",
    "어디",
    "어디에",
    "서로",
    "현재",
    "중인",
    "하는가",
    "해야",
    "문서",
}
_LEXICAL_QUERY_EXPANSIONS = {
    "생태계": {"ecosystem"},
    "퍼스널": {"personal"},
    "관계": {"relation", "relationship"},
    "저장소": {"repository", "repositories", "repo", "repos"},
    "등록": {"registry", "registered"},
    "등록된": {"registry", "registered"},
    "관측": {"observed", "observation", "snapshot"},
    "차이": {"difference", "diff"},
    "디자인": {"design"},
    "산출물": {"artifact", "artifacts", "output", "outputs"},
    "원본": {"source", "original"},
    "자산": {"asset", "assets"},
    "보관": {"archive", "storage", "store", "location", "ownership"},
    "진행": {"active", "in_progress"},
    "goal": {"goals"},
    "근거": {"evidence", "reason", "adr"},
}
_KOREAN_QUERY_SUFFIXES = (
    "에서는", "에게서", "으로", "에서", "까지", "부터", "하고",
    "해야", "인가", "이며", "와", "과", "은", "는", "이", "가",
    "을", "를", "에", "의", "된",
)


@dataclass(frozen=True)
class _SearchRow:
    type: str
    id: str
    data: dict
    blob: str
    tokens: frozenset[str]
    priority: int
    evaluation_contract: bool


@dataclass(frozen=True)
class _SourceFingerprint:
    path: str
    mtime_ns: int
    size: int
    ctime_ns: int
    content_hash: str
    status: str


@dataclass(frozen=True)
class _ManifestDocument:
    document_id: str
    path: str
    content_hash: str
    priority: str
    kind: str
    indexed_at: str


@dataclass(frozen=True)
class _SearchGeneration:
    rows: tuple[_SearchRow, ...]
    token_postings: Mapping[str, frozenset[int]]
    priority_row_count: int
    indexed_files: tuple[_SourceFingerprint, ...]
    watched_directories: tuple[tuple[str, int], ...]
    generation_id: str
    generated_at: str
    corpus_contract_version: str | None
    manifest_documents: tuple[_ManifestDocument, ...]
    revision: str
    build_ms: float
    file_reads: int
    invalid_priority_sources: tuple[str, ...]


def iter_priority_brain_files(memory_dir: Path):
    """Yield P0 root/state/core/live-Goal/live-ADR source files.

    This function enumerates files only while an adapter/indexer is being
    initialized or explicitly refreshed.  It must never be called from the
    per-query search path.
    """
    brain_root = resolve_brain_root(memory_dir)
    if brain_root is None:
        return
    brain_resolved = brain_root.resolve()

    def _inside(path: Path) -> bool:
        try:
            return path.resolve().is_relative_to(brain_resolved)
        except OSError:
            return False

    for name in _BRAIN_ROOT_SOT_FILES:
        path = brain_root / name
        if path.is_file() and _inside(path):
            yield "root", path

    for name in _BRAIN_STATE_FILES:
        path = memory_dir / name
        if path.is_file() and _inside(path):
            yield "state", path

    core = memory_dir / "core"
    for name in _BRAIN_CORE_FILES:
        path = core / name
        if path.is_file() and _inside(path):
            yield "core", path

    goals = brain_root / "goals"
    if goals.is_dir():
        for path in sorted(goals.glob("*.md")):
            if path.name == "_meta.md" or not _inside(path):
                continue
            parsed = MemoryAdapter._parse_md_common(path)
            if parsed is None:
                continue
            frontmatter, _body, _start_line = parsed
            status = str(frontmatter.get("status") or "").upper()
            if frontmatter.get("type") == "goal" and status not in _ACTIVE_GOAL_STATUSES_EXCLUDED:
                yield "goal", path

    adrs = brain_root / "docs" / "adr"
    if adrs.is_dir():
        for path in sorted(adrs.glob("ADR-*.md")):
            if not _inside(path):
                continue
            parsed = MemoryAdapter._parse_md_common(path)
            if parsed is None:
                continue
            frontmatter, _body, _start_line = parsed
            if str(frontmatter.get("status") or "").casefold() in _LIVE_ADR_STATUSES:
                yield "adr", path


class MemoryAdapter(BackendAdapter):
    name = "memory"

    def _load_and_validate_contract(self) -> str | None:
        """Load the Brain corpus contract when present and fail on drift.

        Standalone yohan-mcp fixtures without a Brain checkout retain the
        legacy compiled corpus. A real Brain that publishes the contract must
        provide every P0 exact file and match the compiled R1 implementation.
        """
        brain_root = resolve_brain_root(self.base)
        contract_path = self.base / "core" / "retrieval-contract.yaml"
        if brain_root is None:
            return None
        if not contract_path.is_file():
            # A Brain that already publishes the ecosystem contract is on the
            # truth-plane contract path; silently falling back would turn a
            # deleted retrieval manifest into a legacy success.
            if (self.base / "core" / "ecosystem-contract.yaml").is_file():
                raise RuntimeError("memory_contract_invalid:missing_manifest")
            return None
        try:
            contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise RuntimeError(
                f"memory_contract_invalid:unreadable:{type(exc).__name__}"
            ) from exc
        if not isinstance(contract, dict):
            raise RuntimeError("memory_contract_invalid:not_a_mapping")
        if contract.get("schema") != "brain-retrieval-contract":
            raise RuntimeError("memory_contract_invalid:schema")
        if contract.get("status") != "active":
            raise RuntimeError("memory_contract_invalid:not_active")
        lexical = (contract.get("retrieval") or {}).get("lexical") or {}
        if lexical.get("required") is not True:
            raise RuntimeError("memory_contract_invalid:lexical_not_required")
        if lexical.get("request_time_recursive_scan") != "forbidden":
            raise RuntimeError("memory_contract_invalid:recursive_scan_policy")

        corpus = contract.get("corpus") or {}
        exact = set((corpus.get("P0") or {}).get("exact_files") or [])
        compiled = {
            *_BRAIN_ROOT_SOT_FILES,
            *(f"memory/{name}" for name in _BRAIN_STATE_FILES),
            *(f"memory/core/{name}" for name in _BRAIN_CORE_FILES),
        }
        if exact != compiled:
            raise RuntimeError("memory_contract_invalid:p0_compiled_drift")
        brain_resolved = brain_root.resolve()
        missing: list[str] = []
        for rel in sorted(exact):
            path = brain_root / rel
            try:
                inside = path.resolve().is_relative_to(brain_resolved)
            except OSError:
                inside = False
            if not inside:
                raise RuntimeError(f"memory_contract_invalid:path_escape:{rel}")
            if not path.is_file():
                missing.append(rel)
        if missing:
            raise RuntimeError(
                "memory_contract_invalid:missing_p0:" + ",".join(missing)
            )
        version = str(contract.get("version") or "").strip()
        if not version:
            raise RuntimeError("memory_contract_invalid:version")
        return version

    def __init__(self, base_dir: str | os.PathLike | None = None) -> None:
        self.base = Path(base_dir) if base_dir else resolve_memory_dir()
        # brain .md parsing cache: path -> ((mtime_ns, size, ctime_ns), data).
        self._md_cache: dict[str, tuple[tuple[int, int, int], dict]] = {}
        self._corpus_contract_version = self._load_and_validate_contract()
        self._refresh_lock = threading.RLock()
        self._generation_lock = threading.RLock()
        self._generation = _SearchGeneration(
            rows=(),
            token_postings=MappingProxyType({}),
            priority_row_count=0,
            indexed_files=(),
            watched_directories=(),
            generation_id="",
            generated_at="",
            corpus_contract_version=self._corpus_contract_version,
            manifest_documents=(),
            revision="",
            build_ms=0.0,
            file_reads=0,
            invalid_priority_sources=(),
        )
        # Filesystem discovery happens once at adapter startup, never per query.
        self.refresh_search_index()

    def _capture_generation(self) -> _SearchGeneration:
        """Capture one complete immutable search generation under the swap lock."""
        with self._generation_lock:
            return self._generation

    # Compatibility properties for existing diagnostics/tests.  Each property
    # reads from one generation; query execution itself captures exactly once.
    @property
    def _index_revision(self) -> str:
        return self._capture_generation().revision

    @property
    def _search_rows(self) -> tuple[_SearchRow, ...]:
        return self._capture_generation().rows

    @property
    def _index_manifest(self) -> dict:
        generation = self._capture_generation()
        return {
            "generation_id": generation.generation_id,
            "generated_at": generation.generated_at,
            "source_revision": generation.revision,
            "corpus_contract_version": generation.corpus_contract_version,
            "documents": [
                {
                    "document_id": item.document_id,
                    "path": item.path,
                    "content_hash": item.content_hash,
                    "priority": item.priority,
                    "kind": item.kind,
                    "indexed_at": item.indexed_at,
                }
                for item in generation.manifest_documents
            ],
        }

    # ── 경로 헬퍼 ───────────────────────────────────────────────
    @staticmethod
    def _safe_id(id_: str) -> str:
        """파일명에 쓰기 전 id 봉쇄 검증 — 경로 구분자/'..'/절대경로/드라이브 거부."""
        s = str(id_)
        if s in ("", ".", "..") or any(c in s for c in "/\\:\x00") or os.path.isabs(s):
            raise ValueError(f"잘못된 id(경로 탈출 차단): {id_!r}")
        return s

    def _dir_for(self, type_: str) -> Path:
        sub, single = _LAYOUT[type_]
        return self.base if single else self.base / sub

    def _path_for(self, type_: str, id_: str) -> Path:
        sub, single = _LAYOUT[type_]
        if single:
            return self.base / sub
        p = self.base / sub / f"{self._safe_id(id_)}.yaml"
        # 이중 방어: resolve 후 base 하위인지 재확인
        if not p.resolve().is_relative_to(self.base.resolve()):
            raise ValueError(f"base 디렉토리 탈출 차단: {id_!r}")
        return p

    def _id_of(self, type_: str, data: dict) -> str:
        return str(data.get(_ID_FIELD.get(type_, "id"), ""))

    # ── 읽기/검색 ───────────────────────────────────────────────
    def _read_yaml(self, path: Path) -> dict | None:
        """단일 yaml → dict. 손상/비dict 파일은 None — 해당 파일만 skip(graceful).

        memory/ 는 사람이 손편집하는 SoT 라 깨진 YAML 이 생긴다. 여기서 YAMLError·비dict 를
        흡수하지 않으면 파일 1개가 _iter_all→search 전체를 죽여 백엔드 회수가 0이 된다(격리 실패).
        """
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            data = yaml.safe_load(content) or {}
        except yaml.YAMLError as exc:
            logger.warning("손상 YAML skip: %s: %s: %s", path, type(exc).__name__, exc)
            return None
        if not isinstance(data, dict):
            logger.warning("비dict YAML skip(%s): %s", type(data).__name__, path)
            return None
        return data

    @staticmethod
    def _parse_md_common(path: Path) -> tuple[dict, str, int] | None:
        """brain `.md`+frontmatter 공통 파서 — (frontmatter, 미절단 본문, 본문시작줄) 반환.

        `_read_md`/`_read_md_uncapped` 가 공유하는 내부 헬퍼(공개 계약 아님 — 반환 shape 자유).
        본문시작줄은 **원본 파일 기준 1-indexed** 줄번호로, frontmatter 블록 길이 +
        본문 선두 공백줄(strip 으로 사라질 줄)을 반영한다 — 청킹 역링크(chunk_start_line)가
        본문만 넘겨받고도 파일 기준 정확한 줄을 가리키게 하기 위함(core/chunking.py base_line).
        """
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return None
        fm: dict = {}
        body = text
        body_start_line = 1
        lines = text.split("\n")
        if lines and lines[0].strip() == "---":
            for i in range(1, len(lines)):
                if lines[i].strip() == "---":
                    try:
                        loaded = yaml.safe_load("\n".join(lines[1:i]))
                    except Exception:
                        loaded = None  # 깨진 frontmatter
                    # dict 일 때만 frontmatter 로 인정 → 본문 첫 줄이 우연히 '---'(수평선)여도
                    # 본문을 잘라먹거나 스칼라/list 를 필드로 오주입하지 않는다.
                    if isinstance(loaded, dict):
                        fm = loaded
                        body = "\n".join(lines[i + 1 :])
                        body_start_line = i + 2  # 닫는 --- 다음 줄(0-idx i+1)의 1-idx 파일 줄번호
                    break
        leading_blank = 0
        for ln in body.split("\n"):
            if ln.strip():
                break
            leading_blank += 1
        return fm, body.strip(), body_start_line + leading_blank

    @staticmethod
    def _read_md(path: Path) -> dict | None:
        """brain `.md`+frontmatter → 레코드 dict. 선두 `---` 블록=frontmatter, 나머지=body.

        frontmatter 없으면 {"body": 전문}. 깨진 frontmatter 는 무시하고 본문은 살린다(graceful).
        회수 봉투용 — 본문은 `_MD_BODY_MAX` 로 절단(토큰 폭증 방지). 전문이 필요하면
        `_read_md_uncapped` 를 쓴다(브레인 벡터 시딩 전용, 이 메서드는 불변 유지).
        """
        parsed = MemoryAdapter._parse_md_common(path)
        if parsed is None:
            return None
        fm, body, _start_line = parsed
        rec = dict(fm)
        rec["body"] = body[:_MD_BODY_MAX]  # 상한(토큰 폭증 방지)
        return rec

    @staticmethod
    def _read_md_uncapped(path: Path) -> dict | None:
        """brain_memory 벡터 시딩 전용(scripts/seed_brain_memory.py) — 본문 상한 없이 전문 반환.

        `_read_md` 와 동일 파싱이되 `_MD_BODY_MAX` 절단을 적용하지 않는다(회수 경로 `_read_md`
        는 완전히 불변). 긴 본문은 청킹(core/chunking.py)이 안전한 크기로 나누므로 여기서
        미리 자를 필요가 없다. 추가로 `_body_start_line`(원본 파일 1-indexed 줄번호, 본문의
        첫 줄이 파일의 몇 번째 줄인지)을 반환해 청크 역링크 계산에 쓸 수 있게 한다.
        """
        parsed = MemoryAdapter._parse_md_common(path)
        if parsed is None:
            return None
        fm, body, start_line = parsed
        rec = dict(fm)
        rec["body"] = body
        rec["_body_start_line"] = start_line
        return rec

    def _read_md_cached(self, path: Path) -> dict | None:
        """mtime 기반 캐시 — 변화 없으면 재파싱하지 않는다."""
        try:
            stat = path.stat()
        except OSError:
            return None
        signature = (stat.st_mtime_ns, stat.st_size, stat.st_ctime_ns)
        key = str(path)
        cached = self._md_cache.get(key)
        if cached is not None and cached[0] == signature:
            return cached[1]
        data = self._read_md(path)
        if data is not None:
            self._md_cache[key] = (signature, data)
        return data

    def _iter_source_records(
        self,
        statuses: dict[str, str] | None = None,
        observed_stats: dict[str, tuple[int, int, int]] | None = None,
    ):
        """Discover source records for an explicit index refresh.

        ``statuses`` is populated for every path that was actually inspected,
        including invalid candidates which cannot produce a search row.
        """
        statuses = statuses if statuses is not None else {}
        observed_stats = observed_stats if observed_stats is not None else {}

        def _key(path: Path) -> str:
            return str(path.resolve())

        def _yaml(path: Path) -> dict | None:
            before = path.stat()
            data = self._read_yaml(path)
            after = path.stat()
            before_sig = (before.st_mtime_ns, before.st_size, before.st_ctime_ns)
            after_sig = (after.st_mtime_ns, after.st_size, after.st_ctime_ns)
            if before_sig != after_sig:
                raise RuntimeError(f"memory_index_build_race:source_changed:{path}")
            observed_stats[_key(path)] = after_sig
            statuses[_key(path)] = "indexed" if data is not None else "invalid_or_unreadable"
            return data

        def _md(path: Path) -> dict | None:
            before = path.stat()
            data = self._read_md_cached(path)
            after = path.stat()
            before_sig = (before.st_mtime_ns, before.st_size, before.st_ctime_ns)
            after_sig = (after.st_mtime_ns, after.st_size, after.st_ctime_ns)
            if before_sig != after_sig:
                raise RuntimeError(f"memory_index_build_race:source_changed:{path}")
            observed_stats[_key(path)] = after_sig
            statuses[_key(path)] = "indexed" if data is not None else "invalid_or_unreadable"
            return data

        yielded_paths: set[str] = set()

        def _record(type_: str, id_: str, data: dict, path: Path):
            yielded_paths.add(_key(path))
            return type_, str(id_), data, path

        for type_, (sub, single) in _LAYOUT.items():
            if single:
                p = self.base / sub
                if p.exists():
                    data = _yaml(p)
                    if data is not None:
                        yield _record(type_, self._id_of(type_, data), data, p)
            else:
                d = self.base / sub
                if d.exists():
                    for p in sorted(d.glob("*.yaml")):
                        data = _yaml(p)
                        if data is not None:
                            yield _record(type_, data.get(_ID_FIELD[type_], p.stem), data, p)
        # ── ADR-008 B.3 — brain 지식 폴더의 .md (읽기 전용, 스키마 없는 회수 타입) ──
        base_resolved = self.base.resolve()
        brain_root = resolve_brain_root(self.base)
        memory_doc_dirs = [
            *((kdir, kdir, "P1" if kdir == "rules" else "P2") for kdir in _BRAIN_KNOWLEDGE_DIRS),
            *_BRAIN_NESTED_DOC_DIRS,
        ]
        for rel_dir, kind, default_tier in memory_doc_dirs:
            root = self.base / rel_dir
            if not root.exists():
                continue
            for p in sorted(root.rglob("*.md")):
                # 경로 봉쇄 재확인(심링크 등으로 base 밖 탈출 차단)
                if not p.resolve().is_relative_to(base_resolved):
                    continue
                data = _md(p)
                if data is None:
                    continue
                rec = dict(data)
                memory_rel = str(p.relative_to(self.base)).replace("\\", "/")
                rel = str(
                    p.relative_to(brain_root) if brain_root else p.relative_to(self.base)
                ).replace("\\", "/")
                rec["_path"] = rel
                rec["_retrieval_tier"] = _BRAIN_EXACT_TIER_OVERRIDES.get(memory_rel, default_tier)
                # id 는 frontmatter id/slug, 없으면 **상대경로**(유니크) — 파일 stem 은 중첩 폴더
                # 동명 파일(wiki/2024/intro.md·wiki/2025/intro.md)에서 type::id 충돌→RRF dedup 소실.
                id_ = str(data.get("id") or data.get("slug") or rel)
                # 회수 타입 = brain:<folder>. backend 는 여전히 "memory"(make_record). 스키마 없는
                # 논리 회수 타입이라 검증 스킵·_links 관계 없음(지식노트는 관계그래프 밖 — 정상).
                yield _record(f"brain:{kind}", id_, rec, p)

        for kind, path in iter_priority_brain_files(self.base):
            if _key(path) in yielded_paths:
                continue
            if path.suffix.casefold() in {".yaml", ".yml"}:
                data = _yaml(path)
            else:
                data = _md(path)
            if data is None:
                continue
            rec = dict(data)
            rel = str(path.relative_to(brain_root)).replace("\\", "/") if brain_root else path.name
            rec["_path"] = rel
            rec["_retrieval_tier"] = "P0"
            if kind == "goal":
                rec["_goal_status"] = str(data.get("status") or "")
            id_ = str(data.get("id") or data.get("slug") or rel)
            yield _record(f"brain:{kind}", id_, rec, path)

        if brain_root:
            brain_resolved = brain_root.resolve()
            for rel_dir, kind, tier in _BRAIN_REPO_DOC_DIRS:
                root = brain_root / rel_dir
                if not root.is_dir():
                    continue
                for path in sorted(root.rglob("*.md")):
                    try:
                        if not path.resolve().is_relative_to(brain_resolved):
                            continue
                    except OSError:
                        continue
                    data = _md(path)
                    if data is None:
                        continue
                    rec = dict(data)
                    rel = str(path.relative_to(brain_root)).replace("\\", "/")
                    rec["_path"] = rel
                    rec["_retrieval_tier"] = tier
                    id_ = str(data.get("id") or data.get("slug") or rel)
                    yield _record(f"brain:{kind}", id_, rec, path)

        # Terminal Goals, non-live ADRs and corrupt candidates do not yield a
        # row, but they remain fingerprinted so a repair/status change can
        # invalidate and recover the next complete generation.
        for path in self._discover_candidate_paths():
            key = _key(path)
            if key in statuses:
                continue
            if path.suffix.casefold() in {".yaml", ".yml"}:
                data = _yaml(path)
                if data is not None:
                    statuses[key] = "excluded"
            else:
                data = _md(path)
                if data is not None:
                    brain_root = resolve_brain_root(self.base)
                    rel = (
                        str(path.relative_to(brain_root)).replace("\\", "/")
                        if brain_root and path.is_relative_to(brain_root)
                        else ""
                    )
                    if rel.startswith("goals/") and path.name != "_meta.md" and data.get("type") != "goal":
                        statuses[key] = "invalid_or_unreadable"
                    elif rel.startswith("docs/adr/") and not data.get("status"):
                        statuses[key] = "invalid_or_unreadable"
                    else:
                        statuses[key] = "excluded"

    def _discover_candidate_paths(self) -> tuple[Path, ...]:
        """Enumerate the bounded corpus during generation build, never query."""
        paths: set[Path] = set()
        for _type, (sub, single) in _LAYOUT.items():
            root = self.base / sub
            if single:
                if root.is_file():
                    paths.add(root)
            elif root.is_dir():
                paths.update(root.glob("*.yaml"))
        for rel_dir, _kind, _tier in [
            *((kdir, kdir, "P1" if kdir == "rules" else "P2") for kdir in _BRAIN_KNOWLEDGE_DIRS),
            *_BRAIN_NESTED_DOC_DIRS,
        ]:
            root = self.base / rel_dir
            if root.is_dir():
                paths.update(root.rglob("*.md"))

        brain_root = resolve_brain_root(self.base)
        if brain_root:
            for name in _BRAIN_ROOT_SOT_FILES:
                path = brain_root / name
                if path.is_file():
                    paths.add(path)
            for name in _BRAIN_STATE_FILES:
                path = self.base / name
                if path.is_file():
                    paths.add(path)
            core = self.base / "core"
            paths.update(path for name in _BRAIN_CORE_FILES if (path := core / name).is_file())
            goals = brain_root / "goals"
            if goals.is_dir():
                paths.update(goals.glob("*.md"))
            adrs = brain_root / "docs" / "adr"
            if adrs.is_dir():
                paths.update(adrs.glob("ADR-*.md"))
            for rel_dir, _kind, _tier in _BRAIN_REPO_DOC_DIRS:
                root = brain_root / rel_dir
                if root.is_dir():
                    paths.update(root.rglob("*.md"))
        return tuple(sorted(paths, key=lambda path: str(path).casefold()))

    @staticmethod
    def _tokens(value: str) -> set[str]:
        return {token.casefold() for token in _TOKEN_RE.findall(value or "") if token}

    def _build_search_generation(self) -> _SearchGeneration:
        """Build one complete generation without mutating the active one."""
        started = perf_counter()
        rows: list[_SearchRow] = []
        postings: dict[str, set[int]] = {}
        priority_count = 0
        indexed_files: list[_SourceFingerprint] = []
        manifest_documents: list[_ManifestDocument] = []
        invalid_priority_sources: list[str] = []
        watched_directories: dict[str, int] = {}
        statuses: dict[str, str] = {}
        observed_stats: dict[str, tuple[int, int, int]] = {}
        record_metadata: dict[str, tuple[str, str]] = {}
        generated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        revision = hashlib.sha256()
        for type_, id_, data, source_path in self._iter_source_records(statuses, observed_stats):
            blob = self._match_blob(data)
            tier = str(data.get("_retrieval_tier") or "P2")
            record_metadata[str(source_path.resolve())] = (tier, str(type_))
            priority = {"P0": 3, "P1": 3, "P2": 1}.get(tier, 1)
            if type_ in {"brain:root", "brain:core", "brain:state"}:
                priority = 5
            elif type_ == "brain:goal":
                priority = 4
            index = len(rows)
            tokens = frozenset(self._tokens(blob))
            rel = str(data.get("_path") or "").replace("\\", "/")
            rows.append(_SearchRow(
                type=str(type_),
                id=str(id_),
                data=deepcopy(data),
                blob=blob,
                tokens=tokens,
                priority=priority,
                evaluation_contract=rel == _EVALUATION_CONTRACT_PATH,
            ))
            for token in tokens:
                postings.setdefault(token, set()).add(index)
            if tier == "P0":
                priority_count += 1
            revision.update(type_.encode("utf-8"))
            revision.update(b"\0")
            revision.update(str(id_).encode("utf-8"))
            revision.update(b"\0")
            revision.update(blob.encode("utf-8"))
            revision.update(b"\0")

        # Snapshot fingerprints are checked with stat only. Query execution
        # never opens source files and never enumerates a directory.
        source_paths = self._discover_candidate_paths()
        brain_root = resolve_brain_root(self.base)
        priority_candidates: set[str] = set()
        if brain_root:
            priority_candidates.update(str((brain_root / name).resolve()) for name in _BRAIN_ROOT_SOT_FILES)
            priority_candidates.update(str((self.base / name).resolve()) for name in _BRAIN_STATE_FILES)
            priority_candidates.update(str((self.base / "core" / name).resolve()) for name in _BRAIN_CORE_FILES)
            priority_candidates.update(
                str(path.resolve())
                for path in (brain_root / "goals").glob("*.md")
                if path.name != "_meta.md"
            )
            priority_candidates.update(str(path.resolve()) for path in (brain_root / "docs" / "adr").glob("ADR-*.md"))
        for path in source_paths:
            try:
                before = path.stat()
                raw = path.read_bytes()
                after = path.stat()
            except OSError:
                continue
            before_sig = (before.st_mtime_ns, before.st_size, before.st_ctime_ns)
            after_sig = (after.st_mtime_ns, after.st_size, after.st_ctime_ns)
            if before_sig != after_sig:
                raise RuntimeError(f"memory_index_build_race:source_changed:{path}")
            stat = after
            raw_path = str(path.resolve())
            observed = observed_stats.get(raw_path)
            if observed is not None and observed != after_sig:
                raise RuntimeError(f"memory_index_build_race:source_changed:{path}")
            status = statuses.get(raw_path, "candidate_unread")
            normalized = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
            content_hash = hashlib.sha256(normalized).hexdigest()
            if brain_root and path.resolve().is_relative_to(brain_root.resolve()):
                rel = path.resolve().relative_to(brain_root.resolve()).as_posix()
                document_id = f"brain:{rel}"
            else:
                rel = path.resolve().relative_to(self.base.resolve()).as_posix()
                document_id = f"memory:{rel}"
            indexed_files.append(_SourceFingerprint(
                path=raw_path,
                mtime_ns=stat.st_mtime_ns,
                size=stat.st_size,
                ctime_ns=stat.st_ctime_ns,
                content_hash=content_hash,
                status=status,
            ))
            revision.update(rel.encode("utf-8"))
            revision.update(b"\0")
            revision.update(content_hash.encode("ascii"))
            revision.update(b"\0")
            metadata = record_metadata.get(raw_path)
            if status == "indexed" and metadata is not None:
                tier, kind = metadata
                manifest_documents.append(_ManifestDocument(
                    document_id=document_id,
                    path=rel,
                    content_hash=content_hash,
                    priority=tier,
                    kind=kind,
                    indexed_at=generated_at,
                ))
            if raw_path in priority_candidates and status in {"invalid_or_unreadable", "candidate_unread"}:
                invalid_priority_sources.append(raw_path)
            parent = path.parent.resolve()
            # Do not watch the memory root itself: runtime journals or other
            # out-of-corpus children may legitimately appear beside the
            # allowlist directories. Watch only source-bearing descendants.
            while parent != self.base.resolve() and parent.is_relative_to(self.base.resolve()):
                try:
                    watched_directories[str(parent)] = parent.stat().st_mtime_ns
                except OSError:
                    break
                parent = parent.parent
        if brain_root:
            for directory in (
                self.base / "core",
                brain_root / "goals",
                brain_root / "docs" / "adr",
                *(brain_root / rel for rel, _kind, _tier in _BRAIN_REPO_DOC_DIRS),
            ):
                if directory.is_dir():
                    watched_directories[str(directory.resolve())] = directory.stat().st_mtime_ns
        # Watch empty allowlist/layout directories too so a newly added file is
        # detected even when no indexed source existed there at refresh time.
        discovery_roots = [self.base / name for name in _BRAIN_KNOWLEDGE_DIRS]
        discovery_roots.extend(self.base / rel for rel, _kind, _tier in _BRAIN_NESTED_DOC_DIRS)
        discovery_roots.extend(
            self.base / sub for sub, single in _LAYOUT.values() if not single
        )
        for root in discovery_roots:
            if not root.is_dir():
                continue
            directories = [root, *(path for path in root.rglob("*") if path.is_dir())]
            for directory in directories:
                try:
                    watched_directories[str(directory.resolve())] = directory.stat().st_mtime_ns
                except OSError:
                    continue
        manifest_by_path = {item.path: item for item in manifest_documents}
        for row in rows:
            locator = str(row.data.get("_path") or "").replace("\\", "/")
            evidence = manifest_by_path.get(locator)
            if evidence is not None:
                row.data["_retrieval_document_id"] = evidence.document_id
                row.data["_retrieval_content_hash"] = evidence.content_hash
                row.data["_retrieval_locator"] = evidence.path
        source_revision = revision.hexdigest()
        generation_id = hashlib.sha256(
            f"{self._corpus_contract_version or 'legacy'}\0{source_revision}".encode("utf-8")
        ).hexdigest()
        return _SearchGeneration(
            rows=tuple(rows),
            token_postings=MappingProxyType({
                token: frozenset(indices) for token, indices in postings.items()
            }),
            priority_row_count=priority_count,
            indexed_files=tuple(indexed_files),
            watched_directories=tuple(sorted(watched_directories.items())),
            generation_id=generation_id,
            generated_at=generated_at,
            corpus_contract_version=self._corpus_contract_version,
            manifest_documents=tuple(manifest_documents),
            revision=source_revision,
            build_ms=round((perf_counter() - started) * 1000, 1),
            file_reads=len(source_paths),
            invalid_priority_sources=tuple(sorted(invalid_priority_sources)),
        )

    def refresh_search_index(self) -> None:
        """Build a complete immutable generation, then atomically swap it."""
        # Serialize builds as well as swaps. Otherwise an older slow build can
        # finish after a newer one and overwrite the newer generation.
        with self._refresh_lock:
            generation = self._build_search_generation()
            with self._generation_lock:
                self._generation = generation

    @staticmethod
    def _freshness(generation: _SearchGeneration) -> tuple[bool, str, int]:
        """Check add/edit/delete drift using stat only; stale snapshots fail loud."""
        stat_count = 0
        for fingerprint in generation.indexed_files:
            path = Path(fingerprint.path)
            try:
                stat = path.stat()
                stat_count += 1
            except OSError:
                return False, "source_deleted", stat_count
            if (stat.st_mtime_ns, stat.st_size, stat.st_ctime_ns) != (
                fingerprint.mtime_ns,
                fingerprint.size,
                fingerprint.ctime_ns,
            ):
                return False, "source_edited", stat_count
        if generation.invalid_priority_sources:
            return False, "priority_source_invalid", stat_count
        for raw_path, mtime_ns in generation.watched_directories:
            path = Path(raw_path)
            try:
                current = path.stat().st_mtime_ns
                stat_count += 1
            except OSError:
                return False, "directory_deleted", stat_count
            if current != mtime_ns:
                return False, "directory_membership_changed", stat_count
        return True, "fresh", stat_count

    def _iter_all(self):
        """Yield the startup snapshot without filesystem traversal."""
        generation = self._capture_generation()
        for row in generation.rows:
            yield row.type, row.id, deepcopy(row.data)

    def _query_tokens(self, query: str) -> set[str]:
        tokens: set[str] = set()
        for raw_token in self._tokens(query):
            token = raw_token
            for suffix in _KOREAN_QUERY_SUFFIXES:
                if token.endswith(suffix) and len(token) > len(suffix) + 1:
                    token = token[:-len(suffix)]
                    break
            tokens.add(token)
        meaningful = {token for token in tokens if token not in _QUERY_STOPWORDS}
        meaningful = meaningful or tokens
        expanded = set(meaningful)
        for token in meaningful:
            expanded.update(_LEXICAL_QUERY_EXPANSIONS.get(token, ()))
        return expanded

    def _matching_postings(
        self, query: str, generation: _SearchGeneration
    ) -> dict[str, frozenset[int]]:
        tokens = self._query_tokens(query)
        if not tokens:
            return {}
        matches: dict[str, frozenset[int]] = {}
        for query_token in tokens:
            candidates = set(generation.token_postings.get(query_token, ()))
            if not candidates or any("가" <= char <= "힣" for char in query_token):
                # Only fall back to the vocabulary scan when an exact token is
                # absent. This keeps Korean particle/legacy substring support
                # without paying O(query tokens × vocabulary) for normal terms.
                for indexed_token, rows in generation.token_postings.items():
                    if query_token in indexed_token or indexed_token in query_token:
                        candidates.update(rows)
            matches[query_token] = frozenset(candidates)
        return matches

    def _candidate_indices(
        self,
        query: str,
        generation: _SearchGeneration,
        matching_postings: Mapping[str, frozenset[int]] | None = None,
    ) -> list[int]:
        tokens = self._query_tokens(query)
        if not tokens:
            return list(range(len(generation.rows)))
        matches = matching_postings or self._matching_postings(query, generation)
        counts: dict[int, int] = {}
        for rows in matches.values():
            for index in rows:
                counts[index] = counts.get(index, 0) + 1
        candidates = set(counts)
        if not candidates:
            # Preserve arbitrary substring compatibility when token boundaries
            # cannot produce a safe candidate set.
            return list(range(len(generation.rows)))
        # Multi-term questions target records with corroborating lexical
        # evidence. If that would empty the pool, retain union compatibility.
        if len(tokens) >= 3:
            corroborated = {index for index, count in counts.items() if count >= 2}
            if corroborated:
                candidates = corroborated
        return sorted(candidates)

    @staticmethod
    def _match_blob(data: dict) -> str:
        """substring 매칭용 blob — 레코드의 키·스칼라 값을 **원문 그대로** 개행 결합(소문자).

        과거엔 yaml.safe_dump 재직렬화 출력을 blob 으로 썼는데, emitter 출력 포매팅이
        원문을 변형해 파일에 실재하는 질의가 무음 실패했다(YOHA-4):
        - 기본 width=80 접기(fold)가 긴 줄의 공백을 개행+들여쓰기로 치환
          → 접점에 걸친 다어절 질의 0건 + 빈도점수 왜곡.
        - 따옴표 스타일 이스케이프(작은따옴표 이중화 '' 등)가 문자 자체를 변형.
        값 문자열을 직접 결합하면 이 직렬화 부작용 클래스 전체가 사라진다.
        개행 결합은 서로 다른 값이 이어붙어 가짜 인접 매칭이 생기는 것을 막는다.
        """
        parts: list[str] = []

        def walk(v) -> None:
            if isinstance(v, dict):
                for k, val in v.items():
                    parts.append(str(k))
                    walk(val)
            elif isinstance(v, (list, tuple, set)):
                for item in v:
                    walk(item)
            elif v is not None:
                parts.append(str(v))

        walk(data)
        return "\n".join(parts).lower()

    @staticmethod
    def _token_matches(query_token: str, document_token: str) -> bool:
        return query_token in document_token or document_token in query_token

    def _score_row(
        self,
        query: str,
        row: _SearchRow,
        row_index: int,
        generation: _SearchGeneration,
        matching_postings: Mapping[str, frozenset[int]],
        live_max_ids: tuple[int, int],
    ) -> tuple[float, int]:
        """Score non-contiguous lexical evidence with Korean particle tolerance."""
        query_tokens = self._query_tokens(query)
        if not query_tokens:
            return 1.0, 0
        matched = {
            query_token for query_token in query_tokens
            if row_index in matching_postings.get(query_token, ())
        }
        phrase_count = row.blob.count(query) if query else 0
        if not matched and phrase_count == 0:
            return 0.0, 0
        coverage = len(matched) / len(query_tokens)
        path_tokens = self._tokens(str(row.data.get("_path") or ""))
        path_matches = sum(
            1
            for query_token in query_tokens
            if any(self._token_matches(query_token, token) for token in path_tokens)
        )
        identity_tokens = self._tokens(" ".join(
            str(row.data.get(key) or "")
            for key in ("id", "title", "name", "aliases", "_path")
        ))
        identity_matches = sum(
            1
            for query_token in query_tokens
            if any(self._token_matches(query_token, token) for token in identity_tokens)
        )
        lexical_weight = sum(
            math.log((len(generation.rows) + 1) / (len(matching_postings[token]) + 1)) + 1.0
            for token in matched
        )
        path_weight = path_matches * 32.0
        if row.type in {"brain:root", "brain:core", "brain:state"}:
            path_weight += path_matches * 60.0
        active_work_query = bool(
            {"active", "in_progress"} & query_tokens
            and {"goal", "goals"} & query_tokens
        )
        live_recency = 0.0
        if active_work_query and row.type in {"brain:goal", "brain:adr"}:
            match = re.search(r"\d+", row.id)
            if match:
                numeric_id = int(match.group())
                max_id = live_max_ids[0] if row.type == "brain:goal" else live_max_ids[1]
                ceiling = 80.0
                live_recency = ceiling / (max(0, max_id - numeric_id) + 1)
        score = (
            lexical_weight * 4.0
            + coverage * 4.0
            + min(phrase_count, 3) * 3.0
            + path_weight
            + identity_matches * 18.0
            + row.priority * 8.0
            + live_recency
        )
        return score, len(matched)

    async def search(self, query: str, opts: dict | None = None) -> list[dict]:
        """Search one captured lexical generation; no query-time traversal/open."""
        opts = opts or {}
        want_type = opts.get("type")
        q = (query or "").casefold()
        diagnostics = opts.get("_search_diagnostics")
        diagnostics = diagnostics if isinstance(diagnostics, dict) else None
        query_started = perf_counter()
        generation = self._capture_generation()
        fresh, reason_code, stat_count = self._freshness(generation)
        if not fresh:
            if diagnostics is not None:
                diagnostics.update({
                    "index_mode": "startup_snapshot",
                    "index_revision": generation.revision,
                    "index_generation_id": generation.generation_id,
                    "corpus_contract_version": generation.corpus_contract_version,
                    "manifest_document_count": len(generation.manifest_documents),
                    "fresh": False,
                    "freshness_reason_code": reason_code,
                    "stat_checks": stat_count,
                    "source_open_count": 0,
                    "recursive_traversals": 0,
                    "query_filesystem_complexity": "O(N corpus paths + watched directories)",
                    "invalid_priority_sources": list(generation.invalid_priority_sources),
                })
            raise RuntimeError(
                f"memory_index_stale:{reason_code}; restart server or call refresh_search_index()"
            )
        matching_postings = self._matching_postings(q, generation)
        candidate_indices = self._candidate_indices(q, generation, matching_postings)
        goal_ids = [
            int(match.group())
            for row in generation.rows if row.type == "brain:goal"
            if (match := re.search(r"\d+", row.id))
        ]
        adr_ids = [
            int(match.group())
            for row in generation.rows if row.type == "brain:adr"
            if (match := re.search(r"\d+", row.id))
        ]
        live_max_ids = (max(goal_ids, default=0), max(adr_ids, default=0))
        hits: list[tuple[float, int, int, str, dict]] = []
        for index in candidate_indices:
            row = generation.rows[index]
            if want_type and row.type != want_type:
                continue
            if row.evaluation_contract and opts.get("exclude_evaluation_contract"):
                continue
            # 빈 쿼리는 전건 매칭인데, brain 지식노트(수백 건)가 쏟아지면 후보풀을 삼켜
            # 다른 백엔드를 밀어낸다 → 빈 쿼리일 때 brain:* 는 제외(yaml 엔티티만 나열).
            if not q and row.type.startswith("brain:"):
                continue
            score, matched_count = self._score_row(
                q, row, index, generation, matching_postings, live_max_ids
            )
            if score > 0:
                record_data = deepcopy(row.data)
                document_id = record_data.pop("_retrieval_document_id", None)
                content_hash = record_data.pop("_retrieval_content_hash", None)
                locator = record_data.pop("_retrieval_locator", None)
                record = make_record(row.id, row.type, self.name, record_data, score=score)
                if document_id and content_hash and locator:
                    record["evidence_ref"] = {
                        "document_id": document_id,
                        "content_hash": content_hash,
                        "locator": locator,
                    }
                hits.append(
                    (score, matched_count, row.priority, row.id,
                     record)
                )
        hits.sort(key=lambda item: (-item[0], -item[1], -item[2], item[3]))
        if diagnostics is not None:
            diagnostics.update({
                "index_mode": "startup_snapshot",
                "indexed_records": len(generation.rows),
                "priority_records": generation.priority_row_count,
                "candidate_records": len(candidate_indices),
                "index_revision": generation.revision,
                "index_generation_id": generation.generation_id,
                "corpus_contract_version": generation.corpus_contract_version,
                "manifest_document_count": len(generation.manifest_documents),
                "fresh": True,
                "freshness_reason_code": reason_code,
                "stat_checks": stat_count,
                "index_build_ms": generation.build_ms,
                "query_latency_ms": round((perf_counter() - query_started) * 1000, 1),
                "index_source_count": generation.file_reads,
                "source_open_count": 0,
                "recursive_traversals": 0,
                "query_filesystem_complexity": "O(N corpus paths + watched directories)",
                "invalid_priority_sources": list(generation.invalid_priority_sources),
            })
        return [rec for _score, _matched, _priority, _id, rec in hits]

    # ── 쓰기 ────────────────────────────────────────────────────
    async def create(self, type_: str, data: dict) -> dict:
        if type_ not in _LAYOUT:
            raise ValueError(f"memory 가 모르는 타입: {type_}")
        id_ = self._id_of(type_, data)
        path = self._path_for(type_, id_)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        self.refresh_search_index()
        return make_record(str(id_), type_, self.name, data)

    async def update(self, id_: str, data: dict, type_: str | None = None) -> dict:
        """id_ 로 기존 파일 찾아 부분 병합 후 저장.

        type_ 가 주어지면 그 레이아웃만 조회(검증 타입 = 저장 타입 보장).
        단일파일(profile)은 id 가 실제 PK 와 일치할 때만 매칭해
        다른 엔티티 update 가 profile.yaml 을 오염시키는 것을 막는다.
        """
        if type_ is not None and type_ not in _LAYOUT:
            raise ValueError(f"memory 가 모르는 타입: {type_}")
        candidates = [type_] if type_ is not None else list(_LAYOUT.keys())
        for t in candidates:
            sub, single = _LAYOUT[t]
            path = self._path_for(t, id_)
            if not path.exists():
                continue
            current = self._read_yaml(path)
            if current is None:
                # 손상/비dict YAML 을 {} 로 간주해 병합하면 기존 내용을 조용히 덮어씀(데이터 소실)
                # → 명시적 에러로 사람 복구를 요구한다.
                raise ValueError(f"손상 YAML 로 update 불가(수동 복구 필요): {path}")
            if single and str(current.get(_ID_FIELD[t], "")) != self._safe_id(id_):
                continue  # profile 은 id 일치할 때만 (오염 방지)
            current.update(data)
            path.write_text(
                yaml.safe_dump(current, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            self.refresh_search_index()
            return make_record(str(id_), t, self.name, current)
        raise FileNotFoundError(f"memory 에 id={id_} 엔티티 없음")

    # ── health ──────────────────────────────────────────────────
    async def health_check(self) -> dict:
        with _Timer() as t:
            try:
                self.base.mkdir(parents=True, exist_ok=True)
                probe = self.base / ".health"
                probe.write_text("ok", encoding="utf-8")
                probe.unlink()
                ok, detail = True, f"memory dir 쓰기가능: {self.base}"
            except OSError as exc:
                ok, detail = False, f"memory dir 접근 실패: {exc}"
        return health(ok, t.elapsed_ms, detail)
