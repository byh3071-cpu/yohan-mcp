# -*- coding: utf-8 -*-
"""Qdrant 백엔드 어댑터 (P2 stub).

P2 에선 health_check 만 실동작. 의미검색(search)/벡터적재(create)는 P2.5 에서 구현.
env QDRANT_URL (예: http://localhost:6333).
"""
from __future__ import annotations

import os

import httpx

from adapters.base import BackendAdapter, _Timer, health

_PHASE = "P2.5 예정"


class QdrantAdapter(BackendAdapter):
    name = "qdrant"

    def __init__(self, client: httpx.AsyncClient | None = None, url: str | None = None) -> None:
        self.url = url if url is not None else os.getenv("QDRANT_URL")
        self._client = client
        self._owns_client = client is None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self.url or "", timeout=5.0)
        return self._client

    async def search(self, query: str, opts: dict | None = None) -> list[dict]:
        raise NotImplementedError(f"qdrant.search — {_PHASE}")

    async def create(self, type_: str, data: dict) -> dict:
        raise NotImplementedError(f"qdrant.create — {_PHASE}")

    async def update(self, id_: str, data: dict, type_: str | None = None) -> dict:
        raise NotImplementedError(f"qdrant.update — {_PHASE}")

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def health_check(self) -> dict:
        with _Timer() as t:
            if not self.url:
                return health(False, t.elapsed_ms, "QDRANT_URL 미설정")
            try:
                resp = await self._get_client().get("/healthz")
                ok = resp.status_code == 200
                detail = "Qdrant OK" if ok else f"HTTP {resp.status_code}"
            except Exception as exc:
                ok, detail = False, f"Qdrant 연결 실패: {type(exc).__name__}: {exc}"
        return health(ok, t.elapsed_ms, detail)
