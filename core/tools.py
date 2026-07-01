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
import logging
import os
import re
import socket
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

from adapters.base import BackendAdapter, make_record
from adapters.memory_adapter import MemoryAdapter
from core.paths import resolve_mcp_runtime_dir
from adapters.n8n_adapter import N8nAdapter
from adapters.notion_adapter import NotionAdapter
from adapters.qdrant_adapter import QdrantAdapter
from adapters.studio_adapter import StudioAdapter
from core.router import SmartRouter
from core.schema_validator import SchemaValidator
from core.links import LinkStore
from core.approval import ApprovalQueue
from core.policy import PolicyEngine
from core import protocols as P
from core import scheduler as S
from core import verify as V

ROOT = Path(__file__).resolve().parent.parent

logger = logging.getLogger(__name__)


def _envelope(data, schema_valid, sources_used, **extra) -> dict:
    """중립(집계/조회) 봉투 — Verifiability Engine 의 표준 봉투로 위임.

    엔티티 도구(create/update/publish/ingest/check)는 type_/validator 를 넘겨
    풀 verification(6항목 점수 등)을 계산한다.
    """
    return V.make_envelope(data, sources_used=sources_used, schema_valid=schema_valid, **extra)


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


def _backend_can_create(adapter) -> bool:
    """어댑터가 실생성 가능한지(미설정이면 tool_create 가 드라이런 폴백).

    `can_create` 속성이 없으면(memory 처럼 항상 가능) True 로 본다.
    """
    cc = getattr(adapter, "can_create", None)
    return True if cc is None else bool(cc)


class CreateStore:
    """create 멱등 저널 — append-only JSONL(`<memory>/created.jsonl`).

    한 줄 = 한 생성 스냅샷 {key, envelope, ts}. 같은 key(=type:pk) 재생성 시
    저장된 봉투를 fold(마지막 우선)해 그대로 돌려준다 — 중복 생성 방지(외부 브로커 0).
    """

    def __init__(self, path: "str | os.PathLike | None" = None) -> None:
        if path is not None:
            self.path = Path(path)
        else:
            base = resolve_mcp_runtime_dir()
            self.path = base / "created.jsonl"

    def _all(self) -> list[dict]:
        if not self.path.exists():
            return []
        out: list[dict] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def get(self, key: str) -> dict | None:
        found = None
        for ev in self._all():
            if ev.get("key") == key:
                found = ev.get("envelope")
        return found

    def put(self, key: str, envelope: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        rec = {"key": key, "envelope": envelope, "ts": _now_iso()}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


class ToolContext:
    """도구가 의존하는 어댑터·라우터·검증기·링크저장소 묶음."""

    def __init__(
        self,
        adapters: dict[str, BackendAdapter],
        router: SmartRouter,
        validator: SchemaValidator,
        links_store: "LinkStore | None" = None,
        approvals_store: "ApprovalQueue | None" = None,
        runs_store: "P.RunStore | None" = None,
        policy: "PolicyEngine | None" = None,
        scheduler: "S.Scheduler | None" = None,
        create_store: "CreateStore | None" = None,
    ):
        self.adapters = adapters
        self.router = router
        self.validator = validator
        base = resolve_mcp_runtime_dir()
        self.links_store = links_store or LinkStore(Path(base) / "links.jsonl")
        # P4 — 승인큐/실행저널(프로토콜 엔진). brain-linked 시 ops/mcp-runtime 하위.
        self.approvals_store = approvals_store or ApprovalQueue(Path(base) / "approvals.jsonl")
        self.runs_store = runs_store or P.RunStore(Path(base) / "runs.jsonl")
        # P6 — create 멱등 저널(같은 SoT Key 재생성 중복 방지)
        self.create_store = create_store or CreateStore(Path(base) / "created.jsonl")
        # P5 — 정책 엔진(감사 로그) + 스케줄러(트리거). 정책 자동승인/사람 폴백.
        self.policy = policy or PolicyEngine(log_path=Path(base) / "policy_log.jsonl")
        self.scheduler = scheduler or S.Scheduler(runlog_path=Path(base) / "trigger_runs.jsonl")

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
        t = r.get("type", "")
        # 스키마 없는 타입(관제탑 벡터청크 등)은 검증 대상 아님 — schema_valid 오염 방지(MAJOR-3).
        if ctx.validator.schema_for(t) is None:
            continue
        ok, _ = ctx.validator.validate_partial(t, r.get("data", {}))
        valids.append(ok)
    schema_valid = all(valids) if valids else True
    return _envelope(
        {"results": results, "count": len(results)},
        schema_valid,
        res["sources_used"],
        errors=res["errors"],
    )


async def tool_create(ctx: ToolContext, type_: str, data: dict) -> dict:
    """타입 보고 백엔드 자동선택 + 스키마 검증 후 생성([실호출]/[드라이런] 분기).

    백엔드가 실생성 불가(예: NOTION_TOKEN 미설정)면 **드라이런으로 폴백**한다 —
    표준봉투의 data(생성될 레코드 ref)·verification·provenance 를 모두 채우되 dry_run:true 로 표식.
    멱등: 같은 SoT Key(type:pk) 재생성 시 CreateStore fold+replay 로 중복 생성하지 않는다.
    """
    valid, errors = ctx.validator.validate(type_, data)
    if not valid:
        return _envelope(None, False, [], errors=errors)
    backend = ctx.validator.backend_of(type_)
    if backend is None or backend not in ctx.adapters:
        return _envelope(None, True, [], errors=[f"{type_} 라우팅 대상 백엔드 없음"])
    adapter = ctx.adapters[backend]
    short = _short_type(type_)

    # SoT Key — 노드 PK 로 멱등 키 구성(없으면 멱등 비활성)
    pk_field = V.NODE_PK.get(f"{backend}:{short}")
    pk = str(data[pk_field]) if pk_field and data.get(pk_field) is not None else None
    key = f"{type_}:{pk}" if pk else None
    if key:
        cached = ctx.create_store.get(key)
        if cached is not None:  # 이미 생성됨 — 저장 봉투 그대로(중복 생성 방지)
            env = dict(cached)
            env["idempotent_replay"] = True
            return env

    def _emit(env: dict) -> dict:
        if key:
            ctx.create_store.put(key, env)
        return env

    # ── 드라이런 분기 — 백엔드 미설정 시 graceful 폴백(예외로 죽지 않음) ──
    if not _backend_can_create(adapter):
        rec = make_record(pk or short, short, backend, data)
        return _emit(V.make_envelope(
            rec, sources_used=[backend], type_=type_, validator=ctx.validator,
            schema_links=ctx._links(), entity=data,
            diff={"before": None, "after": data},
            reasoning_steps=[f"{type_} 스키마 검증 통과",
                             f"{backend} 미설정 — 드라이런(실생성 보류, .env 채우면 실생성 전환)"],
            dry_run=True,
        ))

    # ── 실호출 분기 ──
    try:
        rec = await adapter.create(short, data)
        return _emit(V.make_envelope(
            rec, sources_used=[backend], type_=type_, validator=ctx.validator,
            schema_links=ctx._links(), entity=data,
            diff={"before": None, "after": data},
            reasoning_steps=[f"{type_} 스키마 검증 통과", f"{backend} 백엔드로 생성"],
        ))
    except NotImplementedError as exc:
        # stub 백엔드(n8n 등) — created:False 로 생성 안 됨을 명시
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
            return V.make_envelope(
                rec, sources_used=[name], schema_valid=schema_valid,
                type_=type_, validator=ctx.validator if type_ else None,
                schema_links=ctx._links() if type_ else None,
                entity=rec.get("data") if isinstance(rec, dict) else None,
                diff={"before": None, "after": data},
                reasoning_steps=[f"id={id_} → {name} 백엔드 부분 갱신"],
            )
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
    """query 관련 엔티티 + _links 관계 + Dev Log/패턴 회수를 모아 컨텍스트 구성.

    우선순위(양방향 루프의 회수 측): SOUL/정체성 → active-project(matches/links)
    → devlog(과거 경험) → patterns(재사용 가능 패턴). PHASE 3.
    opts: {project?, category?, tags?} — Dev Log/패턴 회수 키. 없으면 해당 회수는 빈 배열.
    """
    opts = opts or {}
    res = await ctx.router.search(query, opts)
    matches = res["results"]
    matched_types = {r.get("type") for r in matches}
    related = [
        link for link in ctx._links()
        if any(t and t in link.get("source", "") for t in matched_types)
        or any(t and t in link.get("target", "") for t in matched_types)
    ]
    # ── 역방향 회수 — Dev Log/패턴 DB (notion 어댑터, 각각 격리) ──
    notion = ctx.adapters.get("notion")
    project = opts.get("project")
    category, tags = opts.get("category"), opts.get("tags")
    devlog: list[dict] = []
    patterns: list[dict] = []
    devlog_q = getattr(notion, "devlog_query", None)
    pattern_q = getattr(notion, "pattern_query", None)
    if devlog_q and project:
        try:
            devlog = await devlog_q(project)
        except Exception as exc:
            logger.warning("devlog 회수 실패: %s: %s", type(exc).__name__, exc)
            devlog = []  # 회수 실패가 컨텍스트 전체를 죽이지 않음
    if pattern_q and (category or tags):
        try:
            patterns = await pattern_q(category, tags)
        except Exception as exc:
            logger.warning("pattern 회수 실패: %s: %s", type(exc).__name__, exc)
            patterns = []
    sources = list(res["sources_used"])
    if devlog:
        sources.append("notion:devlog")
    if patterns:
        sources.append("notion:pattern")
    env = _envelope(
        {"matches": matches, "related_links": related,
         "devlog": devlog, "patterns": patterns, "count": len(matches)},
        True,
        sources,
        errors=res["errors"],
    )
    # ADR-008 P0 — brain 코어룰셋 주입(옵트인·멱등). 기본 off라 미옵트인 호출자는 봉투 불변.
    inject = bool(opts.get("inject_rules")) or os.getenv("YOHAN_INJECT_CORE_RULES") in ("1", "true", "True")
    if inject:
        from core.core_rules import inject_core_rules
        inject_core_rules(env, enabled=True, capabilities=opts.get("capabilities"))
    return env


async def tool_get_core_ruleset(ctx: ToolContext, capabilities=None) -> dict:
    """brain 코어 독트린 다이제스트 + 사용가능 도구 카탈로그 (ADR-008 P0, 읽기 전용).

    에이전트가 get_context 없이도 독트린을 직접 pull. 출처(brain/snapshot/none) 표기.
    capabilities(허용 도구명) 미부여 시 쓰기 도구는 locked 로 표식(capability gating).
    """
    from core.core_rules import available_tools, build_digest, load_core_ruleset
    ruleset, source = load_core_ruleset()
    digest = build_digest(ruleset)
    digest["source"] = source
    data = {"core_rules_digest": digest, "available_tools": available_tools(capabilities)}
    return _envelope(data, True, [f"core-ruleset:{source}"])


# ── stub 도구 (P3/P4 예정) ──────────────────────────────────────
def _stub(name: str, phase: str, **payload) -> dict:
    return _envelope({"status": "stub", "tool": name, "phase": phase, **payload}, None, [])


# P3 단발 발행 프로토콜(하위호환 유지) + P4 멀티스텝 엔진 프로토콜
_SINGLE_PROTOCOLS = {"publish_summary", "summary_to_post"}
_REGISTERED_PROTOCOLS = sorted(
    _SINGLE_PROTOCOLS | set(P.list_protocols()) | {"ingest"}
)


async def tool_run_action(ctx: ToolContext, action: str, params: dict | None = None, policy=None) -> dict:
    """크로스 백엔드 프로토콜 실행 (P4 엔진 + P5 정책).

    - 멀티스텝 프로토콜(ingest_summarize_publish 등)은 Protocol Engine 으로 위임.
      게이트에서 **정책 평가** → 자동승인이면 통과(완주), 아니면 P4 승인큐로 폴백(pending 봉투).
      이어서 `approve(run_id, 'approve'|'reject')` 로 재개/취소한다.
    - 단발 발행(publish_summary/summary_to_post)·ingest 는 P3 호환 경로 유지.
    - policy 미지정 시 ctx.policy(기본 정책) 적용. 트리거는 자체 정책을 주입한다(P5 스케줄러).
    """
    params = params or {}
    # P4/P5 — 멀티스텝 프로토콜은 엔진이 순차 실행 + 게이트(정책) 처리
    if action in P.PROTOCOLS:
        return await P.run_protocol(ctx, action, params, policy=policy)
    # P3 호환 — 단발 SUMMARY→발행
    if action in _SINGLE_PROTOCOLS:
        inner = await tool_publish(ctx, params.get("summary"))
        data = {
            "protocol": action,
            "post": inner.get("data"),
            "published": inner.get("published"),
            "dry_run": inner.get("dry_run"),
            "published_as": inner.get("published_as"),
            "post_verification": inner.get("verification"),
        }
        return V.make_envelope(
            data, sources_used=inner["provenance"]["sources_used"],
            schema_valid=inner["verification"]["schema_valid"],
            reasoning_steps=["프로토콜: summary → publish", *inner["provenance"].get("reasoning_steps", [])],
            errors=inner.get("errors"),
        )
    if action == "ingest":
        return await tool_ingest(ctx, params.get("url", ""))
    return _stub(
        "run_action", "미등록 프로토콜",
        action=action, known_protocols=_REGISTERED_PROTOCOLS,
    )


async def tool_approve(ctx: ToolContext, run_id: str, decision: str, note: str | None = None) -> dict:
    """승인 게이트 결정 (P4) — 대기 중 프로토콜을 재개(approve) 또는 종료(reject).

    멱등: 이미 종결(완료/거부/중단)된 run 은 저장된 최종 봉투를 그대로 돌려준다(중복 처리 무시).
    """
    journal = ctx.runs_store.latest(run_id)
    if journal is None:
        return _envelope(
            {"status": "unknown_run", "run_id": run_id}, None, [],
            errors=[f"알 수 없는 run_id: {run_id}"],
        )
    st = journal.get("status")
    if st in ("done", "rejected", "aborted"):  # 멱등 — 종결된 run 재처리 무시
        env = dict(journal.get("envelope") or {})
        env["idempotent_replay"] = True
        return env
    if st != "pending":
        return _envelope(
            {"status": "not_pending", "run_id": run_id, "run_status": st}, None, [],
            errors=[f"승인 대기 상태가 아님(run_status={st})"],
        )

    res = ctx.approvals_store.resolve(run_id, decision, note)
    if res.get("status") == "invalid":
        return _envelope(
            {"status": "invalid_decision", "run_id": run_id}, None, [],
            errors=[res.get("note")],
        )

    payload = ctx.approvals_store.payload_for(run_id) or {}
    protocol = payload.get("protocol") or journal.get("protocol")
    step_index = payload.get("step_index", journal.get("next_index", 0))

    if decision == "reject":
        reasoning = ((payload.get("context") or {}).get("_reasoning")) or []
        env = P.rejected_envelope(run_id, protocol, step_index, reasoning, note)
        ctx.runs_store.save({
            "run_id": run_id, "protocol": protocol, "status": "rejected",
            "next_index": step_index, "context": payload.get("context") or {}, "envelope": env,
        })
        return env

    # approve → 게이트 통과 후 다음 step 부터 재개
    return await P.run_protocol(
        ctx, protocol, payload.get("params") or {},
        run_id=run_id, resume=True, approved_index=step_index,
    )


# ── P5 — 스케줄러/정책 도구 ─────────────────────────────────────
async def tool_run_trigger(ctx: ToolContext, trigger_id: str, params: dict | None = None) -> dict:
    """트리거 진입점 (P5) — 트리거 정의를 읽어 정책을 적용해 프로토콜 실행.

    트리거의 policy 로 게이트를 평가한다(자동승인/사람 폴백). 실제 cron/웹훅 구동은 P5-B(배포).
    """
    return await S.run_trigger(ctx, trigger_id, params)


async def tool_list_triggers(ctx: ToolContext) -> dict:
    """등록된 트리거 카탈로그 (P5, 읽기 전용)."""
    return S.list_triggers(ctx)


def _trigger_engine(ctx: ToolContext):
    """트리거 실발화 엔진(P7) — 상태/이력/락은 memory base 하위로 격리."""
    from core import triggers as TR  # 지연 임포트: tools ↔ triggers 순환 방지
    base = resolve_mcp_runtime_dir()
    data_dir = Path(base) / "triggerdata"
    return TR.TriggerEngine(ctx, data_dir=data_dir)


async def tool_fire_due(ctx: ToolContext) -> dict:
    """due 트리거 실발화 (P7) — cron 계열(interval/daily) tick 1회.

    멱등/single-flight/watermark/실패격리 모두 엔진이 처리. publish 는 always_gate 유지.
    """
    eng = _trigger_engine(ctx)
    results = await eng.tick()
    fired = [{
        "trigger_id": (r.get("data") or {}).get("trigger_id") or (r.get("data") or {}).get("run_id"),
        "status": (r.get("data") or {}).get("status"),
        "dry_run": r.get("dry_run"),
    } for r in results]
    return _envelope(
        {"fired": fired, "count": len(fired)}, None, [],
        reasoning_steps=[f"due 트리거 {len(fired)}건 발화(tick)"],
    )


async def tool_handle_webhook(ctx: ToolContext, body, signature: str | None) -> dict:
    """웹훅 수신 처리 (P7) — HMAC 검증 후 매핑 트리거 발화(검증 실패는 발화 안 함)."""
    eng = _trigger_engine(ctx)
    return await eng.handle_webhook(body, signature)


async def tool_get_policy(ctx: ToolContext) -> dict:
    """현재 정책 + 오늘 자동발행 수 조회 (P5, 읽기 전용)."""
    snap = ctx.policy.snapshot()
    return _envelope(
        {"policy": snap["policy"], "publishes_today": snap["publishes_today"],
         "rules_available": snap["rules_available"]},
        None, [],
        reasoning_steps=["정책 스냅샷 조회(읽기 전용)"],
    )


async def _resolve_summary(ctx: ToolContext, summary) -> dict | None:
    """publish 입력 해소 — dict 면 그대로, 문자열이면 summary_id 로 노션에서 로드 시도."""
    if isinstance(summary, dict):
        return summary
    sid = str(summary or "").strip()
    if not sid:
        return None
    notion = ctx.adapters.get("notion")
    if notion is not None:
        try:
            for rec in await notion.search(sid, {"type": "summary"}):
                d = rec.get("data", {})
                if d.get("summary_id") == sid or rec.get("id") == sid:
                    return d
        except Exception as exc:
            logger.warning("요약 로드 실패(notion.search): sid=%s %s: %s", sid, type(exc).__name__, exc)
    return None


async def tool_publish(ctx: ToolContext, summary, data: dict | None = None) -> dict:
    """SUMMARY → Studio POST 발행(실/드라이런) + published_as 인스턴스 링크 기록 (P3).

    summary: SUMMARY dict 또는 summary_id 문자열(노션에서 로드 시도).
    data: 하위호환 — 주어지면 이를 SUMMARY 로 본다.
    """
    studio = ctx.adapters.get("studio")
    if studio is None:
        return _envelope(None, None, [], errors=["studio 어댑터 없음"])
    summary_dict = await _resolve_summary(ctx, data if data is not None else summary)
    if summary_dict is None:
        return _envelope(None, False, [], errors=[f"요약 로드 실패: {summary!r}"])
    # always_gate — 실발행(file/pr)은 명시적 승인 통과 시에만. 프로토콜 게이트가 ctx 에 표식.
    approved = bool(getattr(ctx, "_publish_approved", False))
    try:
        result = await studio.publish(summary_dict, approved=approved)
    except Exception as exc:
        return _envelope(None, None, ["studio"], errors=[f"studio.publish 실패: {type(exc).__name__}: {exc}"])

    post = result["post"]
    sid = summary_dict.get("summary_id")
    pid = post.get("post_id")
    link = None
    if sid and pid:
        link = ctx.links_store.record(
            f"notion:summary:{sid}", f"studio:post:{pid}", "published_as", dry_run=result["dry_run"],
        )
    if result.get("already_published"):
        head = f"이미 발행됨(멱등 no-op): {result.get('slug')}.mdx"
    elif result["published"]:
        head = f"Studio MDX 실발행 완료: {result.get('target_path')}"
    else:
        head = f"드라이런 — 파일 미작성({result.get('detail')})"
    steps = [f"SUMMARY({sid or '미상'}) → POST → MDX 렌더(frontmatter {len(result.get('frontmatter') or {})}필드)", head]
    if link:
        steps.append(f"published_as 기록: {link['source']} → {link['target']}")
    # provenance — 원본 SUMMARY 추적(cross-link)
    sources = ["studio"] + ([f"notion:summary:{sid}"] if sid else [])
    # MDX 발행 신호를 봉투 extra 로 노출(data=post/verification 은 불변 유지)
    extra = {k: result[k] for k in (
        "mode", "slug", "target_path", "mdx", "frontmatter_valid",
        "diff", "already_published", "contradiction",
    ) if k in result}
    if result.get("pr") is not None:
        extra["pr"] = result["pr"]
    return V.make_envelope(
        post, sources_used=sources, type_="post", validator=ctx.validator,
        schema_links=ctx._links(), entity=post, reasoning_steps=steps,
        published=result["published"], dry_run=result["dry_run"],
        detail=result["detail"], published_as=link,
        errors=result.get("errors"), **extra,
    )


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
    return V.make_envelope(
        {"resource_id": rid, "ingest_id": iid, "title": resource["title"], "stored": stored},
        sources_used=sources, schema_valid=schema_valid,
        type_="resource", validator=ctx.validator, schema_links=ctx._links(),
        entity=resource,
        reasoning_steps=[f"본문 추출 {len(body)}자", "Notion·Qdrant·memory 3중 적재"],
        errors=errors + verrs,
    )


async def tool_plan(ctx: ToolContext, goal: str, opts: dict | None = None) -> dict:
    """목표 → 적합 프로토콜 추천 + step 미리보기 (P4, dry plan — 실행 안 함).

    키워드 채점으로 등록 프로토콜을 정렬하고, 최상위 프로토콜의 step 체인을 미리 보여준다.
    실제 실행은 `run_action(protocol, params)` — plan 자체는 어떤 도구도 호출하지 않는다.
    """
    scored = P.recommend(goal)
    candidates = [
        {
            "protocol": name,
            "score": score,
            "desc": P.PROTOCOLS[name]["desc"],
            "steps": P.preview(name),
        }
        for name, score in scored
    ]
    # 최고점이 0 이면(키워드 미스) 추천은 비우되 후보 목록은 제공
    best_name, best_score = scored[0] if scored else (None, 0)
    recommended = best_name if best_score > 0 else None
    data = {
        "goal": goal,
        "recommended": recommended,
        "steps": P.preview(recommended) if recommended else [],
        "gates": [s["index"] for s in P.preview(recommended) if s["gate"]] if recommended else [],
        "candidates": candidates,
        "dry_run": True,
        "note": "미리보기만 — 실행은 run_action(protocol, params). 게이트가 있으면 approve 로 진행.",
    }
    return _envelope(
        data, None, [],
        reasoning_steps=[
            f"목표 '{goal}' 키워드 채점",
            f"추천: {recommended or '(명확한 매칭 없음 — 후보 참고)'}",
            "dry plan — 도구 미실행",
        ],
    )


async def tool_check(ctx: ToolContext, type_: str, data: dict | None = None) -> dict:
    """데이터 검증 점검 — Verifiability Engine 으로 6항목 품질 점수까지 반환(P3).

    data 없으면 알려진 타입 목록. data 있으면 스키마 검증 + 6항목 체크리스트/점수.
    """
    if data is None:
        return _envelope({"known_types": ctx.validator.known_types()}, True, [])
    valid, errors = ctx.validator.validate(type_, data)
    checklist = V.quality_checklist(type_, data, ctx.validator)
    return V.make_envelope(
        {"type": type_, "valid": valid, "quality_checklist": checklist},
        sources_used=[], schema_valid=valid,
        type_=type_, validator=ctx.validator, schema_links=ctx._links(),
        entity=data,
        reasoning_steps=[f"{type_} 스키마 검증", "6항목 품질 체크리스트 평가"],
        errors=errors,
    )


def _short_type(type_: str) -> str:
    """'notion:summary' → 'summary' (어댑터 내부는 짧은 타입 사용)."""
    return type_.split(":", 1)[1] if ":" in type_ else type_
