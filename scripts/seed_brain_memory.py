#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""yohan-mcp v2 — yohan-brain memory/ 벡터 인제스트 (스프린트 필수 코어 ②).

두 서브커맨드:
  memory  (기본, 서브커맨드 생략 가능) — memory/ allowlist 6폴더(decisions·wiki·ingest·
           knowledge-hub·projects·rules)의 .md 전수를 md-aware 청킹(core/chunking.py) 후
           임베딩해 Qdrant 'brain_memory' 컬렉션에 적재.
  triples — memory/knowledge-hub/triple-map.md 의 트리플 표를 파싱(core/triple_map.py)해
           문장화 임베딩 후 'ontology_triples' 컬렉션에 적재.

하드페일 가드: 두 서브커맨드 모두 진입 시 임베더가 실 ollama(bge-m3, dim 1024)인지 확인한다.
아니면(hash 폴백 등) 그 자리에서 즉시 예외로 중단 — brain 지식을 저품질 임베딩으로 조용히
적재해 "시딩은 됐는데 검색이 안 되는" 상태를 방지한다(BLOCKER D). `--allow-fallback` 지정 시에만
경고 로그와 함께 강제 진행(테스트/오프라인 전용 — 실사용 금지).

멱등: point id = uuid5(고정 네임스페이스, 안정 키) — memory 는 "path#chunk{i}", triples 는
"subject|relation|object". 같은 입력을 몇 번 재시딩해도 포인트 수가 늘지 않는다(upsert).

사용법:
  python scripts/seed_brain_memory.py                    # brain_memory 전체 시드
  python scripts/seed_brain_memory.py --limit 20         # 파일 20건만(테스트용)
  python scripts/seed_brain_memory.py --rebuild          # 컬렉션 삭제 후 재생성
  python scripts/seed_brain_memory.py triples            # ontology_triples 시드
  python scripts/seed_brain_memory.py triples --rebuild
"""
from __future__ import annotations

import argparse
import asyncio
import logging
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

from adapters.memory_adapter import MemoryAdapter, _BRAIN_KNOWLEDGE_DIRS
from adapters.qdrant_adapter import (
    BRAIN_MEMORY_COLLECTION,
    ONTOLOGY_TRIPLES_COLLECTION,
    QdrantAdapter,
)
from core.chunking import chunk_markdown
from core.paths import resolve_memory_dir
from core.triple_map import parse_triple_map, sentence_for_triple

logger = logging.getLogger(__name__)

# 시딩 전용 uuid5 네임스페이스 — qdrant_adapter._URL_NS(리소스 point_id) 와 겹치지 않는 별도 공간.
_NS = uuid.uuid5(uuid.NAMESPACE_URL, "yohan-mcp/brain-memory-seed")

BATCH_SIZE = 32  # ollama 배치 임베딩 크기(진행률 출력 겸용) — 첫 배치가 느리면 줄여서 재시도.


class HardFailEmbedderError(RuntimeError):
    """하드페일 가드 위반 — 실 ollama(bge-m3, dim 1024) 가 아닌 임베더로 brain 시딩 시도."""


def _assert_ollama_embedder(embedder, allow_fallback: bool) -> None:
    """BLOCKER D — embedder.name!='ollama' or dim!=1024 면 즉시 예외. --allow-fallback 만 예외."""
    detail = f"embedder={embedder.name} dim={embedder.dim} (기대: ollama/1024)"
    if embedder.name == "ollama" and embedder.dim == 1024:
        print(f"임베더 가드 통과: {detail}")
        return
    if allow_fallback:
        logger.warning("하드페일 가드 우회(--allow-fallback): %s", detail)
        print(f"WARNING: --allow-fallback 로 가드 우회 — {detail} (골든쿼리 품질 저하 가능, 실사용 금지)")
        return
    raise HardFailEmbedderError(
        f"brain 시딩 하드페일 가드 위반: {detail} — 실 ollama 임베더 필요. "
        "Ollama 기동 확인(`ollama pull bge-m3`, localhost:11434) 또는 "
        "--allow-fallback(테스트/오프라인 전용, 실사용 금지)."
    )


# ── memory 서브커맨드 ────────────────────────────────────────────
def _iter_brain_md_files(base: Path):
    """allowlist 6폴더의 .md 를 정렬 순회 — memory_adapter._iter_all 과 동일 폴더 규약 공유."""
    base_resolved = base.resolve()
    for kdir in _BRAIN_KNOWLEDGE_DIRS:
        root = base / kdir
        if not root.exists():
            continue
        for p in sorted(root.rglob("*.md")):
            if not p.resolve().is_relative_to(base_resolved):
                continue
            yield kdir, p


def _title_of(rel_path: str, fm: dict) -> str:
    return str(fm.get("title") or fm.get("id") or Path(rel_path).stem)


async def seed_memory(
    *,
    limit: int | None = None,
    offset: int = 0,
    rebuild: bool = False,
    allow_fallback: bool = False,
    batch_size: int = BATCH_SIZE,
    qdrant: "QdrantAdapter | None" = None,
    base_dir: "Path | None" = None,
) -> dict:
    """brain memory/ allowlist .md 전수 → 청킹 → 임베딩 → brain_memory 컬렉션 upsert(멱등).

    qdrant/base_dir 주입 지점 — 테스트가 :memory: Qdrant + tmp_path 로 격리 실행할 수 있게 한다.
    offset/limit — 대량 재실행 시 파일 목록을 슬라이스해 여러 번에 나눠 돌릴 수 있게 한다(예:
    CPU 임베딩이 느려 1회 실행이 오래 걸릴 때). point id 가 경로+청크인덱스 결정적이라 슬라이스를
    나눠 돌려도 멱등 — 어느 슬라이스를 몇 번 반복해도 안전하다.
    반환: {"embedder","dim","files","skipped_files","chunks","collection"}.
    """
    base = base_dir or resolve_memory_dir()
    owns_adapter = qdrant is None
    qdrant = qdrant or QdrantAdapter(collection=BRAIN_MEMORY_COLLECTION)
    try:
        _assert_ollama_embedder(qdrant.embedder, allow_fallback)
        print(f"임베더: {qdrant.embedder.name} (dim={qdrant.embedder.dim}), 컬렉션: {qdrant.collection}")
        print(f"brain memory 루트: {base}")

        if rebuild:
            existed = await qdrant.drop_collection()
            print(f"--rebuild: 컬렉션 '{qdrant.collection}' {'삭제 후 ' if existed else '없음 → '}재생성")
        await qdrant.ensure_collection()
        client = qdrant._get_client()

        all_files = list(_iter_brain_md_files(base))
        files = all_files[offset:]
        if limit:
            files = files[:limit]
        print(
            f"대상 파일: {len(files)}건(전체 {len(all_files)}건 중 offset={offset}) "
            f"(allowlist: {', '.join(_BRAIN_KNOWLEDGE_DIRS)})"
        )

        total_chunks = 0
        skipped_files = 0
        pending_meta: list[tuple[str, dict]] = []
        pending_texts: list[str] = []

        async def _flush() -> None:
            nonlocal pending_meta, pending_texts, total_chunks
            if not pending_texts:
                return
            vecs = await asyncio.to_thread(qdrant.embedder.embed, pending_texts)
            points = [
                models.PointStruct(id=pid, vector=vecs[i], payload=payload)
                for i, (pid, payload) in enumerate(pending_meta)
            ]
            await client.upsert(qdrant.collection, points=points)
            total_chunks += len(points)
            pending_meta = []
            pending_texts = []

        for i, (kdir, path) in enumerate(files, 1):
            rec = MemoryAdapter._read_md_uncapped(path)
            if rec is None:
                skipped_files += 1
                print(f"  스킵 [{i}/{len(files)}] {path}: 읽기 실패")
                continue
            body = rec.get("body", "")
            rel = str(path.relative_to(base)).replace("\\", "/")
            title = _title_of(rel, rec)
            base_line = int(rec.get("_body_start_line", 1))
            chunks = chunk_markdown(body, base_line=base_line)
            if not chunks:
                continue
            for idx, ch in enumerate(chunks):
                pid = str(uuid.uuid5(_NS, f"{rel}#chunk{idx}"))
                payload = {
                    "path": rel,
                    "title": title,
                    "chunk_index": idx,
                    "chunk_start_line": ch.start_line,
                    "type": f"brain:{kdir}",
                    "body": ch.text,
                }
                pending_meta.append((pid, payload))
                pending_texts.append(ch.text)
                if len(pending_texts) >= batch_size:
                    await _flush()
            if i % 20 == 0 or i == len(files):
                print(f"  진행 {i}/{len(files)} 파일 (누적 청크 {total_chunks + len(pending_texts)})")

        await _flush()
        print(
            f"완료 — 파일 {len(files) - skipped_files}건 처리(스킵 {skipped_files}), "
            f"청크 {total_chunks}개 적재 → '{qdrant.collection}'"
        )
        return {
            "embedder": qdrant.embedder.name,
            "dim": qdrant.embedder.dim,
            "files": len(files) - skipped_files,
            "skipped_files": skipped_files,
            "chunks": total_chunks,
            "collection": qdrant.collection,
        }
    finally:
        if owns_adapter:
            await qdrant.aclose()


# ── triples 서브커맨드 ───────────────────────────────────────────
async def seed_triples(
    *,
    rebuild: bool = False,
    allow_fallback: bool = False,
    batch_size: int = BATCH_SIZE,
    qdrant: "QdrantAdapter | None" = None,
    triple_map_path: "Path | None" = None,
) -> dict:
    """memory/knowledge-hub/triple-map.md → 문장화 임베딩 → ontology_triples 컬렉션(멱등)."""
    base = resolve_memory_dir()
    path = triple_map_path or (base / "knowledge-hub" / "triple-map.md")
    owns_adapter = qdrant is None
    qdrant = qdrant or QdrantAdapter(collection=ONTOLOGY_TRIPLES_COLLECTION)
    try:
        _assert_ollama_embedder(qdrant.embedder, allow_fallback)
        print(f"임베더: {qdrant.embedder.name} (dim={qdrant.embedder.dim}), 컬렉션: {qdrant.collection}")

        if not path.exists():
            print(f"triple-map.md 없음: {path} — 0건 적재")
            return {
                "embedder": qdrant.embedder.name, "dim": qdrant.embedder.dim,
                "triples": 0, "collection": qdrant.collection,
            }

        text = path.read_text(encoding="utf-8")
        palette, triples = parse_triple_map(text)
        print(f"triple-map.md 파싱: relation 팔레트 {len(palette)}종, 트리플 {len(triples)}건")

        if rebuild:
            existed = await qdrant.drop_collection()
            print(f"--rebuild: 컬렉션 '{qdrant.collection}' {'삭제 후 ' if existed else '없음 → '}재생성")
        await qdrant.ensure_collection()
        client = qdrant._get_client()

        sentences = [sentence_for_triple(t["subject"], t["relation"], t["object"], palette) for t in triples]
        ok = 0
        for start in range(0, len(triples), batch_size):
            batch_triples = triples[start:start + batch_size]
            batch_sentences = sentences[start:start + batch_size]
            if not batch_triples:
                continue
            vecs = await asyncio.to_thread(qdrant.embedder.embed, batch_sentences)
            points = []
            for t, sent, vec in zip(batch_triples, batch_sentences, vecs):
                pid = str(uuid.uuid5(_NS, f"{t['subject']}|{t['relation']}|{t['object']}"))
                payload = {
                    "subject": t["subject"], "relation": t["relation"], "object": t["object"],
                    "domain": t["domain"], "confidence": t["confidence"], "source": t["source"],
                    "text": sent,
                }
                points.append(models.PointStruct(id=pid, vector=vec, payload=payload))
            await client.upsert(qdrant.collection, points=points)
            ok += len(points)
            print(f"  진행 {min(start + batch_size, len(triples))}/{len(triples)}")

        print(f"완료 — 트리플 {ok}개 적재 → '{qdrant.collection}'")
        return {
            "embedder": qdrant.embedder.name, "dim": qdrant.embedder.dim,
            "triples": ok, "collection": qdrant.collection,
        }
    finally:
        if owns_adapter:
            await qdrant.aclose()


def main() -> int:
    ap = argparse.ArgumentParser(description="yohan-brain memory/ → Qdrant 벡터 인제스트(기본: brain_memory 시드)")
    ap.add_argument("--limit", type=int, default=None, help="처리 파일 수 제한(테스트용, memory 전용)")
    ap.add_argument("--offset", type=int, default=0, help="파일 목록 시작 오프셋(대량 재실행 분할용, memory 전용)")
    ap.add_argument("--rebuild", action="store_true", help="컬렉션 삭제 후 재생성(차원 변경 시)")
    ap.add_argument(
        "--allow-fallback", action="store_true", dest="allow_fallback",
        help="하드페일 가드 우회 — ollama/1024d 아니어도 강제 진행(테스트/오프라인 전용, 실사용 금지)",
    )
    ap.add_argument("--batch-size", type=int, default=BATCH_SIZE, dest="batch_size", help="임베딩 배치 크기")

    sub = ap.add_subparsers(dest="cmd")
    p_tri = sub.add_parser("triples", help="knowledge-hub/triple-map.md → ontology_triples 컬렉션 시드")
    p_tri.add_argument("--rebuild", action="store_true")
    p_tri.add_argument("--allow-fallback", action="store_true", dest="allow_fallback")
    p_tri.add_argument("--batch-size", type=int, default=BATCH_SIZE, dest="batch_size")

    args = ap.parse_args()

    async def _run() -> None:
        load_dotenv()
        if args.cmd == "triples":
            await seed_triples(
                rebuild=args.rebuild, allow_fallback=args.allow_fallback, batch_size=args.batch_size,
            )
        else:
            await seed_memory(
                limit=args.limit, offset=args.offset, rebuild=args.rebuild,
                allow_fallback=args.allow_fallback, batch_size=args.batch_size,
            )

    asyncio.run(_run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
