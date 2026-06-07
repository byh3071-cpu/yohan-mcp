# -*- coding: utf-8 -*-
"""Studio 백엔드 어댑터 (P2 stub).

P2 에선 health_check 만 실동작. 발행(publish=create)은 P3 에서 구현.
env STUDIO_API_URL (예: https://studio.example.com/api).
"""
from __future__ import annotations

import os

import httpx

from adapters.base import BackendAdapter, _Timer, health

_PHASE = "P3 예정"


class StudioAdapter(BackendAdapter):
    name = "studio"

    def __init__(self, client: httpx.AsyncClient | None = None, url: str | None = None) -> None:
        self.url = url if url is not None else os.getenv("STUDIO_API_URL")
        self._client = client
        self._owns_client = client is None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self.url or "", timeout=5.0)
        return self._client

    async def search(self, query: str, opts: dict | None = None) -> list[dict]:
        raise NotImplementedError(f"studio.search — {_PHASE}")

    async def create(self, type_: str, data: dict) -> dict:
        raise NotImplementedError(f"studio.create(publish) — {_PHASE}")

    async def update(self, id_: str, data: dict, type_: str | None = None) -> dict:
        raise NotImplementedError(f"studio.update — {_PHASE}")

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def health_check(self) -> dict:
        with _Timer() as t:
            if not self.url:
                return health(False, t.elapsed_ms, "STUDIO_API_URL 미설정")
            try:
                resp = await self._get_client().get("/health")
                ok = resp.status_code == 200
                detail = "Studio OK" if ok else f"HTTP {resp.status_code}"
            except Exception as exc:
                ok, detail = False, f"Studio 연결 실패: {type(exc).__name__}: {exc}"
        return health(ok, t.elapsed_ms, detail)
