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
# get_context 표출 기본값(#21) — tool_get_context 가 opts 에 주입한다. router 자체 기본은
# 무제한(cap 미적용): raw search(tool_search·protocol step)의 회수를 조용히 줄이지 않기 위함.
PER_PAGE_CAP_DEFAULT = 3


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
        opts = opts or {}
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
        # 페이지 독식 방지(per-page cap) — 융합 '이후'·top_k 절단 '이전'의 표출 다양화(#21).
        # 식별은 청크(#20 회수 보존), 표출 제어는 페이지: 한 notion_page_id 의 cap 초과
        # 청크만 표출에서 제외해 롱페이지가 top_k 를 독식하지 못하게 한다.
        # opts 에 없으면(None 포함) 무제한 — 기본 cap 은 tool_get_context 만 주입한다. ≤0=무제한.
        cap = int(opts.get("per_page_cap") or 0)
        if cap > 0:
            fused = self._cap_per_page(fused, cap)
        # top_k 절단은 RRF 융합 '이후'에만 수행한다 — 백엔드/어댑터가 랭킹 전에 후보를 버리면
        # 융합·다양화 이전에 유효 후보가 폐기되어 재현율/다양성이 급감한다(선절단 금지).
        top_k = int(opts.get("top_k", 5))
        if top_k >= 0:
            fused = fused[:top_k]
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

    @staticmethod
    def _cap_per_page(results: list[dict], cap: int) -> list[dict]:
        """notion_page_id 당 최대 cap 개만 남기고 초과 청크를 표출에서 제외.

        page_id 는 data 안에 있다(qdrant 관제탑 컬렉션 청크). page_id 가 없는
        결과(memory·notion 백엔드 등)는 페이지 개념이 없으므로 cap 을 적용하지
        않고 그대로 통과시킨다 — None 을 한 버킷으로 묶으면 정상 결과가 대량
        탈락하기 때문. 입력이 점수순이므로 남는 것은 페이지별 최상위 청크다.
        """
        counts: dict[str, int] = {}
        kept: list[dict] = []
        for rec in results:
            page_id = (rec.get("data") or {}).get("notion_page_id")
            if page_id is None:
                kept.append(rec)
                continue
            seen = counts.get(page_id, 0)
            if seen < cap:
                counts[page_id] = seen + 1
                kept.append(rec)
        return kept

    def _rrf_fuse(self, ranked_lists: list[tuple[str, list[dict]]]) -> list[dict]:
        """여러 백엔드의 순위 리스트를 RRF 로 합산해 내림차순 정렬.

        융합 키는 '타입::id' 라 서로 다른 타입이 우연히 같은 id 를 가져도
        한 결과로 잘못 합쳐지지 않는다(provenance 무결성 보존).

        RRF 정의상 각 백엔드(시스템)는 한 논리 엔티티당 단 한 번(최선 순위)만
        점수에 기여한다. 단일 백엔드가 같은 엔티티를 여러 청크로 나눠 여러 번
        반환해도(예: qdrant 멀티청크), 그 백엔드에서는 첫(최상위) rank 만 가산해
        '청크 수에 의한 점수 인플레이션'을 막는다. 리스트는 백엔드 내 순위순이므로
        첫 등장 = 최선 순위.
        """
        agg: dict[str, dict] = {}
        for backend, records in ranked_lists:
            counted_keys: set[str] = set()  # 이 백엔드가 이미 가산한 키(멀티청크 중복 가산 차단)
            for rank, rec in enumerate(records, start=1):
                rid = rec.get("id")
                key = f"{rec.get('type')}::{rid}"  # 논리 엔티티 키
                if key in counted_keys:
                    continue  # 같은 백엔드의 동일 엔티티 재등장 — 최선 rank 만 기여
                counted_keys.add(key)
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
