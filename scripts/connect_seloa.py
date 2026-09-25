"""One-time interactive SELOA OAuth connection and read-only smoke test.

Run from the PC that hosts yohan-mcp: python scripts/connect_seloa.py
Tokens are stored with Windows DPAPI. This script never asks for a password in
the terminal and never prints an access or refresh token.
"""
from __future__ import annotations

import asyncio
import sys
import webbrowser
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
from dotenv import load_dotenv
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from adapters.seloa_oauth import (  # noqa: E402
    REDIRECT_HOST,
    REDIRECT_PATH,
    REDIRECT_PORT,
    WindowsTokenStorage,
    make_oauth_provider,
)


async def main() -> None:
    import os

    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    url = os.getenv("SELOA_MCP_URL", "").strip()
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise SystemExit("SELOA_MCP_URL must be an HTTPS MCP endpoint without credentials or query parameters.")

    storage = WindowsTokenStorage(url)
    callback: asyncio.Future[tuple[str, str | None]] = asyncio.get_running_loop().create_future()

    async def receive(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = (await asyncio.wait_for(reader.readline(), 5)).decode("ascii", errors="replace")
            target = request_line.split(" ")[1]
            while await asyncio.wait_for(reader.readline(), 5) not in (b"\r\n", b"\n", b""):
                pass
            query = urlsplit(target)
            params = parse_qs(query.query)
            if query.path == REDIRECT_PATH and params.get("code") and params.get("state"):
                if not callback.done():
                    callback.set_result((params["code"][0], params["state"][0]))
                status = "200 OK"
                body = "SELOA 연결 확인 중입니다. 이 탭은 닫아도 됩니다."
            elif query.path == REDIRECT_PATH and params.get("error"):
                if not callback.done():
                    callback.set_exception(ValueError("SELOA authorization was denied or expired"))
                status = "400 Bad Request"
                body = "SELOA 연결이 완료되지 않았습니다. 다시 시작해 주세요."
            else:
                status = "404 Not Found"
                body = "Not found"
            encoded = body.encode("utf-8")
            writer.write(
                f"HTTP/1.1 {status}\r\nContent-Type: text/plain; charset=utf-8\r\n"
                f"Content-Length: {len(encoded)}\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n".encode("ascii")
                + encoded
            )
            await writer.drain()
        except (IndexError, ValueError, TimeoutError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    try:
        server = await asyncio.start_server(receive, REDIRECT_HOST, REDIRECT_PORT)
    except OSError:
        raise SystemExit(f"Local OAuth callback port {REDIRECT_PORT} is unavailable.") from None

    async def show_login(auth_url: str) -> None:
        if not webbrowser.open(auth_url, new=2):
            raise RuntimeError("Could not open the local browser for SELOA OAuth")
        print("브라우저에서 SELOA 연결 승인 화면을 열었습니다.", flush=True)

    async def wait_callback() -> tuple[str, str | None]:
        return await asyncio.wait_for(callback, 300)

    provider = make_oauth_provider(url, storage, show_login, wait_callback)
    try:
        async with server:
            async with asyncio.timeout(360):
                async with httpx.AsyncClient(auth=provider, timeout=httpx.Timeout(300, connect=10)) as client:
                    async with streamable_http_client(url, http_client=client) as (read, write, _):
                        async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=300)) as session:
                            await session.initialize()
                            result = await session.call_tool("seloa_overview", arguments={})
                            if result.isError:
                                raise RuntimeError("SELOA authenticated, but read-only overview returned an error")
        print("SELOA OAuth 연결과 읽기 호출이 성공했습니다.", flush=True)
    except Exception:
        raise SystemExit("SELOA 연결 또는 읽기 확인에 실패했습니다. 토큰이나 비밀번호는 출력하지 않습니다.") from None


if __name__ == "__main__":
    asyncio.run(main())
