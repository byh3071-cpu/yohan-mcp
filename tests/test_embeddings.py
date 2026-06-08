# -*- coding: utf-8 -*-
"""임베딩 — OllamaEmbedder(mock) + get_embedder 폴백 (P3)."""
import math

import httpx
import pytest

from core import embeddings as E
from core.embeddings import HashEmbedder, OllamaEmbedder, get_embedder


def _mock_ollama(dim=1024, fail=False):
    """ollama /api/embed 를 흉내내는 httpx.Client (입력 개수만큼 dim 벡터 반환)."""
    def handler(request):
        if fail:
            return httpx.Response(200, json={"error": "model 'x' not found"})
        import json
        body = json.loads(request.content)
        n = len(body["input"])
        # 결정적이되 텍스트별로 다른 벡터(첫 성분에 길이 반영)
        embs = [[float(len(t) + 1)] + [0.1] * (dim - 1) for t in body["input"]]
        assert n == len(embs)
        return httpx.Response(200, json={"embeddings": embs})

    return httpx.Client(base_url="http://fake", transport=httpx.MockTransport(handler))


def test_ollama_embedder_dim_and_normalize():
    e = OllamaEmbedder(client=_mock_ollama(dim=1024))
    assert e.name == "ollama" and e.dim == 1024
    vecs = e.embed(["어텐션 메커니즘", "RAG"])
    assert len(vecs) == 2 and len(vecs[0]) == 1024
    for v in vecs:
        assert abs(math.sqrt(sum(x * x for x in v)) - 1.0) < 1e-6  # L2 정규화


def test_ollama_embedder_empty_input():
    e = OllamaEmbedder(client=_mock_ollama())
    assert e.embed([]) == []


def test_ollama_embedder_error_raises():
    with pytest.raises(Exception):
        OllamaEmbedder(client=_mock_ollama(fail=True))  # dim 실측 중 error → 예외


def test_get_embedder_ollama_fallback_to_hash(monkeypatch):
    """EMBED_BACKEND=ollama 인데 서버 다운이면 hash 로 graceful 폴백."""
    monkeypatch.setenv("EMBED_BACKEND", "ollama")

    def boom(*a, **k):
        raise RuntimeError("ollama down")

    monkeypatch.setattr(E, "OllamaEmbedder", boom)
    assert isinstance(get_embedder(), HashEmbedder)


def test_get_embedder_env_alias(monkeypatch):
    """구명칭 EMBEDDING_BACKEND 도 계속 인식(하위호환)."""
    monkeypatch.delenv("EMBED_BACKEND", raising=False)
    monkeypatch.setenv("EMBEDDING_BACKEND", "hash")
    assert get_embedder().name == "hash"


def test_get_embedder_auto_chain_to_hash(monkeypatch):
    """auto: ollama·local 모두 실패하면 최종 hash."""
    monkeypatch.delenv("EMBED_BACKEND", raising=False)
    monkeypatch.delenv("EMBEDDING_BACKEND", raising=False)

    def boom(*a, **k):
        raise RuntimeError("unavailable")

    monkeypatch.setattr(E, "OllamaEmbedder", boom)
    monkeypatch.setattr(E, "LocalEmbedder", boom)
    assert isinstance(get_embedder(), HashEmbedder)
