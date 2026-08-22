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
import os
import re
from pathlib import Path
from time import perf_counter

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
    "ingest",
    "knowledge-hub",
    "projects",
    "rules",
)
# brain 지식노트 본문 상한 — 회수 봉투 토큰 폭증 방지(전문은 data._path 로 접근).
_MD_BODY_MAX = 4000

# Repository-level sources whose omission made exact retrieval disagree with
# the Brain's own entry contract.  The same iterator is reused by the vector
# seeder so lexical and semantic retrieval do not drift apart.
_BRAIN_ROOT_SOT_FILES = (
    "YOHAN-ECOSYSTEM-SOT.md",
    "ASSETS.md",
    "AGENTS.md",
)
_ACTIVE_GOAL_STATUSES_EXCLUDED = {"DONE"}
_TOKEN_RE = re.compile(r"[0-9A-Za-z가-힣_-]+")


def iter_priority_brain_files(memory_dir: Path):
    """Yield ``(kind, path)`` for root SoTs, core contracts and non-DONE goals.

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

    core = memory_dir / "core"
    if core.is_dir():
        for path in sorted(core.iterdir()):
            if path.is_file() and path.suffix.casefold() in {".md", ".yaml", ".yml"} and _inside(path):
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


class MemoryAdapter(BackendAdapter):
    name = "memory"

    def __init__(self, base_dir: str | os.PathLike | None = None) -> None:
        self.base = Path(base_dir) if base_dir else resolve_memory_dir()
        # brain .md 파싱 캐시 (path → (mtime, data)). 변화 없으면 재읽기·재파싱 회피.
        self._md_cache: dict[str, tuple[float, dict]] = {}
        self._search_rows: list[tuple[str, str, dict, str, int]] = []
        self._token_postings: dict[str, set[int]] = {}
        self._priority_row_count = 0
        self._indexed_files: dict[str, tuple[int, int]] = {}
        self._watched_directories: dict[str, int] = {}
        self._index_revision = ""
        self._index_build_ms = 0.0
        self._index_file_reads = 0
        # Filesystem discovery happens once at adapter startup, never per query.
        self.refresh_search_index()

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
            mtime = path.stat().st_mtime
        except OSError:
            return None
        key = str(path)
        cached = self._md_cache.get(key)
        if cached is not None and cached[0] == mtime:
            return cached[1]
        data = self._read_md(path)
        if data is not None:
            self._md_cache[key] = (mtime, data)
        return data

    def _iter_source_records(self):
        """Discover source records for an explicit index refresh."""
        for type_, (sub, single) in _LAYOUT.items():
            if single:
                p = self.base / sub
                if p.exists():
                    data = self._read_yaml(p)
                    if data is not None:
                        yield type_, self._id_of(type_, data), data
            else:
                d = self.base / sub
                if d.exists():
                    for p in sorted(d.glob("*.yaml")):
                        data = self._read_yaml(p)
                        if data is not None:
                            yield type_, data.get(_ID_FIELD[type_], p.stem), data
        # ── ADR-008 B.3 — brain 지식 폴더의 .md (읽기 전용, 스키마 없는 회수 타입) ──
        base_resolved = self.base.resolve()
        for kdir in _BRAIN_KNOWLEDGE_DIRS:
            root = self.base / kdir
            if not root.exists():
                continue
            for p in sorted(root.rglob("*.md")):
                # 경로 봉쇄 재확인(심링크 등으로 base 밖 탈출 차단)
                if not p.resolve().is_relative_to(base_resolved):
                    continue
                data = self._read_md_cached(p)
                if data is None:
                    continue
                rec = dict(data)
                rel = str(p.relative_to(self.base)).replace("\\", "/")
                rec["_path"] = rel
                # id 는 frontmatter id/slug, 없으면 **상대경로**(유니크) — 파일 stem 은 중첩 폴더
                # 동명 파일(wiki/2024/intro.md·wiki/2025/intro.md)에서 type::id 충돌→RRF dedup 소실.
                id_ = str(data.get("id") or data.get("slug") or rel)
                # 회수 타입 = brain:<folder>. backend 는 여전히 "memory"(make_record). 스키마 없는
                # 논리 회수 타입이라 검증 스킵·_links 관계 없음(지식노트는 관계그래프 밖 — 정상).
                yield f"brain:{kdir}", id_, rec

        brain_root = resolve_brain_root(self.base)
        for kind, path in iter_priority_brain_files(self.base):
            if path.suffix.casefold() in {".yaml", ".yml"}:
                data = self._read_yaml(path)
            else:
                data = self._read_md_cached(path)
            if data is None:
                continue
            rec = dict(data)
            rel = str(path.relative_to(brain_root)).replace("\\", "/") if brain_root else path.name
            rec["_path"] = rel
            rec["_retrieval_tier"] = "priority"
            if kind == "goal":
                rec["_goal_status"] = str(data.get("status") or "")
            id_ = str(data.get("id") or data.get("slug") or rel)
            yield f"brain:{kind}", id_, rec

    @staticmethod
    def _tokens(value: str) -> set[str]:
        return {token.casefold() for token in _TOKEN_RE.findall(value or "") if token}

    def refresh_search_index(self) -> None:
        """Build an in-memory lexical snapshot outside the query happy path."""
        started = perf_counter()
        rows: list[tuple[str, str, dict, str, int]] = []
        postings: dict[str, set[int]] = {}
        priority_count = 0
        indexed_files: dict[str, tuple[int, int]] = {}
        watched_directories: dict[str, int] = {}
        revision = hashlib.sha256()
        for type_, id_, data in self._iter_source_records():
            blob = self._match_blob(data)
            priority = 2 if data.get("_retrieval_tier") == "priority" else 0
            index = len(rows)
            rows.append((type_, str(id_), data, blob, priority))
            for token in self._tokens(blob):
                postings.setdefault(token, set()).add(index)
            if priority:
                priority_count += 1
            revision.update(type_.encode("utf-8"))
            revision.update(b"\0")
            revision.update(str(id_).encode("utf-8"))
            revision.update(b"\0")
            revision.update(blob.encode("utf-8"))
            revision.update(b"\0")

        # Snapshot fingerprints are checked with stat only. Query execution
        # never opens source files and never enumerates a directory.
        source_paths: set[Path] = set()
        for _type, _id, data, _blob, _priority in rows:
            rel = data.get("_path")
            if not rel:
                continue
            brain_root = resolve_brain_root(self.base)
            path = (brain_root / rel) if brain_root and str(rel).startswith(("memory/", "goals/")) else None
            if brain_root and path is None and str(rel) in _BRAIN_ROOT_SOT_FILES:
                path = brain_root / rel
            if path is None:
                path = self.base / str(rel)
            source_paths.add(path)
        # Legacy YAML records do not carry _path; include their explicit layout.
        for _type, (sub, single) in _LAYOUT.items():
            root = self.base / sub
            if single:
                if root.is_file():
                    source_paths.add(root)
            elif root.is_dir():
                source_paths.update(root.glob("*.yaml"))
        for path in source_paths:
            try:
                stat = path.stat()
            except OSError:
                continue
            indexed_files[str(path.resolve())] = (stat.st_mtime_ns, stat.st_size)
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
        brain_root = resolve_brain_root(self.base)
        if brain_root:
            for directory in (self.base / "core", brain_root / "goals"):
                if directory.is_dir():
                    watched_directories[str(directory.resolve())] = directory.stat().st_mtime_ns
        # Watch empty allowlist/layout directories too so a newly added file is
        # detected even when no indexed source existed there at refresh time.
        discovery_roots = [self.base / name for name in _BRAIN_KNOWLEDGE_DIRS]
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
        self._search_rows = rows
        self._token_postings = postings
        self._priority_row_count = priority_count
        self._indexed_files = indexed_files
        self._watched_directories = watched_directories
        self._index_revision = revision.hexdigest()
        self._index_build_ms = round((perf_counter() - started) * 1000, 1)
        self._index_file_reads = len(indexed_files)

    def _freshness(self) -> tuple[bool, str, int]:
        """Check add/edit/delete drift using stat only; stale snapshots fail loud."""
        stat_count = 0
        for raw_path, fingerprint in self._indexed_files.items():
            path = Path(raw_path)
            try:
                stat = path.stat()
                stat_count += 1
            except OSError:
                return False, "source_deleted", stat_count
            if (stat.st_mtime_ns, stat.st_size) != fingerprint:
                return False, "source_edited", stat_count
        for raw_path, mtime_ns in self._watched_directories.items():
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
        for type_, id_, data, _blob, _priority in self._search_rows:
            yield type_, id_, data

    def _candidate_indices(self, query: str) -> list[int]:
        tokens = self._tokens(query)
        if not tokens:
            return list(range(len(self._search_rows)))
        posting_lists: list[set[int]] = []
        for query_token in tokens:
            candidates: set[int] = set()
            for indexed_token, rows in self._token_postings.items():
                # Korean particles and legacy substring queries must retain the
                # old semantics ("어텐션" matches token "어텐션은").
                if query_token in indexed_token or indexed_token in query_token:
                    candidates.update(rows)
            if not candidates:
                # Preserve arbitrary substring compatibility when token
                # boundaries cannot produce a safe candidate set.
                return list(range(len(self._search_rows)))
            posting_lists.append(candidates)
        if not posting_lists:
            return list(range(len(self._search_rows)))
        candidates = set.intersection(*(set(posting) for posting in posting_lists))
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

    async def search(self, query: str, opts: dict | None = None) -> list[dict]:
        """레코드 키·값 substring 매칭. opts['type']로 타입 한정 가능."""
        opts = opts or {}
        want_type = opts.get("type")
        q = (query or "").lower()
        diagnostics = opts.get("_search_diagnostics")
        diagnostics = diagnostics if isinstance(diagnostics, dict) else None
        query_started = perf_counter()
        fresh, reason_code, stat_count = self._freshness()
        if not fresh:
            if diagnostics is not None:
                diagnostics.update({
                    "index_mode": "startup_snapshot",
                    "index_revision": self._index_revision,
                    "fresh": False,
                    "freshness_reason_code": reason_code,
                    "freshness_stat_count": stat_count,
                    "query_file_opens": 0,
                    "filesystem_traversal": False,
                })
            raise RuntimeError(
                f"memory_index_stale:{reason_code}; restart server or call refresh_search_index()"
            )
        candidate_indices = self._candidate_indices(q)
        hits: list[tuple[int, int, dict]] = []
        for index in candidate_indices:
            type_, id_, data, blob, priority = self._search_rows[index]
            if want_type and type_ != want_type:
                continue
            # 빈 쿼리는 전건 매칭인데, brain 지식노트(수백 건)가 쏟아지면 후보풀을 삼켜
            # 다른 백엔드를 밀어낸다 → 빈 쿼리일 때 brain:* 는 제외(yaml 엔티티만 나열).
            if not q and str(type_).startswith("brain:"):
                continue
            if not q or q in blob:
                count = blob.count(q) if q else 1
                hits.append(
                    (
                        count,
                        priority,
                        make_record(
                            str(id_), type_, self.name, data, score=float(count)
                        ),
                    )
                )
        # 매칭 빈도 내림차순 = 백엔드 내 순위
        hits.sort(key=lambda t: (-t[0], -t[1]))
        if diagnostics is not None:
            diagnostics.update({
                "index_mode": "startup_snapshot",
                "indexed_records": len(self._search_rows),
                "priority_records": self._priority_row_count,
                "candidate_records": len(candidate_indices),
                "index_revision": self._index_revision,
                "fresh": True,
                "freshness_reason_code": reason_code,
                "freshness_stat_count": stat_count,
                "cold_index_build_ms": self._index_build_ms,
                "warm_query_ms": round((perf_counter() - query_started) * 1000, 1),
                "index_file_reads": self._index_file_reads,
                "query_file_opens": 0,
                "filesystem_traversal": False,
            })
        return [rec for _, _, rec in hits]

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
