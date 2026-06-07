# -*- coding: utf-8 -*-
"""yohan-mcp v2 — Smart Router (P2).

query → 칠 백엔드 선택 → 병렬 search → RRF(Reciprocal Rank Fusion, k=60)로 통합 랭킹.

RRF 공식: 각 백엔드 결과의 순위 rank(1부터) 를 1/(k+rank) 로 변환 후
같은 id 끼리 합산. k=60 은 멤베이스 검증 파라미터.
"""
from __future__ import annotations

import asyncio

from adapters.base import BackendAdapter

RRF_K = 60


class SmartRouter:
    def __init__(self, adapters: dict[str, BackendAdapter], k: int = RRF_K) -> None:
        self.adapters = adapters
        self.k = k

    # ── 백엔드 선택 ──────────────────────────────────────────────
    def select_backends(self, query: str, opts: dict | None = None) -> list[str]:
        """칠 백엔드 이름 목록 결정.

        - opts["backends"] 가 주어지면 그대로 사용(존재하는 것만).
        - 그 외에는 등록된 notion(키워드)·memory(파일)·qdrant(의미유사도)를 모두 선택.
          P2.5 부터 qdrant search 가 실동작하므로 3중 RRF 융합이 활성된다.
          (미구현/장애 백엔드는 _safe_search 가 skip 처리.)
        - query 기반 의도 라우팅(키워드/파일/관계 분기)은 향후 도입 예정
          — 현재 query 인자는 선택에 사용하지 않는다.
        """
        opts = opts or {}
        if opts.get("backends"):
            return [b for b in opts["backends"] if b in self.adapters]
        return [b for b in ("notion", "memory", "qdrant") if b in self.adapters]

    # ── 통합 검색 ───────────────────────────────────────────────
    async def search(self, query: str, opts: dict | None = None) -> dict:
        """선택된 백엔드를 병렬 검색 후 RRF 통합.

        반환: {"results": [...], "sources_used": [...], "errors": {backend: msg}}
        각 result: {id, type, backend, data, rrf_score, sources:[backend...]}
        """
        names = self.select_backends(query, opts)
        tasks = [self._safe_search(n, query, opts) for n in names]
        per_backend = await asyncio.gather(*tasks)

        sources_used: list[str] = []
        errors: dict[str, str] = {}
        ranked_lists: list[tuple[str, list[dict]]] = []
        for name, (records, err) in zip(names, per_backend):
            if err is not None:
                errors[name] = err
                continue
            sources_used.append(name)
            ranked_lists.append((name, records))

        fused = self._rrf_fuse(ranked_lists)
        return {"results": fused, "sources_used": sources_used, "errors": errors}

    async def _safe_search(self, name: str, query: str, opts: dict | None):
        """개별 백엔드 검색 — 예외/미구현은 잡아서 (records, error) 로 변환."""
        adapter = self.adapters[name]
        try:
            records = await adapter.search(query, opts)
            return records, None
        except NotImplementedError:
            return [], f"{name}: search 미구현(P2.5 예정) — skip"
        except Exception as exc:  # 백엔드 장애가 전체 검색을 깨지 않게
            return [], f"{name}: {type(exc).__name__}: {exc}"

    def _rrf_fuse(self, ranked_lists: list[tuple[str, list[dict]]]) -> list[dict]:
        """여러 백엔드의 순위 리스트를 RRF 로 합산해 내림차순 정렬.

        융합 키는 '타입::id' 라 서로 다른 타입이 우연히 같은 id 를 가져도
        한 결과로 잘못 합쳐지지 않는다(provenance 무결성 보존).
        """
        agg: dict[str, dict] = {}
        for backend, records in ranked_lists:
            for rank, rec in enumerate(records, start=1):
                rid = rec.get("id")
                key = f"{rec.get('type')}::{rid}"  # 논리 엔티티 키
                contrib = 1.0 / (self.k + rank)
                if key not in agg:
                    agg[key] = {
                        "id": rid,
                        "type": rec.get("type"),
                        "backend": rec.get("backend", backend),
                        "data": rec.get("data", {}),
                        "rrf_score": 0.0,
                        "sources": [],
                    }
                agg[key]["rrf_score"] += contrib
                if backend not in agg[key]["sources"]:
                    agg[key]["sources"].append(backend)
        # 점수 내림차순, 동점은 (타입, id) 안정 정렬
        return sorted(agg.values(), key=lambda r: (-r["rrf_score"], str(r["type"]), str(r["id"])))
