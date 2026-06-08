# -*- coding: utf-8 -*-
"""yohan-mcp v2 — 인스턴스 링크 저장소 (P3).

`schemas/_links.json` 은 **타입 수준** 관계 맵(notion:summary → studio:post)이라
런타임에 생긴 **개별 인스턴스 관계**(sum_1 → post_1)는 거기에 적으면 스키마가 오염된다.
그래서 인스턴스 링크는 별도 런타임 저장소(JSONL)에 적재한다 — schema 맵은 불변 유지.

노드 표기는 `<backend>:<entity>:<id>` (예: notion:summary:sum_1).
publish 가 published_as 를 기록하고, get_context 등이 traversal 에 활용할 수 있다.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_KST = timezone(timedelta(hours=9))


def _now_iso() -> str:
    return datetime.now(_KST).isoformat(timespec="seconds")


class LinkStore:
    """인스턴스 링크 append-only JSONL 저장소."""

    def __init__(self, path: str | os.PathLike | None = None) -> None:
        if path is not None:
            self.path = Path(path)
        else:
            base = Path(os.getenv("MEMORY_DIR", ROOT / "memory"))
            self.path = base / "links.jsonl"

    def record(self, source: str, target: str, relation: str, **meta) -> dict:
        """인스턴스 링크 1건 기록 후 그 dict 반환(멱등 아님 — 발행 이력은 N건 누적 가능)."""
        link = {"source": source, "target": target, "relation": relation,
                "created_at": _now_iso(), **meta}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(link, ensure_ascii=False) + "\n")
        return link

    def all(self) -> list[dict]:
        if not self.path.exists():
            return []
        out: list[dict] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return out

    def find(self, *, source: str | None = None, target: str | None = None,
             relation: str | None = None) -> list[dict]:
        def ok(link: dict) -> bool:
            return ((source is None or link.get("source") == source)
                    and (target is None or link.get("target") == target)
                    and (relation is None or link.get("relation") == relation))
        return [link for link in self.all() if ok(link)]
