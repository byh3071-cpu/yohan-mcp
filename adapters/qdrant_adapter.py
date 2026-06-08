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
import os
import uuid

from qdrant_client import AsyncQdrantClient, models

from adapters.base import BackendAdapter, _Timer, health, make_record
from core.embeddings import get_embedder

COLLECTION = "yohan_resources"
_URL_NS = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")  # RFC 4122 URL 네임스페이스


class QdrantAdapter(BackendAdapter):
    name = "qdrant"

    def __init__(self, client=None, url: str | None = None, collection: str | None = None, embedder=None) -> None:
        self.url = url if url is not None else os.getenv("QDRANT_URL")
        self.collection = collection or os.getenv("QDRANT_COLLECTION", COLLECTION)
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

    async def _collection_dim(self, client) -> int | None:
        try:
            info = await client.get_collection(self.collection)
            return int(info.config.params.vectors.size)
        except Exception:
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
        await self.ensure_collection()
        client = self._get_client()
        vec = (await self._embed([query or ""]))[0]
        res = await client.query_points(self.collection, query=vec, limit=top_k, with_payload=True)
        records = []
        for p in res.points:
            payload = p.payload or {}
            rid = str(payload.get("resource_id") or payload.get("source_url") or p.id)
            records.append(make_record(rid, "resource", self.name, payload, score=float(p.score)))
        return records

    # ── health ──────────────────────────────────────────────────
    async def health_check(self) -> dict:
        with _Timer() as t:
            try:
                client = self._get_client()
                if not await client.collection_exists(self.collection):
                    return health(False, t.elapsed_ms, f"컬렉션 '{self.collection}' 없음 — 시딩 필요")
                cnt = (await client.count(self.collection)).count
                cdim = await self._collection_dim(client)
            except Exception as exc:
                return health(False, t.elapsed_ms, f"Qdrant 연결 실패: {type(exc).__name__}: {exc}")
            mode = self.url or ":memory:"
            # 임베더 정보는 별도 try — 임베더 미초기화가 'Qdrant 연결 실패'로 오인되지 않게
            try:
                edim = self.embedder.dim
                if cdim is not None and edim != cdim:
                    return health(False, t.elapsed_ms, f"Qdrant [{mode}] '{self.collection}' {cnt} points — 차원 불일치 collection={cdim} != embed={edim}, 재시딩 필요")
                embed_info = f", embed={self.embedder.name}/{edim}"
            except Exception as exc:
                embed_info = f", 임베더 미초기화({type(exc).__name__})"
            return health(True, t.elapsed_ms, f"Qdrant OK [{mode}] — '{self.collection}' {cnt} points (dim={cdim}{embed_info})")

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            try:
                await self._client.close()
            except Exception:
                pass
            self._client = None
