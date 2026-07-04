# -*- coding: utf-8 -*-
"""Qdrant 백엔드 어댑터 (P2.5 실동작).

벡터 의미검색. P1 schemas/notion/resource 본문을 임베딩해 적재/검색.
- env QDRANT_URL (예: http://localhost:6333). 미설정 시 임베디드(:memory:) 폴백.
- env QDRANT_COLLECTION (기본 yohan_resources).
- env QDRANT_TYPE_WEIGHTS (JSON dict) — U3 payload type 검색 가중 오버라이드(키 단위 병합).
- 임베딩은 core.embeddings.get_embedder() (env EMBEDDING_BACKEND).
- point id = uuid5(resource_url) → 재적재 멱등(SoT Key 패턴).
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import uuid

from qdrant_client import AsyncQdrantClient, models

from adapters.base import BackendAdapter, _Timer, health, make_record
from core.embeddings import get_embedder

logger = logging.getLogger(__name__)

COLLECTION = "yohan_resources"
# 관제탑(yohan-control-tower)이 적재하는 읽기전용 4컬렉션 — get_context 검색 대상(bge-m3 1024d 동일 모델).
CONTROL_TOWER_COLLECTIONS = ["knowledge_base", "system_rules", "semantic_cache", "execution_history"]
# yohan-mcp 자신이 적재하는 brain 지식 벡터 컬렉션(scripts/seed_brain_memory.py) — 기본 검색 대상 편입.
BRAIN_MEMORY_COLLECTION = "brain_memory"
ONTOLOGY_TRIPLES_COLLECTION = "ontology_triples"
BRAIN_MEMORY_COLLECTIONS = [BRAIN_MEMORY_COLLECTION, ONTOLOGY_TRIPLES_COLLECTION]
_URL_NS = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")  # RFC 4122 URL 네임스페이스

# ── U3 — payload type 별 검색 가중 ────────────────────────────────
# 실측 문제(#34 후속): brain_memory 의미 질의 top-5 를 대량 ingest 청크(RSS 등)가 점유해
# 고가치 문서(rules/decisions/knowledge-hub)가 밀린다(예: "사람 몸에 비유한 시스템 설계"
# 질의에서 기대 문서가 #17). payload 의 `type: "brain:<folder>"` 를 근거로 스코어를
# 곱셈 가중해 컬렉션 병합 정렬 직전에 재순위화한다 — RRF(rank 기반) 융합은 이 정렬
# 순위를 입력으로 받으므로 가중이 최종 랭킹에 그대로 전파된다.
# 값은 골든 쿼리 3종 실측 튜닝: 결정·규칙(1.25) > 허브(1.2) > 위키(1.15) 차등 —
# decisions/rules 를 wiki 보다 위로 둬야 "밤 무인 루프" 질의에서 해당 결정 로그가
# 위키 일반론 청크에 밀리지 않는다(균등 1.2 로는 top-2 에 그침, 실측).
# 매핑에 없는 타입(관제탑 컬렉션·ontology_triples·레거시 resource 등)은 1.0(무가중).
DEFAULT_TYPE_WEIGHTS: dict[str, float] = {
    "brain:decisions": 1.25,
    "brain:rules": 1.25,
    "brain:wiki": 1.15,
    "brain:knowledge-hub": 1.2,
    "brain:ingest": 0.85,
}


def load_type_weights() -> dict[str, float]:
    """DEFAULT_TYPE_WEIGHTS 에 env QDRANT_TYPE_WEIGHTS(JSON dict 문자열)를 키 단위 병합.

    예: QDRANT_TYPE_WEIGHTS='{"brain:ingest": 0.7}' 이면 ingest 감쇠만 0.7 로 바뀌고
    나머지는 기본값 유지. env 가 무효(JSON 파싱 실패·비dict·유한 양수 아닌 가중)면
    env **전체**를 버리고 기본값 사용 + 경고 로그 — 부분 적용으로 "설정했다고 믿는데
    일부만 먹는" 상태를 만들지 않는다.
    """
    weights = dict(DEFAULT_TYPE_WEIGHTS)
    raw = os.getenv("QDRANT_TYPE_WEIGHTS")
    if not raw:
        return weights
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise TypeError(f"JSON dict 아님: {type(parsed).__name__}")
        merged: dict[str, float] = {}
        for k, v in parsed.items():
            w = float(v)
            if not math.isfinite(w) or w <= 0:
                raise ValueError(f"가중 {k}={v!r} 무효 — 유한 양수만 허용")
            merged[str(k)] = w
    except Exception as exc:
        logger.warning(
            "QDRANT_TYPE_WEIGHTS 무시(기본 가중 사용): %s: %s", type(exc).__name__, exc
        )
        return weights
    weights.update(merged)
    return weights


class QdrantAdapter(BackendAdapter):
    name = "qdrant"

    def __init__(self, client=None, url: str | None = None, collection: str | None = None, embedder=None) -> None:
        self.url = url if url is not None else os.getenv("QDRANT_URL")
        self.collection = collection or os.getenv("QDRANT_COLLECTION", COLLECTION)
        # 검색 대상 = 쓰기 컬렉션(레거시 호환) + 관제탑 4컬렉션 + brain 2컬렉션(brain_memory·
        # ontology_triples), 이 기본 목록은 env 유무와 무관하게 항상 유지한다. env
        # QDRANT_SEARCH_COLLECTIONS 는 대체(replace)가 아니라 **추가(append)** — 예전엔 env 를
        # 하나라도 설정하면 기본 컬렉션이 통째로 사라져 get_context 회수가 조용히 줄어드는
        # 풋건이 있었다(MINOR I). 격리가 필요하면 호출자가 생성 후 `.search_collections` 를
        # 직접 덮어써 override 한다(test_integration_qdrant.py 처럼).
        sc = os.getenv("QDRANT_SEARCH_COLLECTIONS")
        env_extra = [c.strip() for c in sc.split(",") if c.strip()] if sc else []
        extra = [*CONTROL_TOWER_COLLECTIONS, *BRAIN_MEMORY_COLLECTIONS, *env_extra]
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
        # U3 — payload type 가중(기본 + env QDRANT_TYPE_WEIGHTS 병합). 생성 시 1회 확정.
        self.type_weights = load_type_weights()

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

    def _type_weight(self, payload: dict) -> float:
        """payload["type"](예: "brain:decisions") 기준 가중. 매핑에 없으면 1.0.

        관제탑 컬렉션·ontology_triples·레거시 resource 는 payload 에 type 키가
        없거나 문자열이 아니므로 자연히 1.0(무가중)이 된다.
        """
        t = payload.get("type")
        return self.type_weights.get(t, 1.0) if isinstance(t, str) else 1.0

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
        # 후보풀은 top_k 보다 크게(fetch_k) 확보한다. 최종 top_k 절단은 어댑터/컬렉션 단계가
        # 아니라 router 가 RRF 융합 '이후'에 수행한다(랭킹 전 선절단 금지). 어댑터에서 미리
        # top_k 로 자르면 융합·다양화 이전에 다른 페이지 후보가 폐기되어 재현율/다양성이 급감한다.
        fetch_k = int(opts.get("fetch_k", max(top_k * 5, 50)))
        # 읽기 경로에서는 컬렉션을 생성하지 않는다(ensure_collection 부작용 제거 — MAJOR-1).
        client = self._get_client()
        vec = (await self._embed([query or ""]))[0]
        records: list[dict] = []
        for coll in self.search_collections:
            try:
                res = await client.query_points(coll, query=vec, limit=fetch_k, with_payload=True)
            except Exception as exc:
                # 컬렉션 미존재/차원불일치(bge-m3 미설치 등) — 건너뛰고 계속.
                # 진단은 health_check(status 도구)가 '차원 불일치 … bge-m3 pull 필요'로 표면화.
                logger.warning("Qdrant 컬렉션 '%s' 검색 실패: %s: %s", coll, type(exc).__name__, exc)
                continue
            # 레거시(yohan_resources)=resource, 관제탑 컬렉션=컬렉션명(스키마 없는 벡터청크 — 검증 제외).
            rtype = "resource" if coll == self.collection else coll
            for p in res.points:
                payload = p.payload or {}
                # record id = 청크 고유 point_id. 페이지 공유키(notion_page_id)가 아니라 청크 단위로
                # 식별해야 router 의 dedup 키(type::id)가 같은 페이지의 서로 다른 청크를 하나로
                # 붕괴시키지 않는다(회수 보존). notion_page_id/chunk_index/title 등 페이지 메타는
                # payload(data)에 그대로 남아 provenance·역참조에 쓰인다.
                rid = str(p.id)
                score = float(p.score) if p.score is not None else None  # None-safe(BLOCKER-1)
                # U3 — payload type 가중을 병합 정렬 '직전' score 에 곱해 재순위화.
                # 양수 score 에만 적용 — COSINE 음수 score 에 곱하면 부스트/감쇠가
                # 역전된다(0.85×음수 = 상승). 음수 후보는 원점수 유지(랭킹 하위권).
                if score is not None and score > 0:
                    score *= self._type_weight(payload)
                records.append(make_record(rid, rtype, self.name, payload, score=score))
        # 컬렉션 간 병합 — score 내림차순이 곧 RRF 입력 순위(base.py 계약). None 은 맨 뒤.
        # U3 가중이 이미 곱해진 score 로 정렬하므로 type 부스트/감쇠가 RRF 입력 순위에 반영된다.
        records.sort(key=lambda r: r["score"] if r["score"] is not None else float("-inf"), reverse=True)
        # fetch_k 로만 상한(후보풀 크기). 최종 top_k 절단은 router 가 RRF 융합 이후 적용.
        return records[:fetch_k]

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
