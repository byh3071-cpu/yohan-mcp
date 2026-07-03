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

import logging
import os
from pathlib import Path

import yaml

from adapters.base import BackendAdapter, _Timer, health, make_record
from core.paths import resolve_memory_dir

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


class MemoryAdapter(BackendAdapter):
    name = "memory"

    def __init__(self, base_dir: str | os.PathLike | None = None) -> None:
        self.base = Path(base_dir) if base_dir else resolve_memory_dir()
        # brain .md 파싱 캐시 (path → (mtime, data)). 변화 없으면 재읽기·재파싱 회피.
        self._md_cache: dict[str, tuple[float, dict]] = {}

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
    def _read_md(path: Path) -> dict | None:
        """brain `.md`+frontmatter → 레코드 dict. 선두 `---` 블록=frontmatter, 나머지=body.

        frontmatter 없으면 {"body": 전문}. 깨진 frontmatter 는 무시하고 본문은 살린다(graceful).
        """
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return None
        fm: dict = {}
        body = text
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
                    break
        rec = dict(fm)
        rec["body"] = body.strip()[:_MD_BODY_MAX]  # 상한(토큰 폭증 방지)
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

    def _iter_all(self):
        """(type_, id_, data) 전부 순회 — yaml 레이아웃 + brain .md 지식노트."""
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

    async def search(self, query: str, opts: dict | None = None) -> list[dict]:
        """파일 본문 substring 매칭. opts['type']로 타입 한정 가능."""
        opts = opts or {}
        want_type = opts.get("type")
        q = (query or "").lower()
        hits: list[tuple[int, dict]] = []
        for type_, id_, data in self._iter_all():
            if want_type and type_ != want_type:
                continue
            # 빈 쿼리는 전건 매칭인데, brain 지식노트(수백 건)가 쏟아지면 후보풀을 삼켜
            # 다른 백엔드를 밀어낸다 → 빈 쿼리일 때 brain:* 는 제외(yaml 엔티티만 나열).
            if not q and str(type_).startswith("brain:"):
                continue
            blob = yaml.safe_dump(data, allow_unicode=True).lower()
            if not q or q in blob:
                count = blob.count(q) if q else 1
                hits.append(
                    (
                        count,
                        make_record(
                            str(id_), type_, self.name, data, score=float(count)
                        ),
                    )
                )
        # 매칭 빈도 내림차순 = 백엔드 내 순위
        hits.sort(key=lambda t: -t[0])
        return [rec for _, rec in hits]

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
