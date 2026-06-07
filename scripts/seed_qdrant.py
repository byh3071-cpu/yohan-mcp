#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""yohan-mcp v2 — Qdrant 시딩 (P2.5).

Notion RESOURCE DB 전체를 임베딩해 Qdrant 'yohan_resources' 컬렉션에 적재.
- 멱등: point id = uuid5(resource_url) → 재실행 시 중복 없이 덮어쓰기.
- 배치 진행률 출력, 실패 건 스킵·로깅.
- 옵션: --limit N (테스트용), --batch N.

사용법:
  python scripts/seed_qdrant.py            # 전체
  python scripts/seed_qdrant.py --limit 10 # 10건만
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Windows 콘솔 UTF-8 (docs/patterns/env-windows-console-utf8.md)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

# scripts/ 에서 직접 실행해도 리포 루트 패키지(adapters/core)를 import 할 수 있게
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

from adapters.notion_adapter import NotionAdapter
from adapters.qdrant_adapter import QdrantAdapter


async def seed(limit: int | None = None, batch: int = 64) -> int:
    load_dotenv()
    notion = NotionAdapter()
    qdrant = QdrantAdapter()

    if not qdrant.url:
        print("⚠ QDRANT_URL 미설정 → 임베디드(:memory:) 모드 — 영속되지 않음(데모용).")
    if not notion.token:
        print("⚠ NOTION_TOKEN 미설정 → 로드할 RESOURCE 0건. .env 설정 후 재실행.")

    await qdrant.ensure_collection()
    print(f"임베더: {qdrant.embedder.name} (dim={qdrant.embedder.dim}), 컬렉션: {qdrant.collection}")

    resources = await notion.fetch_all("resource", limit=limit)
    print(f"RESOURCE 로드: {len(resources)}건")

    ok = fail = 0
    for i, r in enumerate(resources, 1):
        try:
            await qdrant.create("resource", r)
            ok += 1
        except Exception as exc:
            fail += 1
            print(f"  스킵 [{i}] {r.get('resource_id', '?')}: {type(exc).__name__}: {exc}")
        if i % batch == 0:
            print(f"  진행 {i}/{len(resources)}")

    try:
        cnt = (await qdrant._get_client().count(qdrant.collection)).count
    except Exception:
        cnt = -1
    print(f"완료 — 적재 {ok}, 실패 {fail}, 컬렉션 포인트 {cnt}")

    await notion.aclose()
    await qdrant.aclose()
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description="Qdrant 시딩 (Notion RESOURCE → 벡터)")
    ap.add_argument("--limit", type=int, default=None, help="적재 건수 제한(테스트용)")
    ap.add_argument("--batch", type=int, default=64, help="진행률 출력 배치 크기")
    args = ap.parse_args()
    asyncio.run(seed(limit=args.limit, batch=args.batch))
    return 0


if __name__ == "__main__":
    sys.exit(main())
