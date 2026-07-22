#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""brain 벡터 검색 회귀 측정 — 골든셋 고정 질문으로 hit@k · recall@k 를 낸다.

LLM 을 쓰지 않는다. 질문을 임베딩해 brain_memory 를 조회하고, 골든셋이 명시한 정답
문서 경로가 top-k 안에 있는지만 본다 — 같은 인덱스면 항상 같은 숫자가 나온다.

왜 경로 대조인가: 즉석 검증에서 "경로에 특정 단어가 들어가면 통과" 식으로 쟀다가
무관한 문서가 통과 처리되는 가짜 양성을 봤다. 정답은 골든셋에 문서 단위로 못 박고
전체 경로 일치로만 판정한다.

지표:
  hit@k    — 정답 문서 중 하나라도 top-k 에 들었나 (질문당 0/1) 의 평균
  recall@k — 정답 문서 중 top-k 에 든 비율 (질문당 0~1) 의 평균
  정답이 여러 개인 질문에서 둘이 갈린다. 하나만 찾아도 답은 되지만 회수율은 낮다.

`indexed: false` 문항은 정답 문서가 시딩 allowlist 밖이라 실패가 정상이다. 집계를
분리해 출력하고 총점에서 뺀다 — 섞으면 allowlist 확장 효과가 검색 품질 변화로 오독된다.

사용법:
  python scripts/eval_recall.py                 # 기본 골든셋, top_k 는 파일 값
  python scripts/eval_recall.py --k 10          # top-k 덮어쓰기
  python scripts/eval_recall.py --json out.json # 기준선 저장(전후 비교용)
  python scripts/eval_recall.py --verbose       # 질문별 top-k 경로까지 출력
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml
from dotenv import load_dotenv

from adapters.qdrant_adapter import BRAIN_MEMORY_COLLECTION, QdrantAdapter
from core.paths import resolve_memory_dir

load_dotenv()

DEFAULT_GOLDEN_REL = "wiki/answers/golden-set.yaml"


def load_golden(path: Path) -> dict:
    """골든셋 로드 + 최소 검증. 스키마가 깨진 채 0점을 내는 것보다 즉시 터지는 게 낫다."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    questions = data.get("questions") or []
    if not questions:
        raise ValueError(f"골든셋에 questions 가 없다: {path}")
    for q in questions:
        for key in ("id", "question", "expect"):
            if not q.get(key):
                raise ValueError(f"골든셋 항목에 {key} 누락: {q.get('id', q)}")
    return data


def _payload_path_to_repo(path: str) -> str:
    """brain_memory payload 의 path 는 memory/ 기준 — 골든셋의 레포루트 기준으로 맞춘다."""
    return f"memory/{path}".replace("\\", "/")


async def evaluate(golden: dict, k: int, verbose: bool) -> dict:
    q_adapter = QdrantAdapter(collection=BRAIN_MEMORY_COLLECTION)
    try:
        client = q_adapter._get_client()
        rows = []
        for item in golden["questions"]:
            vec = await asyncio.to_thread(q_adapter.embedder.embed, [item["question"]])
            hits = (
                await client.query_points(
                    q_adapter.collection, query=vec[0], limit=k, with_payload=True
                )
            ).points
            # 같은 문서의 여러 청크가 잡히면 문서 단위로 접는다 — 지표는 문서 단위다.
            found: list[str] = []
            for h in hits:
                p = _payload_path_to_repo(h.payload.get("path", ""))
                if p not in found:
                    found.append(p)
            expect = list(item["expect"])
            matched = [e for e in expect if e in found]
            rows.append(
                {
                    "id": item["id"],
                    "question": item["question"],
                    "category": item.get("category", ""),
                    "indexed": bool(item.get("indexed", True)),
                    "hit": bool(matched),
                    "recall": len(matched) / len(expect),
                    "expect": expect,
                    "matched": matched,
                    "top": [
                        (
                            _payload_path_to_repo(h.payload.get("path", "")),
                            round(h.score, 3),
                        )
                        for h in hits
                    ],
                }
            )
        return {"k": k, "rows": rows}
    finally:
        await q_adapter.aclose()


def report(result: dict, verbose: bool) -> int:
    k, rows = result["k"], result["rows"]
    scored = [r for r in rows if r["indexed"]]
    control = [r for r in rows if not r["indexed"]]

    for r in rows:
        mark = "통과" if r["hit"] else ("미인덱스" if not r["indexed"] else "실패")
        print(f"\n[{mark}] {r['id']} {r['question']}")
        print(
            f"    정답 {len(r['matched'])}/{len(r['expect'])} — recall {r['recall']:.2f}"
        )
        for e in r["expect"]:
            print(f"      {'O' if e in r['matched'] else 'X'} {e}")
        if verbose or not r["hit"]:
            for p, s in r["top"]:
                print(f"        {s}  {p}")

    n = len(scored)
    hit = sum(r["hit"] for r in scored)
    rec = sum(r["recall"] for r in scored) / n if n else 0.0
    print(f"\n{'=' * 60}")
    print(
        f"인덱스 대상 {n}문항 — hit@{k} {hit}/{n} ({hit / n:.0%})  recall@{k} {rec:.2f}"
    )
    if control:
        c_hit = sum(r["hit"] for r in control)
        print(
            f"대조군(미인덱스) {len(control)}문항 — hit {c_hit}/{len(control)}"
            " (0이 정상. allowlist 확장 후 올라가야 한다)"
        )
    # 실패 문항이 있으면 비영 종료 — CI/스크립트가 회귀를 잡을 수 있게.
    return 0 if hit == n else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="brain 검색 회귀 측정 (골든셋 기반)")
    ap.add_argument(
        "--golden", type=Path, help=f"골든셋 경로(기본 memory/{DEFAULT_GOLDEN_REL})"
    )
    ap.add_argument("--k", type=int, help="top-k 덮어쓰기(기본: 골든셋 top_k)")
    ap.add_argument("--json", type=Path, help="결과를 JSON 으로 저장(기준선 비교용)")
    ap.add_argument(
        "--verbose", action="store_true", help="통과 문항도 top-k 경로 출력"
    )
    args = ap.parse_args()

    path = args.golden or (resolve_memory_dir() / DEFAULT_GOLDEN_REL)
    if not path.exists():
        print(f"골든셋 없음: {path}")
        return 2
    golden = load_golden(path)
    k = args.k or int(golden.get("top_k", 5))
    print(f"골든셋: {path} (문항 {len(golden['questions'])}개, top_k={k})")

    result = asyncio.run(evaluate(golden, k, args.verbose))
    code = report(result, args.verbose)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        print(f"결과 저장: {args.json}")
    return code


if __name__ == "__main__":
    sys.exit(main())
