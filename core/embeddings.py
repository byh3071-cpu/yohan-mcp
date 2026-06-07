# -*- coding: utf-8 -*-
"""yohan-mcp v2 — 임베딩 추상화 (P2.5).

백엔드 선택은 env EMBEDDING_BACKEND: auto(기본)|local|openai|hash.
- local  : sentence-transformers (한국어 OK, 비용 0, torch 필요)
- openai : text-embedding-3-small (OPENAI_API_KEY 필요)
- hash   : 의존성 0 결정적 폴백 (의미품질 낮음, 파이프라인/테스트/오프라인용)
- auto   : local 시도 → 실패 시 hash

모든 임베더는 `.name`, `.dim`, `.embed(texts) -> list[list[float]]` 제공.
hash 의 기본 dim 은 384 라 paraphrase-multilingual-MiniLM-L12-v2 와 같아
나중에 모델만 교체해도 컬렉션 차원이 유지된다.
"""
from __future__ import annotations

import hashlib
import math
import os
import re

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


def get_embedder():
    """env 에 따라 임베더 생성. 기본 auto → local 실패 시 hash 폴백."""
    backend = os.getenv("EMBEDDING_BACKEND", "auto").lower()
    if backend == "hash":
        return HashEmbedder()
    if backend == "openai":
        return OpenAIEmbedder()
    if backend == "local":
        return LocalEmbedder()
    # auto
    try:
        return LocalEmbedder()
    except Exception:
        return HashEmbedder()
