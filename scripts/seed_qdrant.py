#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""yohan-mcp v2 — Qdrant 시딩 (P2.5).

Notion RESOURCE DB 전체를 임베딩해 Qdrant 'yohan_resources' 컬렉션에 적재.
- 멱등: point id = uuid5(resource_url) → 재실행 시 중복 없이 덮어쓰기.
- 배치 진행률 출력, 실패 건 스킵·로깅.
- 옵션: --limit N (테스트용), --batch N.

사용법:
  python scripts/seed_qdrant.py             # 전체
  python scripts/seed_qdrant.py --limit 10  # 10건만
  python scripts/seed_qdrant.py --rebuild   # 컬렉션 삭제 후 재생성(차원 변경 시: hash384→ollama1024)
  python scripts/seed_qdrant.py --demo      # Notion 없이 내장 데모 RESOURCE 로 파이프라인 실증
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

# Notion 없이도 ollama 임베딩 파이프라인을 실증하는 내장 데모 RESOURCE
_DEMO_RESOURCES = [
    {
        "resource_id": "res_demo_attention", "title": "트랜스포머 어텐션 메커니즘 정리",
        "source_url": "https://demo.yohan-mcp.dev/attention", "resource_type": "아티클",
        "domain": "AI", "status": "신규",
        "raw_content": "어텐션은 쿼리·키·값의 내적으로 토큰 간 관련도를 계산해 가중 평균한다. 멀티헤드로 표현 다양성을 높인다.",
        "captured_at": "2026-06-08T09:00:00+09:00",
    },
    {
        "resource_id": "res_demo_rag", "title": "RAG 청크 크기와 검색 품질",
        "source_url": "https://demo.yohan-mcp.dev/rag-chunk", "resource_type": "아티클",
        "domain": "AI", "status": "신규",
        "raw_content": "256~512 토큰 구간에서 재현율·정밀도 균형이 가장 좋다. 과도한 청크는 노이즈를 늘려 응답 품질을 떨어뜨린다.",
        "captured_at": "2026-06-08T09:05:00+09:00",
    },
    {
        "resource_id": "res_demo_mcp", "title": "노션 MCP 연동 실전",
        "source_url": "https://demo.yohan-mcp.dev/notion-mcp", "resource_type": "문서",
        "domain": "개발", "status": "신규",
        "raw_content": "MCP 서버로 노션 DB 를 타입 시스템으로 묶어 Dev Log 자동 적재와 크로스 백엔드 검색을 구성한다.",
        "captured_at": "2026-06-08T09:10:00+09:00",
    },
]


async def seed(limit: int | None = None, batch: int = 64, rebuild: bool = False, demo: bool = False) -> int:
    load_dotenv()
    notion = NotionAdapter()
    qdrant = QdrantAdapter()

    if not qdrant.url:
        print("⚠ QDRANT_URL 미설정 → 임베디드(:memory:) 모드 — 영속되지 않음(데모용).")

    if rebuild:
        existed = await qdrant.drop_collection()
        print(f"--rebuild: 컬렉션 '{qdrant.collection}' {'삭제 후 ' if existed else '없음 → '}재생성")

    await qdrant.ensure_collection()
    print(f"임베더: {qdrant.embedder.name} (dim={qdrant.embedder.dim}), 컬렉션: {qdrant.collection}")

    if demo:
        resources = _DEMO_RESOURCES[:limit] if limit else _DEMO_RESOURCES
        print(f"--demo: 내장 RESOURCE {len(resources)}건 (Notion 불필요)")
    else:
        if not notion.token:
            print("⚠ NOTION_TOKEN 미설정 → 로드할 RESOURCE 0건. .env 설정 또는 --demo 사용.")
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
    ap.add_argument("--rebuild", action="store_true", help="컬렉션 삭제 후 재생성(차원 변경 시)")
    ap.add_argument("--demo", action="store_true", help="Notion 없이 내장 데모 RESOURCE 적재")
    args = ap.parse_args()
    asyncio.run(seed(limit=args.limit, batch=args.batch, rebuild=args.rebuild, demo=args.demo))
    return 0


if __name__ == "__main__":
    sys.exit(main())
