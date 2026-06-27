# -*- coding: utf-8 -*-
"""memory 백엔드 어댑터 (P2 실동작).

로컬 파일시스템 memory/ 디렉토리를 읽고 쓴다.
- profile  → memory/profile.yaml           (단일)
- decision → memory/decisions/<id>.yaml
- ingest   → memory/ingest/<id>.yaml

환경변수 MEMORY_DIR / YOHAN_BRAIN_ROOT 로 베이스 경로 변경 (기본: 리포 루트/memory, deprecated).
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml

from adapters.base import BackendAdapter, _Timer, health, make_record
from core.paths import resolve_memory_dir

ROOT = Path(__file__).resolve().parent.parent

# 타입 → (서브경로, 단일파일 여부)
_LAYOUT = {
    "profile": ("profile.yaml", True),
    "decision": ("decisions", False),
    "ingest": ("ingest", False),
}
# 타입별 ID 필드명 (스키마 PK)
_ID_FIELD = {"profile": "name", "decision": "decision_id", "ingest": "ingest_id"}


class MemoryAdapter(BackendAdapter):
    name = "memory"

    def __init__(self, base_dir: str | os.PathLike | None = None) -> None:
        self.base = Path(base_dir) if base_dir else resolve_memory_dir()

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
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            return None
        return yaml.safe_load(content) or {}

    def _iter_all(self):
        """(type_, id_, data) 전부 순회."""
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

    async def search(self, query: str, opts: dict | None = None) -> list[dict]:
        """파일 본문 substring 매칭. opts['type']로 타입 한정 가능."""
        opts = opts or {}
        want_type = opts.get("type")
        q = (query or "").lower()
        hits: list[tuple[int, dict]] = []
        for type_, id_, data in self._iter_all():
            if want_type and type_ != want_type:
                continue
            blob = yaml.safe_dump(data, allow_unicode=True).lower()
            if not q or q in blob:
                count = blob.count(q) if q else 1
                hits.append((count, make_record(str(id_), type_, self.name, data, score=float(count))))
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
        path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
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
            current = self._read_yaml(path) or {}
            if single and str(current.get(_ID_FIELD[t], "")) != self._safe_id(id_):
                continue  # profile 은 id 일치할 때만 (오염 방지)
            current.update(data)
            path.write_text(
                yaml.safe_dump(current, allow_unicode=True, sort_keys=False), encoding="utf-8"
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
