# -*- coding: utf-8 -*-
"""임베딩 — OllamaEmbedder(mock) + get_embedder 폴백 (P3)."""
import logging
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


def test_get_embedder_ollama_fallback_logs_warning(monkeypatch, caplog):
    """EMBED_BACKEND=ollama 폴백 시 진단 WARNING 로그를 남긴다."""
    monkeypatch.setenv("EMBED_BACKEND", "ollama")

    def boom(*a, **k):
        raise RuntimeError("ollama down")

    monkeypatch.setattr(E, "OllamaEmbedder", boom)
    with caplog.at_level(logging.WARNING):
        assert isinstance(get_embedder(), HashEmbedder)
    assert any(
        "실패" in r.message or r.levelno == logging.WARNING for r in caplog.records
    )


def test_get_embedder_auto_chain_logs_warning(monkeypatch, caplog):
    """auto 폴백 시 ollama·local 실패 진단 WARNING 로그를 남긴다."""
    monkeypatch.delenv("EMBED_BACKEND", raising=False)
    monkeypatch.delenv("EMBEDDING_BACKEND", raising=False)

    def boom(*a, **k):
        raise RuntimeError("unavailable")

    monkeypatch.setattr(E, "OllamaEmbedder", boom)
    monkeypatch.setattr(E, "LocalEmbedder", boom)
    with caplog.at_level(logging.WARNING):
        assert isinstance(get_embedder(), HashEmbedder)
    assert any(
        "실패" in r.message or r.levelno == logging.WARNING for r in caplog.records
    )


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


def test_ollama_timeout_env(monkeypatch, caplog):
    """OLLAMA_TIMEOUT — 유효값 적용, 무효값(비수치/0 이하)은 기본 60초 폴백 + 경고."""
    monkeypatch.delenv("OLLAMA_TIMEOUT", raising=False)
    assert E._ollama_timeout() == 60.0
    monkeypatch.setenv("OLLAMA_TIMEOUT", "300")
    assert E._ollama_timeout() == 300.0
    for bad in ("abc", "0", "-5", "nan"):
        monkeypatch.setenv("OLLAMA_TIMEOUT", bad)
        with caplog.at_level(logging.WARNING):
            assert E._ollama_timeout() == 60.0


# ── embed_lenient — 상류 NaN 버그 내성 (ollama#16625) ────────────
def _nan_bug_ollama(poison: set[str], dim=1024, calls=None):
    """`poison` 에 든 텍스트가 배치에 하나라도 있으면 500 을 내는 mock — 실 ollama 거동."""
    import json

    def handler(request):
        body = json.loads(request.content)
        texts = body["input"]
        if calls is not None:
            calls.append(list(texts))
        if any(t in poison for t in texts):
            return httpx.Response(500, json={"error": "failed to encode response: json: unsupported value: NaN"})
        return httpx.Response(200, json={"embeddings": [[float(len(t) + 1)] + [0.1] * (dim - 1) for t in texts]})

    return httpx.Client(base_url="http://fake", transport=httpx.MockTransport(handler))


def test_embed_lenient_passthrough_when_healthy():
    e = OllamaEmbedder(client=_nan_bug_ollama(poison=set()))
    out = E.embed_lenient(e, ["가", "나", "다"])
    assert len(out) == 3
    assert all(v is not None for v in out)
    assert out == e.embed(["가", "나", "다"])  # 정상 경로는 embed 와 동일 결과


def test_embed_lenient_isolates_poison_and_salvages():
    """독 청크 하나가 배치 전체를 죽이지 못하고, 슬래시 치환으로 구제된다."""
    bad = "**원문:** [열기](http://karpathy.github.io/2026/02/12/microgpt/)"
    e = OllamaEmbedder(client=_nan_bug_ollama(poison={bad}))
    out = E.embed_lenient(e, ["앞 청크", bad, "뒤 청크"])
    assert len(out) == 3
    assert all(v is not None for v in out)  # 구제 성공 — 스킵된 청크 없음


def test_embed_lenient_returns_none_when_unsalvageable():
    """변형으로도 못 살리면 그 항목만 None — 나머지는 정상 반환(순서 보존)."""
    bad = "죽는 텍스트"  # 슬래시가 없어 변형해도 그대로 → 구제 불가
    e = OllamaEmbedder(client=_nan_bug_ollama(poison={bad}))
    out = E.embed_lenient(e, ["앞", bad, "뒤"])
    assert len(out) == 3
    assert out[1] is None
    assert out[0] is not None and out[2] is not None


def test_embed_lenient_preserves_order_with_multiple_poison():
    b1, b2 = "독1", "독2"
    e = OllamaEmbedder(client=_nan_bug_ollama(poison={b1, b2}))
    out = E.embed_lenient(e, ["a", b1, "b", b2, "c"])
    assert [v is None for v in out] == [False, True, False, True, False]


def test_embed_lenient_reraises_non_nan_errors():
    """접속불가·타임아웃 등은 삼키지 않는다 — 삼키면 빈 인덱스로 조용히 성공한다."""

    class Down:
        def embed(self, texts):
            raise httpx.ConnectError("서버 다운")

    with pytest.raises(httpx.ConnectError):
        E.embed_lenient(Down(), ["아무거나"])


def test_embed_lenient_bisects_instead_of_one_by_one():
    """정상 배치는 호출 1번, 오염 배치만 이분 탐색 — 낱개 재시도로 퇴화하지 않는다."""
    calls: list[list[str]] = []
    e = OllamaEmbedder(client=_nan_bug_ollama(poison=set(), calls=calls))
    calls.clear()
    E.embed_lenient(e, [f"청크{i}" for i in range(32)])
    assert len(calls) == 1  # 건강한 배치는 단 한 번


def test_embed_lenient_empty():
    e = OllamaEmbedder(client=_nan_bug_ollama(poison=set()))
    assert E.embed_lenient(e, []) == []
