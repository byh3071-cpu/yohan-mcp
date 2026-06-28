# -*- coding: utf-8 -*-
"""yohan-mcp v2 — 임베딩 추상화 (P2.5 → P3).

백엔드 선택은 env EMBED_BACKEND(또는 구명칭 EMBEDDING_BACKEND): auto(기본)|ollama|local|openai|hash.
- ollama : 로컬 ollama 서버(localhost:11434) 실모델 (P3 기본, 한국어 OK, 비용 0, GPU/CPU)
- local  : sentence-transformers (한국어 OK, 비용 0, torch 필요)
- openai : text-embedding-3-small (OPENAI_API_KEY 필요)
- hash   : 의존성 0 결정적 폴백 (의미품질 낮음, 파이프라인/테스트/오프라인용)
- auto   : ollama 시도 → local → hash (인프라 우선순위)

모든 임베더는 `.name`, `.dim`, `.embed(texts) -> list[list[float]]` 제공.
ollama 가 죽었거나 모델 미설치면 hash(dim 384)로 graceful 폴백한다(P3 교훈).
차원이 바뀌면 Qdrant 컬렉션을 재생성해야 한다(seed_qdrant --rebuild).
"""
from __future__ import annotations

import hashlib
import logging
import math
import os
import re

logger = logging.getLogger(__name__)

DEFAULT_DIM = 384


class HashEmbedder:
    """토큰을 해시해 고정차원 벡터에 부호화 + L2 정규화한 결정적 임베더."""

    name = "hash"

    def __init__(self, dim: int = DEFAULT_DIM) -> None:
        self.dim = dim

    def _one(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for tok in re.findall(r"\w+", (text or "").lower()):
            h = int(hashlib.sha256(tok.encode("utf-8")).hexdigest(), 16)
            vec[h % self.dim] += 1.0 if (h >> 8) & 1 else -1.0
        norm = math.sqrt(sum(v * v for v in vec))
        if norm == 0.0:
            # 토큰 0개(빈/문장부호/이모지) → 영벡터는 COSINE 에서 거부됨.
            # 결정적 단위벡터로 폴백(실 Qdrant 서버에서도 안전).
            h = int(hashlib.sha256((text or "∅").encode("utf-8")).hexdigest(), 16)
            vec[h % self.dim] = 1.0
            return vec
        return [v / norm for v in vec]

    def embed(self, texts) -> list[list[float]]:
        return [self._one(t) for t in texts]


class LocalEmbedder:
    """sentence-transformers 기반 (설치돼 있을 때만)."""

    name = "local"

    def __init__(self, model_name: str | None = None) -> None:
        from sentence_transformers import SentenceTransformer  # lazy import

        self.model_name = model_name or os.getenv(
            "EMBEDDING_MODEL", "paraphrase-multilingual-MiniLM-L12-v2"
        )
        self._model = SentenceTransformer(self.model_name)
        self.dim = int(self._model.get_sentence_embedding_dimension())

    def embed(self, texts) -> list[list[float]]:
        vecs = self._model.encode(list(texts), normalize_embeddings=True)
        return [list(map(float, v)) for v in vecs]


class OpenAIEmbedder:
    """OpenAI text-embedding-3-small (OPENAI_API_KEY 필요)."""

    name = "openai"

    def __init__(self, model_name: str | None = None) -> None:
        self.model_name = model_name or os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
        self.api_key = os.getenv("OPENAI_API_KEY")
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY 미설정 — openai 임베딩 불가")
        self.dim = 1536  # text-embedding-3-small

    def embed(self, texts) -> list[list[float]]:
        import httpx

        resp = httpx.post(
            "https://api.openai.com/v1/embeddings",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"model": self.model_name, "input": list(texts)},
            timeout=30.0,
        )
        resp.raise_for_status()
        return [d["embedding"] for d in resp.json()["data"]]


class OllamaEmbedder:
    """로컬 ollama 서버 임베딩 (REST /api/embed, 모델 기본 bge-m3).

    - url   : env OLLAMA_URL (기본 http://localhost:11434)
    - model : env EMBEDDING_MODEL (기본 bge-m3, dim 1024)
    dim 은 생성자에서 1건 임베딩으로 실측한다 → 모델 교체에 자동 적응.
    모델 미설치/서버 다운이면 생성자에서 예외 → get_embedder 가 hash 로 폴백.
    embed 은 동기 호출(qdrant_adapter 가 asyncio.to_thread 로 감쌈).
    """

    name = "ollama"

    def __init__(self, model_name: str | None = None, url: str | None = None, client=None) -> None:
        import httpx  # lazy

        self.model_name = model_name or os.getenv("EMBEDDING_MODEL", "bge-m3")
        self.url = (url if url is not None else os.getenv("OLLAMA_URL", "http://localhost:11434")).rstrip("/")
        # 첫 호출은 모델 로딩이 끼어 느릴 수 있어 타임아웃을 넉넉히
        self._client = client or httpx.Client(base_url=self.url, timeout=60.0)
        # dim 실측 — 모델 미설치면 여기서 예외가 나 폴백을 유도한다
        self.dim = len(self._embed_batch(["dim probe"])[0])

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        """ollama /api/embed (배치 input 지원). 실패 시 명확한 예외."""
        resp = self._client.post("/api/embed", json={"model": self.model_name, "input": list(texts)})
        resp.raise_for_status()
        body = resp.json()
        if "error" in body:
            raise RuntimeError(f"ollama embed 오류: {body['error']}")
        vecs = body.get("embeddings")
        if not vecs:
            raise RuntimeError(f"ollama embed 응답에 embeddings 없음: keys={list(body)}")
        return [_l2_normalize(v) for v in vecs]

    def embed(self, texts) -> list[list[float]]:
        items = list(texts)
        if not items:
            return []
        return self._embed_batch(items)


def _l2_normalize(vec: list[float]) -> list[float]:
    """COSINE 거리는 크기에 불변이지만, hash 임베더와 일관되게 단위벡터로 정규화."""
    norm = math.sqrt(sum(float(v) * float(v) for v in vec))
    if norm == 0.0:
        return [float(v) for v in vec]
    return [float(v) / norm for v in vec]


def get_embedder():
    """env 에 따라 임베더 생성. 기본 auto → ollama→local→hash 순 폴백.

    EMBED_BACKEND(신) 우선, 없으면 EMBEDDING_BACKEND(구) 호환.
    명시 백엔드(ollama 포함)도 실패 시 hash 로 graceful 폴백한다.
    """
    backend = (os.getenv("EMBED_BACKEND") or os.getenv("EMBEDDING_BACKEND") or "auto").lower()
    if backend == "hash":
        return HashEmbedder()
    if backend == "openai":
        return OpenAIEmbedder()
    if backend == "local":
        return LocalEmbedder()
    if backend == "ollama":
        try:
            return OllamaEmbedder()
        except Exception as exc:
            logger.warning("ollama 임베더 생성 실패: %s: %s", type(exc).__name__, exc)
            return HashEmbedder()  # 모델 미설치/서버 다운 → graceful
    # auto: 인프라 우선순위 ollama → local → hash
    for ctor in (OllamaEmbedder, LocalEmbedder):
        try:
            return ctor()
        except Exception as exc:
            logger.warning("%s 임베더 생성 실패(다음 후보로 폴백): %s: %s", getattr(ctor, "name", ctor), type(exc).__name__, exc)
            continue
    return HashEmbedder()
