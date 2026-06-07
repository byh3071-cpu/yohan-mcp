# -*- coding: utf-8 -*-
"""yohan-mcp v2 — 의도 기반 도구 10개 로직 (P2).

MCP 등록(server.py)과 분리해 테스트가 직접 호출할 수 있게 한다.
모든 도구는 검증 메타 봉투를 반환:
    { "data": ..., "verification": {"schema_valid": ...}, "provenance": {"sources_used": [...]} }
"""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
import re
import socket
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

from adapters.base import BackendAdapter
from adapters.memory_adapter import MemoryAdapter
from adapters.n8n_adapter import N8nAdapter
from adapters.notion_adapter import NotionAdapter
from adapters.qdrant_adapter import QdrantAdapter
from adapters.studio_adapter import StudioAdapter
from core.router import SmartRouter
from core.schema_validator import SchemaValidator

ROOT = Path(__file__).resolve().parent.parent


def _envelope(data, schema_valid, sources_used, **extra) -> dict:
    env = {
        "data": data,
        "verification": {"schema_valid": schema_valid},
        "provenance": {"sources_used": sources_used},
    }
    env.update(extra)
    return env


KST = timezone(timedelta(hours=9))


def _now_iso() -> str:
    return datetime.now(KST).isoformat(timespec="seconds")


class _TextExtractor(HTMLParser):
    """script/style 제외하고 텍스트 + <title> 추출하는 stdlib 파서(bs4 회피)."""

    def __init__(self) -> None:
        super().__init__()
        self._skip = False
        self._in_title = False
        self.title = ""
        self._chunks: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip = True
        if tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = False
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._skip:
            return
        if self._in_title:
            self.title += data
            return  # 제목은 본문(_chunks)에 중복 적재하지 않음
        text = data.strip()
        if text:
            self._chunks.append(text)

    @property
    def text(self) -> str:
        return "\n".join(self._chunks)


# <script>/<style> 블록 선제거 (파서 CDATA 모드의 리터럴 </script> 누출 완화)
_SCRIPT_STYLE_RE = re.compile(r"(?is)<(script|style)\b[^>]*>.*?</\1\s*>")


def _extract_html(html: str) -> tuple[str, str]:
    cleaned = _SCRIPT_STYLE_RE.sub(" ", html or "")
    p = _TextExtractor()
    p.feed(cleaned)
    return p.title.strip(), p.text


_MAX_FETCH_BYTES = 2_000_000  # 본문 2MB 상한 (DoS 완화)


async def _assert_public_url(url: str) -> None:
    """SSRF 차단 — http/https 만 허용 + 해석된 IP 가 사설/루프백/링크로컬/예약이면 거부.

    169.254.169.254(클라우드 메타데이터/IMDS), localhost, 사내 IP 접근을 막는다.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"허용되지 않은 스킴: {parsed.scheme!r}")
    host = parsed.hostname
    if not host:
        raise ValueError(f"호스트 없음: {url!r}")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, port, 0, socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError(f"DNS 해석 실패: {host} ({exc})")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            raise ValueError(f"내부/사설 대역 차단(SSRF): {host} → {ip}")


async def _fetch_url(url: str, client: httpx.AsyncClient | None = None) -> tuple[str, str]:
    """URL 본문 → (title, text). 테스트는 이 함수를 monkeypatch 한다.

    SSRF 가드(사설/메타데이터 차단) + 리다이렉트 수동 검증 + 2MB/텍스트 상한.
    """
    own = client is None
    client = client or httpx.AsyncClient(timeout=15.0, follow_redirects=False)
    try:
        current = url
        for _ in range(4):  # 리다이렉트 최대 3회 + 최초 1회
            await _assert_public_url(current)
            async with client.stream("GET", current) as resp:
                if resp.is_redirect:
                    loc = resp.headers.get("location")
                    if not loc:
                        raise ValueError("리다이렉트 Location 없음")
                    current = urljoin(current, loc)  # 다음 루프에서 재검증
                    continue
                resp.raise_for_status()
                ctype = resp.headers.get("content-type", "")
                if ctype and not ctype.startswith("text/"):
                    return (current, "")  # 비텍스트(PDF/이미지 등) graceful skip
                buf = bytearray()
                async for chunk in resp.aiter_bytes():
                    buf += chunk
                    if len(buf) > _MAX_FETCH_BYTES:
                        break  # 크기 상한 — 부분 본문만 사용
                return _extract_html(buf.decode(resp.encoding or "utf-8", errors="replace"))
        raise ValueError("리다이렉트 과다")
    finally:
        if own:
            await client.aclose()


async def _headroom_health() -> dict | None:
    """HEADROOM_URL 설정 시 압축 프록시 헬스 체크(없으면 None — status 에 미표기)."""
    url = os.getenv("HEADROOM_URL")
    if not url:
        return None
    try:
        async with httpx.AsyncClient(timeout=2.0) as c:
            resp = await c.get(url.rstrip("/") + "/health")
        return {"ok": resp.status_code == 200, "detail": f"headroom HTTP {resp.status_code}"}
    except Exception as exc:
        return {"ok": False, "detail": f"headroom 연결 실패: {type(exc).__name__}: {exc}"}


class ToolContext:
    """도구가 의존하는 어댑터·라우터·검증기 묶음."""

    def __init__(self, adapters: dict[str, BackendAdapter], router: SmartRouter, validator: SchemaValidator):
        self.adapters = adapters
        self.router = router
        self.validator = validator

    @classmethod
    def from_env(cls) -> "ToolContext":
        adapters: dict[str, BackendAdapter] = {
            "notion": NotionAdapter(),
            "memory": MemoryAdapter(),
            "qdrant": QdrantAdapter(),
            "studio": StudioAdapter(),
            "n8n": N8nAdapter(),
        }
        validator = SchemaValidator()
        router = SmartRouter(adapters)
        return cls(adapters, router, validator)

    def _links(self) -> list[dict]:
        path = self.validator.dir / "_links.json"
        if not path.exists():
            return []
        return json.loads(path.read_text(encoding="utf-8")).get("links", [])

    async def aclose_all(self) -> None:
        """모든 어댑터의 자체 생성 리소스(httpx 클라이언트) 정리."""
        await asyncio.gather(*[a.aclose() for a in self.adapters.values()], return_exceptions=True)


# ── 실동작 도구 ─────────────────────────────────────────────────
async def tool_search(ctx: ToolContext, query: str, opts: dict | None = None) -> dict:
    """통합 검색 — Router 가 백엔드 선택→병렬 호출→RRF 융합."""
    res = await ctx.router.search(query, opts)
    results = res["results"]
    # 검색은 조회이므로 부분검증(required 미강제) — 제공된 필드 타입 정합만 본다
    valids = []
    for r in results:
        ok, _ = ctx.validator.validate_partial(r.get("type", ""), r.get("data", {}))
        valids.append(ok)
    schema_valid = all(valids) if valids else True
    return _envelope(
        {"results": results, "count": len(results)},
        schema_valid,
        res["sources_used"],
        errors=res["errors"],
    )


async def tool_create(ctx: ToolContext, type_: str, data: dict) -> dict:
    """타입 보고 백엔드 자동선택 + 스키마 검증 후 생성."""
    valid, errors = ctx.validator.validate(type_, data)
    if not valid:
        return _envelope(None, False, [], errors=errors)
    backend = ctx.validator.backend_of(type_)
    if backend is None or backend not in ctx.adapters:
        return _envelope(None, True, [], errors=[f"{type_} 라우팅 대상 백엔드 없음"])
    try:
        rec = await ctx.adapters[backend].create(_short_type(type_), data)
        return _envelope(rec, True, [backend])
    except NotImplementedError as exc:
        # stub 백엔드(studio/n8n/qdrant) — created:False 로 생성 안 됨을 명시
        return _envelope({"status": "stub", "created": False, "note": str(exc)}, True, [backend])
    except Exception as exc:  # 백엔드 장애(ValueError/RuntimeError/httpx 등) 격리 → 봉투
        return _envelope(None, True, [], errors=[f"{backend}: {type(exc).__name__}: {exc}"])


async def tool_update(ctx: ToolContext, id_: str, data: dict, type_: str | None = None) -> dict:
    """id_ 엔티티 갱신 — type_ 주어지면 부분검증 후 해당 백엔드로 라우팅."""
    # type_ 미지정이면 검증을 못 하므로 schema_valid=None(='미검증', stub 도구 관례와 일치)
    schema_valid: bool | None = None
    errors: list[str] = []
    backend = None
    if type_:
        schema_valid, errors = ctx.validator.validate_partial(type_, data)
        backend = ctx.validator.backend_of(type_)
    if schema_valid is False:
        return _envelope(None, False, [], errors=errors)
    # 백엔드 미지정 시 memory 우선(파일 id 조회), 실패하면 notion
    order = [backend] if backend else ["memory", "notion"]
    last_err = None
    for name in [b for b in order if b in ctx.adapters]:
        try:
            rec = await ctx.adapters[name].update(id_, data, type_=type_)
            return _envelope(rec, schema_valid, [name])
        except (FileNotFoundError, NotImplementedError) as exc:
            last_err = f"{name}: {exc}"
        except Exception as exc:  # httpx/ValueError(경로차단 포함) 등 백엔드 장애 격리
            last_err = f"{name}: {type(exc).__name__}: {exc}"
    return _envelope(None, schema_valid, [], errors=[last_err or "갱신 대상 없음"])


async def tool_status(ctx: ToolContext) -> dict:
    """5개 백엔드 health_check 한 줄 요약."""
    names = list(ctx.adapters.keys())
    # health_check 는 예외 금지 계약이지만, 위반해도 부분 결과를 보존
    checks = await asyncio.gather(
        *[ctx.adapters[n].health_check() for n in names], return_exceptions=True
    )
    summaries = []
    details = {}
    for n, h in zip(names, checks):
        if isinstance(h, BaseException):
            h = {"ok": False, "latency_ms": 0, "detail": f"health_check 계약 위반: {type(h).__name__}: {h}"}
        flag = "OK" if h.get("ok") else "FAIL"
        summaries.append(f"{n}: {flag} ({h.get('latency_ms', 0)}ms) — {h.get('detail', '')}")
        details[n] = h
    # Headroom 압축 프록시 헬스 (HEADROOM_URL 설정 시에만 표기)
    hr = await _headroom_health()
    if hr is not None:
        summaries.append(f"headroom: {'ON' if hr['ok'] else 'OFF'} — {hr['detail']}")
        details["headroom"] = hr
    return _envelope({"summaries": summaries, "details": details}, True, names)


async def tool_get_context(ctx: ToolContext, query: str, opts: dict | None = None) -> dict:
    """query 관련 엔티티 + _links.json 관계를 모아 컨텍스트 구성."""
    res = await ctx.router.search(query, opts)
    matches = res["results"]
    matched_types = {r.get("type") for r in matches}
    related = [
        link for link in ctx._links()
        if any(t and t in link.get("source", "") for t in matched_types)
        or any(t and t in link.get("target", "") for t in matched_types)
    ]
    return _envelope(
        {"matches": matches, "related_links": related, "count": len(matches)},
        True,
        res["sources_used"],
        errors=res["errors"],
    )


# ── stub 도구 (P3/P4 예정) ──────────────────────────────────────
def _stub(name: str, phase: str, **payload) -> dict:
    return _envelope({"status": "stub", "tool": name, "phase": phase, **payload}, None, [])


async def tool_run_action(ctx: ToolContext, action: str, params: dict | None = None) -> dict:
    """n8n 워크플로 실행 (P4 예정)."""
    return _stub("run_action", "P4 예정", action=action, params=params or {})


async def tool_publish(ctx: ToolContext, type_: str, data: dict | None = None) -> dict:
    """Studio 발행 (P3 예정). 입력 스키마는 가능하면 검증."""
    schema_valid = None
    if data is not None:
        schema_valid, _ = ctx.validator.validate(type_, data)
    return _envelope({"status": "stub", "tool": "publish", "phase": "P3 예정"}, schema_valid, [])


async def tool_ingest(ctx: ToolContext, source: str, data: dict | None = None) -> dict:
    """URL 수집 → Notion RESOURCE + Qdrant 벡터 + memory ingest 로그 3중 적재.

    _links.json 의 ingested_from(1:1) 관계대로 ingest → resource 를 연결.
    각 백엔드는 독립 격리 — 일부 실패해도 나머지는 적재한다.
    """
    url = source
    try:
        title, text = await _fetch_url(url)
    except Exception as exc:
        return _envelope(None, False, [], errors=[f"fetch 실패: {type(exc).__name__}: {exc}"])

    now = _now_iso()
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    rid, iid = f"res_{digest}", f"ing_{digest}"  # SoT Key — 재수집 멱등
    body = text[:5000]
    resource = {
        "resource_id": rid, "title": title or url, "source_url": url,
        "resource_type": "아티클", "domain": "기타", "status": "신규",
        "raw_content": body, "captured_at": now,
    }
    ingest = {
        # source 는 사람이 식별하는 출처 라벨(호스트명), URL 은 source_url 몫
        "ingest_id": iid, "source": urlparse(url).netloc or url, "source_url": url, "raw": body,
        "ingested_at": now, "target_resource_id": rid,  # ingested_from(1:1)
    }

    sources: list[str] = []
    errors: list[str] = []
    stored: dict = {}
    # Notion RESOURCE → Qdrant 벡터 → memory ingest 로그 순, 각각 격리
    for name, type_, payload in (("notion", "resource", resource), ("qdrant", "resource", resource), ("memory", "ingest", ingest)):
        if name not in ctx.adapters:
            continue
        try:
            rec = await ctx.adapters[name].create(type_, payload)
            sources.append(name)
            stored[name] = {"id": rec.get("id")}
        except Exception as exc:
            errors.append(f"{name}: {type(exc).__name__}: {exc}")

    schema_valid, verrs = ctx.validator.validate("resource", resource)
    return _envelope(
        {"resource_id": rid, "ingest_id": iid, "title": resource["title"], "stored": stored},
        schema_valid, sources, errors=errors + verrs,
    )


async def tool_plan(ctx: ToolContext, goal: str, opts: dict | None = None) -> dict:
    """목표→실행계획 수립 (P4 예정)."""
    return _stub("plan", "P4 예정", goal=goal)


async def tool_check(ctx: ToolContext, type_: str, data: dict | None = None) -> dict:
    """데이터 검증 점검 — 스키마 검증은 P2 에서도 동작."""
    if data is None:
        return _envelope({"known_types": ctx.validator.known_types()}, True, [])
    valid, errors = ctx.validator.validate(type_, data)
    return _envelope({"type": type_, "valid": valid}, valid, [], errors=errors)


def _short_type(type_: str) -> str:
    """'notion:summary' → 'summary' (어댑터 내부는 짧은 타입 사용)."""
    return type_.split(":", 1)[1] if ":" in type_ else type_
