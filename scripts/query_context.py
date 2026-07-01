#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""get_context 빠른 확인 CLI — brain 지식노트가 회수되는지 눈으로 보는 개발 유틸.

사용:  python scripts/query_context.py <검색어>       (기본어: cursor)
예:    .\.venv\\Scripts\\python.exe scripts\\query_context.py cursor
"""
import asyncio
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# scripts/ 에서 직접 실행해도 리포 루트 패키지(core/adapters)를 import
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

from core.tools import ToolContext, tool_get_context  # noqa: E402


async def main(query: str) -> None:
    load_dotenv()
    ctx = ToolContext.from_env()
    try:
        env = await tool_get_context(ctx, query, {"top_k": 15})
        matches = env["data"]["matches"]
        brain = [m for m in matches if str(m["type"]).startswith("brain:")]
        print(f"질의: {query!r} — 전체 {len(matches)}건, brain 지식노트 {len(brain)}건")
        for m in brain:
            print(f"  - {m['type']:16s} {m['data'].get('_path')}")
        if not brain:
            print("  (brain 회수 0 — 단일 토큰으로 검색해 보세요. 다어절은 약함)")
    finally:
        await ctx.aclose_all()


if __name__ == "__main__":
    q = sys.argv[1] if len(sys.argv) > 1 else "cursor"
    asyncio.run(main(q))
