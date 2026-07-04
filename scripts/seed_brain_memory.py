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

증분(U8): memory 서브커맨드는 파일별 sha256(원본 바이트)을 로컬 매니페스트
`.cache/brain_seed_manifest.json`(mcp 레포 내, gitignore)에 기록한다. 재실행 시 hash 불변
파일은 임베딩 없이 스킵하고 변경/신규만 재임베딩한다 — 전량(~25분)이 무변경 재실행 시
수 초로 준다. 삭제된 파일·축소된 파일의 잔존(stale) 포인트는 **목록만 보고**하고 자동
삭제하지 않는다(삭제는 사람 판단). `--full` 로 hash 스킵 없이 전량 재임베딩을 강제한다.
매니페스트는 collection/embedder/dim/base 가 현재 실행과 다르면 통째로 무효 처리(전량).

사용법:
  python scripts/seed_brain_memory.py                    # brain_memory 증분 시드(기본)
  python scripts/seed_brain_memory.py --full             # hash 스킵 없이 전량 재임베딩
  python scripts/seed_brain_memory.py --limit 20         # 파일 20건만(테스트용)
  python scripts/seed_brain_memory.py --rebuild          # 컬렉션 삭제 후 재생성(매니페스트 리셋)
  python scripts/seed_brain_memory.py triples            # ontology_triples 시드
  python scripts/seed_brain_memory.py triples --rebuild
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
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
from core.paths import ROOT, resolve_memory_dir
from core.triple_map import parse_triple_map, sentence_for_triple

logger = logging.getLogger(__name__)

# 시딩 전용 uuid5 네임스페이스 — qdrant_adapter._URL_NS(리소스 point_id) 와 겹치지 않는 별도 공간.
_NS = uuid.uuid5(uuid.NAMESPACE_URL, "yohan-mcp/brain-memory-seed")

BATCH_SIZE = 32  # ollama 배치 임베딩 크기(진행률 출력 겸용) — 첫 배치가 느리면 줄여서 재시도.

# U8 — 증분 시딩 매니페스트(파일 상대경로 → sha256/청크수). memory/ 밖(mcp 레포 내 .cache/,
# gitignore)에 두어 brain SoT 를 오염시키지 않는다. CLI 기본 경로 — 라이브러리 호출(테스트)은
# manifest_path 를 직접 주입하거나 None(증분 비활성·현행 전량 동작)으로 쓴다.
DEFAULT_MANIFEST_PATH = ROOT / ".cache" / "brain_seed_manifest.json"
_MANIFEST_VERSION = 1


def _load_manifest(path: Path | None, *, collection: str, embedder: str, dim: int, base: str) -> dict:
    """매니페스트 로드 + 환경 정합 검증. 없거나 무효면 빈 files 로 시작(=전량 처리).

    collection/embedder/dim/base 중 하나라도 현재 실행과 다르면 그 매니페스트의 hash 는
    "지금 Qdrant 컬렉션에 이 임베딩이 들어있다"는 증거가 못 되므로 통째로 버린다.
    """
    empty = {
        "version": _MANIFEST_VERSION, "collection": collection,
        "embedder": embedder, "dim": dim, "base": base, "files": {},
    }
    if path is None or not path.exists():
        return empty
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"매니페스트 손상 → 전량 처리: {path} ({type(exc).__name__}: {exc})")
        return empty
    for key, want in (
        ("version", _MANIFEST_VERSION), ("collection", collection),
        ("embedder", embedder), ("dim", dim), ("base", base),
    ):
        if data.get(key) != want:
            print(f"매니페스트 환경 불일치({key}: {data.get(key)!r} != {want!r}) → 전량 처리")
            return empty
    files = data.get("files")
    if not isinstance(files, dict):
        return empty
    # 항목 shape 방어 — 손편집 등으로 비dict 항목이 섞이면 그 항목만 버린다(전량 재처리 유도).
    data["files"] = {k: v for k, v in files.items() if isinstance(v, dict)}
    return data


def _save_manifest(path: Path, manifest: dict) -> None:
    """원자적 저장(tmp 쓰기 → replace) — 크래시로 잘린 JSON 이 다음 실행을 오염시키지 않게."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


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
    full: bool = False,
    manifest_path: "Path | None" = None,
    qdrant: "QdrantAdapter | None" = None,
    base_dir: "Path | None" = None,
) -> dict:
    """brain memory/ allowlist .md 전수 → 청킹 → 임베딩 → brain_memory 컬렉션 upsert(멱등).

    qdrant/base_dir 주입 지점 — 테스트가 :memory: Qdrant + tmp_path 로 격리 실행할 수 있게 한다.
    offset/limit — 대량 재실행 시 파일 목록을 슬라이스해 여러 번에 나눠 돌릴 수 있게 한다(예:
    CPU 임베딩이 느려 1회 실행이 오래 걸릴 때). point id 가 경로+청크인덱스 결정적이라 슬라이스를
    나눠 돌려도 멱등 — 어느 슬라이스를 몇 번 반복해도 안전하다.

    U8 증분 — manifest_path 지정 시 파일별 sha256 을 매니페스트에 기록하고, 재실행에서
    hash 불변 파일은 임베딩 없이 스킵한다(full=True 면 스킵 없이 전량 재임베딩). 매니페스트
    갱신은 파일 단위 원자적: 파일의 모든 청크가 Qdrant upsert 로 반영된 flush 시점에만
    기록·저장되므로 중간 크래시 후 재실행이 미완 파일을 자동으로 다시 임베딩한다.
    삭제된 파일·청크 수가 줄어든 파일의 잔존(stale) 포인트는 목록으로 보고만 하고 절대
    자동 삭제하지 않는다(삭제는 사람 판단). manifest_path=None 이면 증분 비활성(현행 전량).
    반환: {"embedder","dim","files","skipped_files","chunks","collection",
           "new_files","reembedded_files","unchanged_files","deleted","stale_chunks","manifest"}.
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

        # ── U8 매니페스트 — collection/embedder/dim/base 불일치면 통째 무효(전량) ──
        manifest = _load_manifest(
            manifest_path, collection=qdrant.collection,
            embedder=qdrant.embedder.name, dim=qdrant.embedder.dim,
            base=str(base.resolve()),
        )
        if rebuild:
            manifest["files"] = {}  # 컬렉션을 지웠으므로 과거 적재 증거도 무효
            if manifest_path is not None:
                _save_manifest(manifest_path, manifest)
        known: dict[str, dict] = manifest["files"]
        # ── 외부 wipe 가드 (critic MAJOR, 2026-07-05): 매니페스트는 "과거에 적재했다"는
        # 주장일 뿐 실제 포인트 존재 증거가 아니다. 컬렉션이 외부에서 초기화(volume wipe·
        # 드롭 후 재생성 등)됐는데 매니페스트만 남으면 증분 시드가 전부 "무변경 스킵"으로
        # 오판해 brain_memory 가 빈 채 exit 0 → 검색 silent 전멸. 매니페스트가 적재 이력을
        # 주장하는데 실제 포인트가 0 이면 매니페스트를 통째 무효화하고 전량 처리한다.
        if known:
            actual_points = (await client.count(qdrant.collection)).count
            if actual_points == 0:
                print(
                    f"⚠️ 매니페스트는 {len(known)}건 적재를 주장하나 컬렉션 '{qdrant.collection}' "
                    "실제 포인트 0 — 외부 초기화 감지, 매니페스트 무효화 후 전량 시딩"
                )
                manifest["files"] = {}
                known = manifest["files"]
        if manifest_path is not None:
            print(f"{'전량(--full)' if full else '증분'} 시딩 — 매니페스트 {manifest_path} (기존 {len(known)}건)")

        all_files = list(_iter_brain_md_files(base))
        files = all_files[offset:]
        if limit:
            files = files[:limit]
        print(
            f"대상 파일: {len(files)}건(전체 {len(all_files)}건 중 offset={offset}) "
            f"(allowlist: {', '.join(_BRAIN_KNOWLEDGE_DIRS)})"
        )
        # 삭제 감지는 슬라이스 전 디스크 전수 기준 — limit/offset 분할 실행이 삭제로 오인되지 않게.
        disk_rels = {str(p.relative_to(base)).replace("\\", "/") for _, p in all_files}

        total_chunks = 0
        skipped_files = 0
        n_new = n_reembed = n_unchanged = 0
        stale_chunks: list[dict] = []
        pending_meta: list[tuple[str, dict]] = []
        pending_texts: list[str] = []
        staged_entries: dict[str, dict] = {}  # 전 청크가 pending 에 들어간 파일만 스테이징

        async def _flush() -> None:
            nonlocal pending_meta, pending_texts, total_chunks
            if pending_texts:
                vecs = await asyncio.to_thread(qdrant.embedder.embed, pending_texts)
                points = [
                    models.PointStruct(id=pid, vector=vecs[i], payload=payload)
                    for i, (pid, payload) in enumerate(pending_meta)
                ]
                await client.upsert(qdrant.collection, points=points)
                total_chunks += len(points)
                pending_meta = []
                pending_texts = []
            # 증분 커밋 — 이 시점에 staged 파일의 모든 청크는 위 upsert 로 반영 완료.
            # (스테이징은 파일의 전 청크가 pending 에 들어간 뒤에만 하므로, pending 을
            # 전부 비운 지금은 staged 파일 전부가 Qdrant 에 존재한다 — 파일 단위 원자성.)
            if staged_entries:
                known.update(staged_entries)
                staged_entries.clear()
                if manifest_path is not None:
                    _save_manifest(manifest_path, manifest)

        for i, (kdir, path) in enumerate(files, 1):
            rel = str(path.relative_to(base)).replace("\\", "/")
            try:
                raw = path.read_bytes()
            except OSError:
                skipped_files += 1
                print(f"  스킵 [{i}/{len(files)}] {path}: 읽기 실패")
                continue
            sha = hashlib.sha256(raw).hexdigest()
            prev = known.get(rel)
            if not full and prev is not None and prev.get("sha256") == sha:
                n_unchanged += 1  # hash 불변 — 임베딩/upsert 없이 스킵(U8)
                continue
            rec = MemoryAdapter._read_md_uncapped(path)
            if rec is None:
                skipped_files += 1
                print(f"  스킵 [{i}/{len(files)}] {path}: 읽기 실패")
                continue
            body = rec.get("body", "")
            title = _title_of(rel, rec)
            base_line = int(rec.get("_body_start_line", 1))
            chunks = chunk_markdown(body, base_line=base_line)
            if prev is None:
                n_new += 1
            else:
                n_reembed += 1
                old_n = int(prev.get("chunks", 0))
                if len(chunks) < old_n:
                    # 청크 수 축소 — path#chunk{new}..{old-1} 포인트가 옛 내용으로 잔존(stale).
                    stale_chunks.append(
                        {"path": rel, "old_chunks": old_n, "new_chunks": len(chunks)}
                    )
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
            # 파일의 모든 청크가 pending 에 들어간 뒤에만 스테이징(0청크 파일도 기록해
            # 다음 실행에서 재파싱을 건너뛴다) — 커밋은 _flush 가 upsert 완료 후 수행.
            staged_entries[rel] = {"sha256": sha, "chunks": len(chunks)}
            if i % 20 == 0 or i == len(files):
                print(f"  진행 {i}/{len(files)} 파일 (누적 청크 {total_chunks + len(pending_texts)})")

        await _flush()

        # ── stale 보고(자동 삭제 금지 — 삭제는 사람) ──
        deleted = sorted(set(known) - disk_rels)
        if deleted:
            print(f"삭제 감지 {len(deleted)}건 — Qdrant 포인트는 자동 삭제하지 않음(삭제는 사람):")
            for rel in deleted:
                n = int(known[rel].get("chunks", 0))
                print(f"  - {rel} (잔존 포인트 {n}개, id=uuid5(시드NS, '{rel}#chunk0..{max(n - 1, 0)}'))")
        if stale_chunks:
            print(f"축소 파일 stale 청크 {len(stale_chunks)}건 — 자동 삭제하지 않음(삭제는 사람):")
            for s in stale_chunks:
                print(
                    f"  - {s['path']}: 청크 {s['old_chunks']} → {s['new_chunks']} "
                    f"(chunk{s['new_chunks']}..{s['old_chunks'] - 1} 잔존)"
                )

        processed = len(files) - skipped_files - n_unchanged
        print(
            f"완료 — 파일 {processed}건 처리(신규 {n_new}·재임베딩 {n_reembed}·"
            f"무변경 스킵 {n_unchanged}·읽기실패 {skipped_files}), "
            f"청크 {total_chunks}개 적재 → '{qdrant.collection}'"
        )
        return {
            "embedder": qdrant.embedder.name,
            "dim": qdrant.embedder.dim,
            "files": processed,
            "skipped_files": skipped_files,
            "chunks": total_chunks,
            "collection": qdrant.collection,
            "new_files": n_new,
            "reembedded_files": n_reembed,
            "unchanged_files": n_unchanged,
            "deleted": deleted,
            "stale_chunks": stale_chunks,
            "manifest": str(manifest_path) if manifest_path is not None else None,
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
    ap.add_argument("--rebuild", action="store_true", help="컬렉션 삭제 후 재생성(차원 변경 시, 매니페스트 리셋)")
    ap.add_argument(
        "--full", action="store_true",
        help="U8 증분 스킵 없이 전량 재임베딩 강제(매니페스트는 갱신됨, memory 전용)",
    )
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
                full=args.full, manifest_path=DEFAULT_MANIFEST_PATH,  # CLI 는 항상 증분(U8)
            )

    asyncio.run(_run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
