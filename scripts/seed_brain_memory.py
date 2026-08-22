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
import re
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
from core.embeddings import embed_lenient
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


# ── 벡터 색인 전용 확장 소스 ──────────────────────────────────────
# memory_adapter 의 _BRAIN_KNOWLEDGE_DIRS 에서 core 를 뺀 판단("회수 노이즈·config 중복")은
# **substring 회수 기준에선 여전히 맞다** — 문자열이 걸리기만 하면 무조건 결과에 오르니
# config 파일이 노이즈가 된다. 반면 벡터 회수는 유사도 순위라 저관련 문서가 위로 안 올라온다.
# 그래서 공유 allowlist 는 건드리지 않고, 확장은 이 시딩 스크립트(벡터 경로) 안에서만 한다.
# 결과적으로 두 경로의 대상이 갈라지므로 아래 목록이 벡터 색인 범위의 SoT 다.
_VECTOR_EXTRA_MD_DIRS = ("core", "design-intelligence")  # core/*.md — anti-patterns·az-protocol 등 규범 문서
# design-intelligence: 2026-08 신설 — allowlist·제외목록 어디에도 없어 조용히 빠져 있었다(누락 수정).

# 저장소 루트(= memory 의 부모) 기준 색인 폴더 — memory/ 밖이지만 실전 회수 가치가 높은 것만.
# docs/ 246건 전체는 계획서·핸드오프·아카이브가 섞여 노이즈라 넣지 않는다. 규칙("에러 시
# 패턴 사전 먼저 조회")이 지목하는 문서가 검색에 안 걸리던 구멍만 메운다.
_VECTOR_REPO_MD_DIRS = {"docs/patterns": "patterns", "docs/troubleshooting": "troubleshooting"}
_VECTOR_STATE_YAML = ("active-project.yaml", "profile.yaml", "soul.yaml")  # memory/ 루트
_VECTOR_YAML_DIRS = ("core",)  # core/*.yaml — roster·ruleset·projects 등 상태 정본

# yaml 선두 주석(`# 현재 집중 중인 작업`)이 md 헤딩으로 파싱돼 13자짜리 껍데기 청크가
# 생긴다(active-project.yaml 실측). 임베딩 비용만 먹고 회수 가치는 0이라 버린다.
# 실측상 yaml 의 유효 청크는 최소 500자대라 80 은 껍데기만 걸러내는 안전한 하한이다.
_MIN_YAML_CHUNK_CHARS = 80


# ── memory 서브커맨드 ────────────────────────────────────────────
def _iter_brain_source_files(base: Path):
    """벡터 색인 대상을 정렬 순회 — allowlist .md + core/*.md + 상태·core *.yaml.

    yield 하는 kdir 은 payload 의 `type: brain:<kdir>` 로 그대로 쓰인다.
    루트 yaml 은 폴더 순회가 아니라 **파일명 명시**로 집는다 — memory/ 루트를 전수
    순회하면 나중에 생기는 임시·산출물 yaml 까지 조용히 색인에 섞이기 때문이다.
    """
    base_resolved = base.resolve()

    def _inside(p: Path) -> bool:
        # 경로 봉쇄(심링크 등으로 base 밖 탈출 차단)
        return p.resolve().is_relative_to(base_resolved)

    for kdir in (*_BRAIN_KNOWLEDGE_DIRS, *_VECTOR_EXTRA_MD_DIRS):
        root = base / kdir
        if not root.exists():
            continue
        for p in sorted(root.rglob("*.md")):
            if _inside(p):
                yield kdir, p

    repo_root = base_resolved.parent

    def _inside_repo(p: Path) -> bool:
        return p.resolve().is_relative_to(repo_root)

    for rel, kdir in _VECTOR_REPO_MD_DIRS.items():
        root = repo_root / rel
        if not root.exists():
            continue
        for p in sorted(root.rglob("*.md")):
            if _inside_repo(p):
                yield kdir, p

    for name in _VECTOR_STATE_YAML:
        p = base / name
        if p.exists() and _inside(p):
            yield "state", p

    for kdir in _VECTOR_YAML_DIRS:
        root = base / kdir
        if not root.exists():
            continue
        for p in sorted(root.glob("*.yaml")):  # rglob 아님 — core/ 바로 아래만
            if _inside(p):
                yield kdir, p


def _rel_of(p: Path, base: Path) -> str:
    """매니페스트·payload 용 상대경로.

    memory/ 안은 base 기준(`decisions/x.md`), 밖은 저장소 루트 기준(`docs/patterns/x.md`).
    prefix 가 갈리므로 두 출처가 같은 키로 충돌하지 않는다.
    """
    pr, br = Path(p).resolve(), Path(base).resolve()
    try:
        return str(pr.relative_to(br)).replace("\\", "/")
    except ValueError:
        return str(pr.relative_to(br.parent)).replace("\\", "/")


def _title_of(rel_path: str, fm: dict) -> str:
    return str(fm.get("title") or fm.get("id") or Path(rel_path).stem)


# 인제스터(src/ingest/{rss-feed,geeknews,url}.ts)가 모든 수집 문서 끝에 붙이는 원문 역링크.
# 지식 내용이 0인데다 같은 URL 이 frontmatter `source_url` 에 이미 있어 색인에선 순수 노이즈고,
# 실제로 상류 bge-m3 NaN 버그(ollama#16625)를 유발한 청크 2건이 전부 이 푸터였다.
# `\s` 는 개행까지 먹어 줄을 통째로 삼킨다(= 뒤쪽 줄번호 밀림) — 개행 뺀 공백만 허용.
_SOURCE_FOOTER_RE = re.compile(
    r"^\*\*원문:\*\*[ \t]*\[열기\]\([^)\n]*\)[ \t]*$", re.MULTILINE
)


def _blank_source_footers(body: str) -> str:
    """원문 역링크 줄을 빈 줄로 치환 — 색인에서만 빼고 원본 파일은 건드리지 않는다.

    줄을 지우지 않고 비우는 이유: 푸터가 본문 중간에 낄 수 있는데(url.ts 는 두 번 붙인다)
    삭제하면 뒤쪽 줄 번호가 전부 밀려 chunk_start_line 역링크가 어긋난다.
    """
    return _SOURCE_FOOTER_RE.sub("", body)


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

        all_files = list(_iter_brain_source_files(base))
        files = all_files[offset:]
        if limit:
            files = files[:limit]
        md_dirs = ", ".join((*_BRAIN_KNOWLEDGE_DIRS, *_VECTOR_EXTRA_MD_DIRS, *_VECTOR_REPO_MD_DIRS))
        print(
            f"대상 파일: {len(files)}건(전체 {len(all_files)}건 중 offset={offset}) "
            f"(md: {md_dirs} / yaml: {', '.join(_VECTOR_STATE_YAML)}, "
            f"{'·'.join(f'{d}/*.yaml' for d in _VECTOR_YAML_DIRS)})"
        )
        # 삭제 감지는 슬라이스 전 디스크 전수 기준 — limit/offset 분할 실행이 삭제로 오인되지 않게.
        disk_rels = {_rel_of(p, base) for _, p in all_files}

        total_chunks = 0
        skipped_files = 0
        n_new = n_reembed = n_unchanged = 0
        stale_chunks: list[dict] = []
        unembeddable: list[dict] = []  # 상류 NaN 버그로 색인 못 한 청크(ollama#16625)
        pending_meta: list[tuple[str, dict]] = []
        pending_texts: list[str] = []
        staged_entries: dict[str, dict] = {}  # 전 청크가 pending 에 들어간 파일만 스테이징

        async def _flush() -> None:
            nonlocal pending_meta, pending_texts, total_chunks
            if pending_texts:
                # embed 가 아니라 embed_lenient — 상류 bge-m3 NaN 버그(ollama#16625)로 죽는
                # 청크 하나가 배치 전체를, 나아가 전량 시딩을 죽이지 못하게. 못 살린 청크는
                # None 으로 오고 색인에서 빠진다(아래에서 명시 보고).
                vecs = await asyncio.to_thread(
                    embed_lenient, qdrant.embedder, pending_texts
                )
                points = []
                for i, (pid, payload) in enumerate(pending_meta):
                    if vecs[i] is None:
                        unembeddable.append(
                            {"path": payload["path"], "chunk_index": payload["chunk_index"]}
                        )
                        continue
                    points.append(models.PointStruct(id=pid, vector=vecs[i], payload=payload))
                if points:  # 배치 전원이 임베딩 불가면 upsert 할 게 없다
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
            rel = _rel_of(path, base)
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
            is_yaml = path.suffix in (".yaml", ".yml")
            if is_yaml:
                # yaml 엔 frontmatter 가 없다 — 파일 전체가 본문이고 줄 오프셋도 없다(base_line=1).
                # utf-8-sig — 이 PC 도구체인이 BOM 을 붙이는 경우가 있어 ﻿ 가 본문 선두에
                # 섞이면 첫 청크 임베딩이 오염된다. BOM 이 없으면 utf-8 과 동일 동작.
                try:
                    body = raw.decode("utf-8-sig")
                except UnicodeDecodeError:
                    skipped_files += 1
                    print(f"  스킵 [{i}/{len(files)}] {path}: UTF-8 디코드 실패")
                    continue
                title = path.stem
                base_line = 1
            else:
                rec = MemoryAdapter._read_md_uncapped(path)
                if rec is None:
                    skipped_files += 1
                    print(f"  스킵 [{i}/{len(files)}] {path}: 읽기 실패")
                    continue
                body = rec.get("body", "")
                title = _title_of(rel, rec)
                base_line = int(rec.get("_body_start_line", 1))
            chunks = chunk_markdown(_blank_source_footers(body), base_line=base_line)
            if is_yaml:
                # 껍데기 청크 제거(_MIN_YAML_CHUNK_CHARS 주석 참조).
                # md 경로엔 적용하지 않는다 — 기존 청크 수가 바뀌면 매니페스트가 전부
                # "축소된 파일"로 잡혀 stale 보고가 무의미하게 폭발한다.
                chunks = [c for c in chunks if len(c.text.strip()) >= _MIN_YAML_CHUNK_CHARS]
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

        if unembeddable:
            # 조용히 넘어가면 "시딩은 됐는데 그 문서만 영영 안 잡히는" 상태가 된다 — 반드시 보인다.
            print(
                f"임베딩 불가 청크 {len(unembeddable)}건 — 색인에서 빠짐"
                " (상류 bge-m3 NaN 버그 ollama#16625, 우회 변형으로도 실패):"
            )
            for u in unembeddable:
                print(f"  - {u['path']} #chunk{u['chunk_index']}")

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
            "unembeddable": unembeddable,
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
