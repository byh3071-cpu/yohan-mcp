# -*- coding: utf-8 -*-
"""yohan-mcp v2 — MCP 서버 진입점 (P2).

5개 백엔드 Adapter + Smart Router + 의도 기반 도구 10개를 MCP 로 노출.
실행:  python server.py   (stdio MCP)
"""
from __future__ import annotations

import sys
from contextlib import asynccontextmanager

# 콘솔/진단(stderr) 한글 표시용 UTF-8 (P1 교훈).
# stdout 은 MCP stdio 의 JSON-RPC 스트림이며 SDK 가 자체 UTF-8 래퍼를 쓰므로 건드리지 않는다.
# (서버 런타임 경로에서 sys.stdout 으로 print 금지 — 프레이밍 오염 방지.)
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

from core import tools as T
from core.tools import ToolContext

load_dotenv()

ctx = ToolContext.from_env()


@asynccontextmanager
async def _lifespan(_server):
    """서버 종료 시 어댑터의 httpx 클라이언트 등 리소스 정리."""
    try:
        yield
    finally:
        await ctx.aclose_all()


mcp = FastMCP("yohan-mcp", lifespan=_lifespan)


@mcp.tool()
async def search(query: str, opts: dict | None = None) -> dict:
    """통합 검색: 여러 백엔드를 병렬 조회 후 RRF 로 융합한 결과."""
    return await T.tool_search(ctx, query, opts)


@mcp.tool()
async def create(type: str, data: dict) -> dict:
    """타입을 보고 백엔드를 자동선택해 엔티티 생성(스키마 검증 포함)."""
    return await T.tool_create(ctx, type, data)


@mcp.tool()
async def update(id: str, data: dict, type: str | None = None) -> dict:
    """id 엔티티를 부분 갱신(타입 주어지면 부분 검증)."""
    return await T.tool_update(ctx, id, data, type)


@mcp.tool()
async def get_context(query: str, opts: dict | None = None) -> dict:
    """질의 관련 엔티티와 _links 관계를 모아 컨텍스트 구성."""
    return await T.tool_get_context(ctx, query, opts)


@mcp.tool()
async def status() -> dict:
    """5개 백엔드 health_check 한 줄 요약."""
    return await T.tool_status(ctx)


@mcp.tool()
async def run_action(action: str, params: dict | None = None) -> dict:
    """n8n 워크플로 실행 (P4 예정 stub)."""
    return await T.tool_run_action(ctx, action, params)


@mcp.tool()
async def publish(type: str, data: dict | None = None) -> dict:
    """Studio 발행 (P3 예정 stub, 입력 스키마 검증)."""
    return await T.tool_publish(ctx, type, data)


@mcp.tool()
async def ingest(source: str, data: dict | None = None) -> dict:
    """수집 파이프라인 (P3 예정 stub, 입력 스키마 검증)."""
    return await T.tool_ingest(ctx, source, data)


@mcp.tool()
async def plan(goal: str, opts: dict | None = None) -> dict:
    """목표→실행계획 수립 (P4 예정 stub)."""
    return await T.tool_plan(ctx, goal, opts)


@mcp.tool()
async def check(type: str, data: dict | None = None) -> dict:
    """데이터를 P1 스키마로 검증(P2 실동작). data 없으면 알려진 타입 목록."""
    return await T.tool_check(ctx, type, data)


if __name__ == "__main__":
    mcp.run()
