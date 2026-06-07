# -*- coding: utf-8 -*-
"""yohan-mcp v2 — 공통 Adapter 인터페이스 (P2).

모든 백엔드(Notion·memory·Qdrant·Studio·n8n)는 `BackendAdapter` 를 구현한다.
반환 dict 는 P1 `schemas/` 로 검증 가능한 형태이며, enum 값은
`schemas/_shared-enums.json` 의 $defs 를 따른다.

표준 레코드(Record) 형태 — search/create/update 의 단위 반환:
    {
        "id":      str,          # 엔티티 고유 ID
        "type":    str,          # 스키마 키 (예: "summary", "decision")
        "backend": str,          # 출처 백엔드 이름 (예: "notion")
        "score":   float | None, # 백엔드 자체 관련도 점수(없으면 None; RRF 는 순위 사용)
        "data":    dict,         # 해당 스키마로 검증 가능한 본문
    }
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Any


def make_record(id_: str, type_: str, backend: str, data: dict, score: float | None = None) -> dict:
    """표준 레코드 dict 를 만든다 (Adapter 구현체 공용 헬퍼)."""
    return {"id": id_, "type": type_, "backend": backend, "score": score, "data": data}


def health(ok: bool, latency_ms: int, detail: str) -> dict:
    """health_check 표준 반환 dict."""
    return {"ok": ok, "latency_ms": latency_ms, "detail": detail}


class _Timer:
    """health_check 지연 측정용 컨텍스트 매니저. time.monotonic 만 사용(결정적).

    elapsed_ms 는 property 라 with 블록 안에서 조기 return 해도 안전하게 읽힌다.
    """

    def __enter__(self) -> "_Timer":
        self._start = time.monotonic()
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    @property
    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self._start) * 1000)


class BackendAdapter(ABC):
    """백엔드 어댑터 추상 베이스.

    실동작 어댑터(Notion·memory)는 모든 메서드를 구현한다.
    stub 어댑터(Qdrant·Studio·n8n)는 health_check 만 실동작하고
    search/create/update 는 NotImplementedError 를 던진다(라우터/서버가 우회).
    """

    name: str = "base"

    @abstractmethod
    async def search(self, query: str, opts: dict | None = None) -> list[dict]:
        """query 로 백엔드를 검색해 표준 레코드 리스트를 반환.

        리스트의 순서가 곧 백엔드 내 순위이며, Router 의 RRF 입력이 된다.
        """
        raise NotImplementedError

    @abstractmethod
    async def create(self, type_: str, data: dict) -> dict:
        """type_ 스키마에 해당하는 엔티티를 생성하고 표준 레코드를 반환."""
        raise NotImplementedError

    @abstractmethod
    async def update(self, id_: str, data: dict, type_: str | None = None) -> dict:
        """id_ 엔티티를 data 로 갱신(부분 병합)하고 표준 레코드를 반환.

        type_ 가 주어지면 검증·라우팅에 쓰인 타입을 그대로 사용해
        '검증 타입 ≠ 저장 타입' 분기를 막는다(없으면 백엔드가 추론).
        """
        raise NotImplementedError

    @abstractmethod
    async def health_check(self) -> dict:
        """백엔드 연결성 점검. {"ok": bool, "latency_ms": int, "detail": str} 반환.

        절대 예외를 던지지 않는다 — 실패는 ok=False + detail 로 표현.
        """
        raise NotImplementedError

    async def aclose(self) -> None:
        """자체 생성한 리소스(httpx 클라이언트 등) 정리. 기본은 no-op."""
        return None
