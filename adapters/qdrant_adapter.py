# -*- coding: utf-8 -*-
"""Qdrant 백엔드 어댑터 (P2.5 실동작).

벡터 의미검색. P1 schemas/notion/resource 본문을 임베딩해 적재/검색.
- env QDRANT_URL (예: http://localhost:6333). 미설정 시 임베디드(:memory:) 폴백.
- env QDRANT_COLLECTION (기본 yohan_resources).
- 임베딩은 core.embeddings.get_embedder() (env EMBEDDING_BACKEND).
- point id = uuid5(resource_url) → 재적재 멱등(SoT Key 패턴).
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid

from qdrant_client import AsyncQdrantClient, models

from adapters.base import BackendAdapter, _Timer, health, make_record
from core.embeddings import get_embedder

logger = logging.getLogger(__name__)

COLLECTION = "yohan_resources"
# 관제탑(yohan-control-tower)이 적재하는 읽기전용 4컬렉션 — get_context 검색 대상(bge-m3 1024d 동일 모델).
CONTROL_TOWER_COLLECTIONS = ["knowledge_base", "system_rules", "semantic_cache", "execution_history"]
_URL_NS = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")  # RFC 4122 URL 네임스페이스


class QdrantAdapter(BackendAdapter):
    name = "qdrant"

    def __init__(self, client=None, url: str | None = None, collection: str | None = None, embedder=None) -> None:
        self.url = url if url is not None else os.getenv("QDRANT_URL")
        self.collection = collection or os.getenv("QDRANT_COLLECTION", COLLECTION)
        # 검색 대상 = 쓰기 컬렉션(레거시 호환) + 관제탑 4컬렉션. env QDRANT_SEARCH_COLLECTIONS 로 override.
        sc = os.getenv("QDRANT_SEARCH_COLLECTIONS")
        extra = [c.strip() for c in sc.split(",") if c.strip()] if sc else list(CONTROL_TOWER_COLLECTIONS)
        seen: set[str] = set()
        self.search_collections: list[str] = []
        for c in [self.collection, *extra]:
            if c and c not in seen:
                seen.add(c)
                self.search_collections.append(c)
        self._client = client
        self._owns_client = client is None
        self._embedder = embedder
        self._ensured = False

    @property
    def embedder(self):
        if self._embedder is None:
            self._embedder = get_embedder()
        return self._embedder

    def _get_client(self) -> AsyncQdrantClient:
        if self._client is None:
            if self.url:
                self._client = AsyncQdrantClient(url=self.url)
            else:
                # Docker/서버 없으면 임베디드 메모리 모드 (휘발성, 데모/테스트용)
                self._client = AsyncQdrantClient(location=":memory:")
        return self._client

    @staticmethod
    def point_id(resource_url: str) -> str:
        """resource_url → 결정적 UUID. 재실행 시 동일 id 로 upsert 되어 멱등."""
        return str(uuid.uuid5(_URL_NS, str(resource_url)))

    @staticmethod
    def _text_of(data: dict) -> str:
        """임베딩 대상 텍스트: 제목 + 핵심 인사이트 + 본문/요약."""
        parts = [
            str(data.get("title") or ""),
            " ".join(data.get("key_insights") or []),
            str(data.get("raw_content") or data.get("summary") or ""),
        ]
        return "\n".join(p for p in parts if p)

    async def _embed(self, texts) -> list[list[float]]:
        # 임베딩(동기 CPU/네트워크)을 스레드로 보내 이벤트루프 블로킹 방지
        return await asyncio.to_thread(self.embedder.embed, list(texts))

    async def _collection_dim(self, client, coll: str | None = None) -> int | None:
        try:
            info = await client.get_collection(coll or self.collection)
            return int(info.config.params.vectors.size)
        except Exception as exc:
            logger.warning("Qdrant 컬렉션 차원 조회 실패: %s: %s", type(exc).__name__, exc)
            return None

    async def drop_collection(self) -> bool:
        """컬렉션 삭제(존재 시). 임베더 차원 변경(예: hash384→ollama1024) 후 재생성용."""
        client = self._get_client()
        existed = await client.collection_exists(self.collection)
        if existed:
            await client.delete_collection(self.collection)
        self._ensured = False
        return existed

    async def ensure_collection(self) -> None:
        client = self._get_client()
        if self._ensured:
            return
        if not await client.collection_exists(self.collection):
            await client.create_collection(
                self.collection,
                vectors_config=models.VectorParams(size=self.embedder.dim, distance=models.Distance.COSINE),
            )
        else:
            # 기존 컬렉션 차원 ≠ 임베더 차원이면 명확한 에러(난해한 broadcast 오류 방지)
            existing = await self._collection_dim(client)
            if existing is not None and existing != self.embedder.dim:
                raise RuntimeError(
                    f"Qdrant 컬렉션 '{self.collection}' 차원 불일치: 기존 {existing} "
                    f"!= 임베더 {self.embedder.name}/{self.embedder.dim} — 재시딩 또는 "
                    f"컬렉션/EMBEDDING_BACKEND 변경 필요"
                )
        self._ensured = True

    # ── 적재 ────────────────────────────────────────────────────
    async def create(self, type_: str, data: dict) -> dict:
        """resource 데이터를 벡터 + payload 로 upsert (멱등)."""
        await self.ensure_collection()
        client = self._get_client()
        url = str(data.get("source_url") or data.get("resource_id") or "").strip()
        if not url:
            # 빈 키로 uuid5('') 고정 id → 서로 다른 자료 침묵 덮어쓰기 방지
            raise ValueError(f"point_id 키 없음(source_url·resource_id 모두 빔): title={data.get('title')!r}")
        pid = self.point_id(url)
        vec = (await self._embed([self._text_of(data)]))[0]
        payload = {
            "source_url": url,  # 스키마(resource) 필드명과 일치
            "resource_id": data.get("resource_id"),
            "title": data.get("title"),
            "domain": data.get("domain"),
            "status": data.get("status"),
            "resource_type": data.get("resource_type"),
        }
        await client.upsert(self.collection, points=[models.PointStruct(id=pid, vector=vec, payload=payload)])
        return make_record(str(data.get("resource_id") or pid), "resource", self.name, payload)

    async def update(self, id_: str, data: dict, type_: str | None = None) -> dict:
        # 벡터 스토어는 upsert = update
        return await self.create(type_ or "resource", data)

    # ── 검색 ────────────────────────────────────────────────────
    async def search(self, query: str, opts: dict | None = None) -> list[dict]:
        opts = opts or {}
        top_k = int(opts.get("top_k", 5))
        # 읽기 경로에서는 컬렉션을 생성하지 않는다(ensure_collection 부작용 제거 — MAJOR-1).
        client = self._get_client()
        vec = (await self._embed([query or ""]))[0]
        records: list[dict] = []
        for coll in self.search_collections:
            try:
                res = await client.query_points(coll, query=vec, limit=top_k, with_payload=True)
            except Exception as exc:
                # 컬렉션 미존재/차원불일치(bge-m3 미설치 등) — 건너뛰고 계속.
                # 진단은 health_check(status 도구)가 '차원 불일치 … bge-m3 pull 필요'로 표면화.
                logger.warning("Qdrant 컬렉션 '%s' 검색 실패: %s: %s", coll, type(exc).__name__, exc)
                continue
            # 레거시(yohan_resources)=resource, 관제탑 컬렉션=컬렉션명(스키마 없는 벡터청크 — 검증 제외).
            rtype = "resource" if coll == self.collection else coll
            for p in res.points:
                payload = p.payload or {}
                rid = str(
                    payload.get("notion_page_id")
                    or payload.get("resource_id")
                    or payload.get("source_url")
                    or p.id
                )
                score = float(p.score) if p.score is not None else None  # None-safe(BLOCKER-1)
                records.append(make_record(rid, rtype, self.name, payload, score=score))
        # 컬렉션 간 병합 — score 내림차순이 곧 RRF 입력 순위(base.py 계약). None 은 맨 뒤.
        records.sort(key=lambda r: r["score"] if r["score"] is not None else float("-inf"), reverse=True)
        return records[:top_k]

    # ── health ──────────────────────────────────────────────────
    async def health_check(self) -> dict:
        with _Timer() as t:
            try:
                client = self._get_client()
                counts: dict[str, int] = {}
                dims: set[int] = set()
                for coll in self.search_collections:
                    if await client.collection_exists(coll):
                        counts[coll] = (await client.count(coll)).count
                        d = await self._collection_dim(client, coll)
                        if d is not None:
                            dims.add(d)
            except Exception as exc:
                return health(False, t.elapsed_ms, f"Qdrant 연결 실패: {type(exc).__name__}: {exc}")
            mode = self.url or ":memory:"
            if not counts:
                return health(False, t.elapsed_ms, f"Qdrant [{mode}] 검색 컬렉션 없음 — 시딩 필요 ({', '.join(self.search_collections)})")
            total = sum(counts.values())
            present = ", ".join(f"{k}={v}" for k, v in counts.items())
            # 임베더 정보는 별도 try — 임베더 미초기화가 'Qdrant 연결 실패'로 오인되지 않게
            try:
                edim = self.embedder.dim
                bad = sorted(d for d in dims if d != edim)
                if bad:
                    return health(False, t.elapsed_ms, f"Qdrant [{mode}] 차원 불일치 collection={sorted(dims)} != embed={edim} — bge-m3 pull/재시딩 필요 ({present})")
                embed_info = f", embed={self.embedder.name}/{edim}"
            except Exception as exc:
                embed_info = f", 임베더 미초기화({type(exc).__name__})"
            return health(True, t.elapsed_ms, f"Qdrant OK [{mode}] — {total} points ({present}{embed_info})")

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            try:
                await self._client.close()
            except Exception as exc:
                logger.warning("Qdrant 클라이언트 종료 실패: %s: %s", type(exc).__name__, exc)
            self._client = None
