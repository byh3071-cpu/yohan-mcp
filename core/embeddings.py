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

import atexit
import hashlib
import logging
import math
import os
import re
import shutil
import subprocess
import time

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


def _ollama_timeout(default: float = 60.0) -> float:
    """env OLLAMA_TIMEOUT 초 파싱 — 무효값(비수치/0 이하)은 기본 60초 폴백 + 경고.

    대량 시딩 배치(큰 원자 청크×배치)가 CPU 에서 60초를 넘겨 ReadTimeout 으로 죽는
    실측 사례(U8 전량 시드) 때문에 조절 가능하게 한다. env 오타가 시딩을 조용히
    죽이지 않도록 무효값은 예외가 아니라 기본값 폴백이다.
    """
    raw = os.getenv("OLLAMA_TIMEOUT")
    if not raw:
        return default
    try:
        timeout = float(raw)
        if not timeout > 0:  # 0/음수/NaN 전부 거부(NaN 은 비교가 False)
            raise ValueError(raw)
    except ValueError:
        logger.warning("OLLAMA_TIMEOUT 무효(%r) → 기본 %s초", raw, default)
        return default
    return timeout


# ── ollama 온디맨드 기동 ─────────────────────────────────────────
# 상주 금지 정책: ollama 를 부팅 자동시작에 두지 않는다. 임베딩이 실제로 필요해진
# 순간에만 `ollama serve` 를 띄우고, **우리가 띄운 것만** 인터프리터 종료 시 함께
# 내린다(사용자가 손수 켜 둔 서버는 남의 것이므로 건드리지 않는다).
# env OLLAMA_AUTOSTART=0 으로 이 자동 기동을 끌 수 있다.
_spawned: subprocess.Popen | None = None


def _ollama_alive(url: str, timeout: float = 1.5) -> bool:
    """서버 생존 확인 — 모델 로딩과 무관한 가벼운 엔드포인트로 찌른다."""
    import httpx  # lazy

    try:
        return httpx.get(f"{url}/api/version", timeout=timeout).status_code == 200
    except Exception:
        return False


def _find_ollama_exe() -> str | None:
    """ollama 실행파일 위치. PATH → env OLLAMA_EXE → OS 기본 설치 경로 순.

    설치 직후에는 갱신된 PATH 가 이미 떠 있는 프로세스(MCP 서버 등)에 반영되지 않아
    which 가 빈손으로 돌아온다. 기본 설치 경로까지 뒤져야 재로그인 없이 첫 기동이 된다.
    """
    if exe := os.getenv("OLLAMA_EXE"):
        return exe if os.path.isfile(exe) else None
    if found := shutil.which("ollama"):
        return found
    local = os.getenv("LOCALAPPDATA") or ""
    candidates = [
        os.path.join(local, "Programs", "Ollama", "ollama.exe") if local else "",
        r"C:\Program Files\Ollama\ollama.exe",
        "/usr/local/bin/ollama",
        "/opt/homebrew/bin/ollama",
        "/usr/bin/ollama",
    ]
    return next((c for c in candidates if c and os.path.isfile(c)), None)


def _stop_spawned() -> None:
    """우리가 띄운 ollama 만 종료 — 상주 금지 정책의 마무리."""
    global _spawned
    proc, _spawned = _spawned, None
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            logger.debug("ollama 종료 실패(무시)", exc_info=True)


def ensure_ollama_server(url: str, wait_s: float | None = None) -> bool:
    """ollama 가 떠 있으면 True. 없으면 백그라운드로 띄우고 응답할 때까지 기다린다.

    콜드 스타트(첫 질의)에서만 수 초가 들고 이후 요청은 즉시 처리된다. 실패해도
    예외를 내지 않는다 — 호출자(OllamaEmbedder 생성자)가 hash 로 폴백하기 때문.
    """
    global _spawned
    if _ollama_alive(url):
        return True
    if (os.getenv("OLLAMA_AUTOSTART", "1") or "").strip().lower() in ("0", "false", "no"):
        return False
    if _spawned is None or _spawned.poll() is not None:
        exe = _find_ollama_exe()
        if not exe:
            logger.warning("ollama 실행파일을 못 찾음(PATH/OLLAMA_EXE/기본경로) — 자동 기동 생략")
            return False
        flags = 0
        if os.name == "nt":
            # 콘솔 창을 띄우지 않고, 부모의 Ctrl+C 시그널 그룹과 분리해 기동
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0
            )
        _spawned = subprocess.Popen(
            [exe, "serve"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
        )
        atexit.register(_stop_spawned)
        logger.info("ollama 온디맨드 기동 (pid=%s)", _spawned.pid)
    deadline = time.monotonic() + (
        wait_s if wait_s is not None else float(os.getenv("OLLAMA_START_TIMEOUT", "30"))
    )
    while time.monotonic() < deadline:
        if _ollama_alive(url):
            return True
        time.sleep(0.4)
    logger.warning("ollama 기동 대기 초과 — hash 폴백 예정")
    return False


class OllamaEmbedder:
    """로컬 ollama 서버 임베딩 (REST /api/embed, 모델 기본 bge-m3).

    - url   : env OLLAMA_URL (기본 http://localhost:11434)
    - model : env EMBEDDING_MODEL (기본 bge-m3, dim 1024)
    - timeout : env OLLAMA_TIMEOUT 초(기본 60) — 대량 시딩 배치(큰 원자 청크×배치)가
      CPU 에서 60초를 넘겨 ReadTimeout 으로 죽는 실측 사례가 있어 조절 가능하게 한다
      (seed_brain_memory 전량 시드는 OLLAMA_TIMEOUT=300 권장).
    dim 은 생성자에서 1건 임베딩으로 실측한다 → 모델 교체에 자동 적응.
    모델 미설치/서버 다운이면 생성자에서 예외 → get_embedder 가 hash 로 폴백.
    embed 은 동기 호출(qdrant_adapter 가 asyncio.to_thread 로 감쌈).
    """

    name = "ollama"

    def __init__(self, model_name: str | None = None, url: str | None = None, client=None) -> None:
        import httpx  # lazy

        self.model_name = model_name or os.getenv("EMBEDDING_MODEL", "bge-m3")
        self.url = (url if url is not None else os.getenv("OLLAMA_URL", "http://localhost:11434")).rstrip("/")
        # 첫 호출은 모델 로딩이 끼어 느릴 수 있어 타임아웃을 넉넉히 (env OLLAMA_TIMEOUT 조절).
        self._client = client or httpx.Client(base_url=self.url, timeout=_ollama_timeout())
        # 상주 금지 — 서버가 꺼져 있으면 이 시점에 띄운다(실패해도 아래 dim 실측이
        # 예외를 내 get_embedder 가 hash 로 폴백하므로 여기서 막지 않는다).
        if client is None:
            ensure_ollama_server(self.url)
        # dim 실측 — 모델 미설치면 여기서 예외가 나 폴백을 유도한다
        self.dim = len(self._embed_batch(["dim probe"])[0])

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        """ollama /api/embed (배치 input 지원). 실패 시 명확한 예외."""
        payload: dict = {"model": self.model_name, "input": list(texts)}
        # 유휴 시 모델을 VRAM/RAM 에서 내리는 시간. 상주 금지 정책이라 env 로 조절한다
        # (시딩처럼 연속 호출이 이어지는 구간은 길게, 평상시 단발 질의는 짧게).
        if ka := os.getenv("OLLAMA_KEEP_ALIVE"):
            payload["keep_alive"] = ka
        resp = self._client.post("/api/embed", json=payload)
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


# ── 상류 모델 버그 내성 ──────────────────────────────────────────
#
# bge-m3(F16 GGUF)는 특정 토큰 시퀀스에서 attention 수치가 터져 NaN 임베딩을 뱉고,
# Go 의 encoding/json 이 NaN 을 직렬화 못 해 ollama 가 500 을 낸다(ollama#16625, 미해결).
# 완전 결정적이고 내용 의존이라 재시도로는 절대 안 풀린다. 실측 — brain 청크 1,115개 중
# 2개(0.18%)가 여기 걸렸고, 둘 다 RSS 인제스터가 붙이는 `**원문:** [열기](URL)` 푸터였다:
#   **원문:** [열기](http://karpathy.github.io/2026/02/12/microgpt/)   18토큰·60자
# 이 한 청크가 배치 전체를 죽여 전량 시딩이 167/355 에서 4회 연속 멈췄다.
#
# 상류 권장 우회는 슬래시/쉼표 제거다. 위 두 건 모두 슬래시를 공백으로 바꾸면 통과한다.

_SALVAGE_TRANSFORMS: tuple[tuple[str, "object"], ...] = (
    ("슬래시→공백", lambda s: s.replace("/", " ")),
    ("쉼표→공백", lambda s: s.replace("/", " ").replace(",", " ")),
)


def _is_nan_bug_error(exc: Exception) -> bool:
    """ollama 의 NaN 500 인가? 접속불가·타임아웃·404 등은 여기 해당하지 않는다.

    이걸 구분 못 하면 서버가 죽었을 때 전 청크를 낱개로 재시도하다 전부 None 을 반환해
    빈 인덱스로 조용히 성공하는 최악의 실패모드가 된다 — 그런 오류는 그대로 터뜨려야 한다.
    """
    resp = getattr(exc, "response", None)
    return resp is not None and getattr(resp, "status_code", None) == 500


def _embed_one_lenient(embedder, text: str) -> list[float] | None:
    """한 건 임베딩. NaN 버그면 우회 변형으로 구제 시도, 끝내 실패하면 None."""
    try:
        return embedder.embed([text])[0]
    except Exception as exc:
        if not _is_nan_bug_error(exc):
            raise
    for label, transform in _SALVAGE_TRANSFORMS:
        salvaged = transform(text)
        if salvaged == text:
            continue
        try:
            vec = embedder.embed([salvaged])[0]
        except Exception as exc:
            if not _is_nan_bug_error(exc):
                raise
            continue
        # 저장되는 본문은 원문 그대로고 벡터만 변형본에서 나온다 — 검색 정확도가 조금 흔들리지만
        # 청크를 통째로 잃는 것보다 낫다.
        logger.warning("임베딩 NaN 버그 우회(%s): %r", label, text[:80])
        return vec
    logger.error("임베딩 실패 — 우회 불가, 이 청크는 색인에서 빠진다: %r", text[:80])
    return None


def embed_lenient(embedder, texts) -> list[list[float] | None]:
    """`embedder.embed` 의 내성 버전 — 상류 NaN 버그로 죽는 항목만 None 이 된다.

    반환 길이는 입력과 항상 같고 순서도 보존한다(호출자가 인덱스로 짝지을 수 있게).
    정상 배치는 한 번의 embed 호출로 끝나고, 실패했을 때만 이분 탐색으로 범인을 좁힌다.
    ollama 외 임베더(hash/local/openai)는 애초에 이 예외를 안 내므로 사실상 통과 경로다.
    """
    items = list(texts)
    if not items:
        return []
    try:
        return list(embedder.embed(items))
    except Exception as exc:
        if not _is_nan_bug_error(exc):
            raise
    if len(items) == 1:
        return [_embed_one_lenient(embedder, items[0])]
    mid = len(items) // 2
    return embed_lenient(embedder, items[:mid]) + embed_lenient(embedder, items[mid:])


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
