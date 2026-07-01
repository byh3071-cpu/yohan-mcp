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
  python scripts/seed_qdrant.py --control-tower-demo  # 관제탑 4컬렉션 데모 청크(get_context 회수 실증, 데모 전용)
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from pathlib import Path

# Windows 콘솔 UTF-8 (docs/patterns/env-windows-console-utf8.md)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

# scripts/ 에서 직접 실행해도 리포 루트 패키지(adapters/core)를 import 할 수 있게
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
from qdrant_client import models

from adapters.notion_adapter import NotionAdapter
from adapters.qdrant_adapter import CONTROL_TOWER_COLLECTIONS, QdrantAdapter

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


# ── 관제탑 4컬렉션 데모 시드 (로컬/CI 실증 전용, control-tower 실데이터 아님) ──
# 실소유자는 yohan-control-tower — 이 시드는 get_context 벡터 회수를 로컬에서 재현하기 위한
# 데모 청크일 뿐이다. 각 청크는 payload._demo=True 로 마킹된다.
#
# 안전 설계: 실 관제탑 컬렉션(knowledge_base 등)에는 절대 쓰지 않는다. 아래 키에
# DEMO_COLLECTION_PREFIX 를 붙인 **데모 전용 컬렉션**(demo_knowledge_base 등)에만
# 생성·삭제·적재한다 → 실 QDRANT_URL 대상이라도 실데이터 드롭·오염·오차원 생성 0.
# get_context 로 회수하려면 QDRANT_SEARCH_COLLECTIONS 로 데모 컬렉션을 지정한다(아래 출력 안내).
DEMO_COLLECTION_PREFIX = "demo_"

_CONTROL_TOWER_DEMO: dict[str, list[tuple[str, list[str]]]] = {
    "knowledge_base": [
        ("kb-attention", [
            "어텐션은 쿼리·키·값의 내적으로 토큰 간 관련도를 계산해 가중 평균한다.",
            "멀티헤드 어텐션은 서로 다른 부분공간에서 관계를 병렬 학습한다.",
            "셀프 어텐션 복잡도는 시퀀스 길이 제곱이라 긴 문서는 청크 분할이 필요하다.",
        ]),
        ("kb-rag-chunk", [
            "256~512 토큰 청크에서 재현율·정밀도 균형이 가장 좋다.",
            "청크 오버랩은 경계에서 잘린 문맥을 복원해 회수 손실을 줄인다.",
        ]),
    ],
    "system_rules": [
        ("sr-guardrails", [
            "되돌릴 수 없는 작업은 LLM을 결정경로에서 제외하고 룰+하드리밋으로 구현한다.",
            "LLM이 뱉는 닫힌집합 값은 코드에서 allowlist 로 대조한다.",
        ]),
    ],
    "semantic_cache": [
        ("sc-embed", [
            "동일 질의의 임베딩을 캐시해 반복 검색 비용을 줄인다.",
            "캐시 키는 정규화된 질의 문자열 해시로 구성한다.",
        ]),
    ],
    "execution_history": [
        ("eh-run", [
            "프로토콜 실행은 run_id 저널에 append-only 로 기록해 멱등 재생한다.",
            "게이트에서 멈춘 실행은 승인 큐로 폴백해 사람 결정을 기다린다.",
        ]),
    ],
}


async def seed_control_tower_demo(qdrant: QdrantAdapter, rebuild: bool = False) -> int:
    """데모 전용 컬렉션(demo_*)에 관제탑 회수용 데모 청크를 시드. 반환: 적재한 포인트 수.

    실 관제탑 컬렉션(knowledge_base 등)은 건드리지 않는다 — demo_ 접두 컬렉션만 생성/드롭/적재.
    """
    print("⚠ 관제탑 데모 시드 — 로컬/CI 실증 전용(실데이터 아님, payload._demo=True 마킹).")
    print(f"   실 관제탑 컬렉션 불가침 — '{DEMO_COLLECTION_PREFIX}' 접두 데모 컬렉션에만 쓴다.")
    client = qdrant._get_client()
    dim = qdrant.embedder.dim
    print(f"임베더: {qdrant.embedder.name} (dim={dim})")

    total = 0
    seeded: list[str] = []
    for base_coll, pages in _CONTROL_TOWER_DEMO.items():
        coll = f"{DEMO_COLLECTION_PREFIX}{base_coll}"  # demo_knowledge_base — 실 컬렉션과 이름 충돌 0
        if rebuild and await client.collection_exists(coll):
            await client.delete_collection(coll)  # 데모 컬렉션만 드롭(실데이터 무관)
        if not await client.collection_exists(coll):
            await client.create_collection(
                coll, vectors_config=models.VectorParams(size=dim, distance=models.Distance.COSINE)
            )
        ok = 0
        for page_id, chunks in pages:
            try:
                # 어댑터 계약과 동일하게 동기 임베딩을 스레드로 — 이벤트루프 블로킹 방지
                vecs = await asyncio.to_thread(qdrant.embedder.embed, chunks)
            except Exception as exc:  # 임베딩 실패는 해당 페이지만 스킵
                print(f"  스킵 [{coll}/{page_id}] 임베딩 실패: {type(exc).__name__}: {exc}")
                continue
            points = [
                models.PointStruct(
                    id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"demo/{coll}/{page_id}#chunk{i}")),
                    vector=vecs[i],
                    payload={"notion_page_id": page_id, "chunk_index": i,
                             "title": page_id, "text": chunks[i], "_demo": True},
                )
                for i in range(len(chunks))
            ]
            try:
                await client.upsert(coll, points=points)
                ok += len(points)
            except Exception as exc:  # 차원 불일치 등 — 컬렉션 단위 격리
                print(f"  스킵 [{coll}/{page_id}] upsert 실패: {type(exc).__name__}: {exc}")
        print(f"  {coll}: {ok} 청크 적재")
        total += ok
        if ok:
            seeded.append(coll)
    print(f"완료 — 데모 청크 {total}개 (데모 컬렉션 {len(seeded)}개)")
    if seeded:
        joined = ",".join(seeded)
        print("get_context 로 회수하려면 검색 대상에 데모 컬렉션을 지정(PowerShell):")
        print(f'  $env:QDRANT_SEARCH_COLLECTIONS="{joined}"; python server.py')
    return total


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


async def _run_control_tower_demo(rebuild: bool) -> None:
    load_dotenv()
    qdrant = QdrantAdapter()
    try:
        if not qdrant.url:
            print("⚠ QDRANT_URL 미설정 → 임베디드(:memory:) 모드 — 영속되지 않음(데모용).")
        await seed_control_tower_demo(qdrant, rebuild=rebuild)
    finally:
        await qdrant.aclose()  # 예외 시에도 클라이언트 정리


def main() -> int:
    ap = argparse.ArgumentParser(description="Qdrant 시딩 (Notion RESOURCE → 벡터)")
    ap.add_argument("--limit", type=int, default=None, help="적재 건수 제한(테스트용)")
    ap.add_argument("--batch", type=int, default=64, help="진행률 출력 배치 크기")
    ap.add_argument("--rebuild", action="store_true", help="컬렉션 삭제 후 재생성(차원 변경 시)")
    ap.add_argument("--demo", action="store_true", help="Notion 없이 내장 데모 RESOURCE 적재")
    ap.add_argument("--control-tower-demo", action="store_true", dest="control_tower_demo",
                    help="관제탑 4컬렉션 데모 청크 시드(get_context 회수 실증, 데모 전용)")
    args = ap.parse_args()
    if args.control_tower_demo:
        asyncio.run(_run_control_tower_demo(rebuild=args.rebuild))
    else:
        asyncio.run(seed(limit=args.limit, batch=args.batch, rebuild=args.rebuild, demo=args.demo))
    return 0


if __name__ == "__main__":
    sys.exit(main())
