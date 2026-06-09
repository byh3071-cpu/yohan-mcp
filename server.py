# -*- coding: utf-8 -*-
"""yohan-mcp v2 — MCP 서버 진입점 (P2).

5개 백엔드 Adapter + Smart Router + 의도 기반 도구 10개를 MCP 로 노출.
실행:  python server.py   (stdio MCP)
"""
from __future__ import annotations

import json
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
    """프로토콜(순차 step 체인) 실행 (P4 — 자율성 L2, Protocol Engine 위임).

    멀티스텝 프로토콜(ingest_summarize_publish·resource_to_decision 등)을 순차 실행한다.
    되돌리기 어려운 step(외부 발행 등) 직전의 **승인 게이트**에 도달하면 실행을 멈추고
    `run_id` 가 담긴 pending 봉투를 반환한다 → `approve(run_id, 'approve'|'reject')` 로 진행/취소.
    publish_summary/summary_to_post·ingest 는 P3 단발 경로로 호환 유지. n8n·외부 큐 미사용.
    """
    return await T.tool_run_action(ctx, action, params)


@mcp.tool()
async def approve(run_id: str, decision: str, note: str | None = None) -> dict:
    """승인 게이트 결정 (P4 — 자율성 L2).

    run_action 이 게이트에서 멈춰 반환한 `run_id` 에 대해 사람이 결정한다.
    - decision='approve' → 게이트 다음 step 부터 재개(발행 등 완주).
    - decision='reject'  → 게이트 step 미실행 + 종료 봉투(사유 note).
    멱등: 이미 종결된 run 의 재호출은 무시되고 저장된 최종 봉투를 돌려준다.
    """
    return await T.tool_approve(ctx, run_id, decision, note)


@mcp.tool()
async def run_trigger(trigger_id: str, params: dict | None = None) -> dict:
    """트리거 실행 (P5 — 자율성 L3). triggers.json 정의를 읽어 트리거 정책으로 프로토콜 실행.

    게이트에서 정책이 자동승인하면 무인 완주, 한도 초과/위험 행위면 승인큐로 폴백한다.
    실제 cron/웹훅 구동(데이터 플레인)은 P5-B(배포)에서 이 진입점을 호출 — 호스트 미정.
    """
    return await T.tool_run_trigger(ctx, trigger_id, params)


@mcp.tool()
async def list_triggers() -> dict:
    """등록된 트리거 카탈로그 조회 (P5, 읽기 전용)."""
    return await T.tool_list_triggers(ctx)


@mcp.tool()
async def fire_due_triggers() -> dict:
    """due 트리거 실발화 (P7 — 무인 구동). interval/daily 트리거 중 발화 시점 도달분을 1회 tick.

    멱등(run_key)·single-flight 락·watermark 증분·실패 격리 적용. 외부 실발행(publish)은
    트리거 정책의 always_gate 를 유지한다(require_approval 이면 승인 큐 폴백까지만).
    """
    return await T.tool_fire_due(ctx)


@mcp.tool()
async def webhook(body: str, signature: str | None = None) -> dict:
    """웹훅 수신 처리 (P7). 요청 본문 + HMAC-SHA256 서명(.env WEBHOOK_SECRET)을 검증해 매핑 트리거 발화.

    서명 누락/불일치는 발화하지 않는다(401). 동일 event_id 재전송은 멱등 1회만.
    """
    return await T.tool_handle_webhook(ctx, body, signature)


@mcp.tool()
async def policy() -> dict:
    """현재 정책(자동승인/always_gate/일일한도) + 오늘 자동발행 수 조회 (P5, 읽기 전용)."""
    return await T.tool_get_policy(ctx)


@mcp.tool()
async def publish(summary: dict | str) -> dict:
    """SUMMARY → Studio 블로그 MDX 발행(src/content/blog/{slug}.mdx).

    STUDIO_REPO_PATH 미설정 or STUDIO_PUBLISH_MODE=dry_run 이면 드라이런(파일 미작성).
    file/pr 모드는 always_gate — 승인 통과 시에만 실제 쓰기. published_as 인스턴스 링크 기록.
    summary 는 SUMMARY dict 또는 summary_id 문자열(노션 로드).
    """
    return await T.tool_publish(ctx, summary)


@mcp.tool()
async def ingest(source: str, data: dict | None = None) -> dict:
    """수집 파이프라인 (P3 예정 stub, 입력 스키마 검증)."""
    return await T.tool_ingest(ctx, source, data)


@mcp.tool()
async def plan(goal: str, opts: dict | None = None) -> dict:
    """목표→적합 프로토콜 추천 + step 미리보기 (P4, dry plan). 실행은 run_action 으로."""
    return await T.tool_plan(ctx, goal, opts)


@mcp.tool()
async def check(type: str, data: dict | None = None) -> dict:
    """데이터를 P1 스키마로 검증 + 6항목 품질점수(P3 Verifiability). data 없으면 알려진 타입 목록."""
    return await T.tool_check(ctx, type, data)


# ── MCP Resources (에이전트가 읽는 타입/관계/상태) ─────────────────
_SCHEMAS_DIR = ctx.validator.dir


def _example_of(type_: str) -> dict:
    """P1 스키마 property examples[0] 를 모아 스키마 정합 예시 1건 구성(few-shot 재활용)."""
    schema = ctx.validator.schema_for(type_) or {}
    out: dict = {}
    for field in schema.get("required", list(schema.get("properties", {}).keys())):
        spec = schema.get("properties", {}).get(field, {})
        ex = spec.get("examples")
        if ex:
            out[field] = ex[0]
    return out


def _safe_segment(seg: str) -> str:
    """리소스 경로 세그먼트 봉쇄 — 경로 구분자/'..'/널 거부(임의 파일 읽기 차단)."""
    if not seg or seg in (".", "..") or any(c in seg for c in "/\\:\x00"):
        raise ValueError(f"잘못된 스키마 경로 세그먼트: {seg!r}")
    return seg


@mcp.resource("resource://schemas/{backend}/{entity}", mime_type="application/json")
def schema_resource(backend: str, entity: str) -> str:
    """단일 엔티티 스키마(description/examples 포함) 원문 — 에이전트가 읽고 데이터 생성."""
    path = _SCHEMAS_DIR / _safe_segment(backend) / f"{_safe_segment(entity)}.schema.json"
    # 이중 방어: resolve 후 schemas 디렉토리 하위인지 재확인
    if not path.resolve().is_relative_to(_SCHEMAS_DIR.resolve()):
        raise ValueError(f"schemas 디렉토리 탈출 차단: {backend}/{entity}")
    if not path.exists():
        raise ValueError(f"스키마 없음: {backend}/{entity}")
    return path.read_text(encoding="utf-8")


@mcp.resource("resource://schemas/_links", mime_type="application/json")
def links_resource() -> str:
    """크로스 백엔드 관계 맵 전체(_links.json) — 타입 수준 관계 그래프."""
    return (_SCHEMAS_DIR / "_links.json").read_text(encoding="utf-8")


@mcp.resource("resource://schemas/_index", mime_type="application/json")
def schema_index_resource() -> str:
    """알려진 스키마 타입 목록(backend:entity) — 어떤 스키마를 읽을 수 있는지 색인."""
    return json.dumps({"known_types": ctx.validator.known_types()}, ensure_ascii=False, indent=2)


@mcp.resource("resource://status/current", mime_type="application/json")
async def status_resource() -> str:
    """5개 백엔드 실시간 상태(status 도구 결과) — 라이브 헬스 스냅샷."""
    env = await T.tool_status(ctx)
    return json.dumps(env["data"], ensure_ascii=False, indent=2)


# ── MCP Prompts (few-shot 설계서) ──────────────────────────────────
@mcp.prompt(name="create-summary")
def create_summary_prompt(resource_title: str = "", resource_text: str = "") -> str:
    """원본 자료 → SUMMARY 생성 few-shot. P1 summary 스키마/examples 재활용."""
    example = _example_of("summary")
    return (
        "당신은 yohan-mcp 의 요약 에이전트다. 아래 원본 자료를 읽고 "
        "notion:summary 스키마에 정합하는 SUMMARY 한 건을 만들어 `create(type='summary', data=...)` 로 저장하라.\n"
        "규칙: key_insights 는 실행가능한 통찰 1줄씩, domain 은 공유 enum, confidence 0~1, "
        "resource_id 로 원본을 추적(summarized_by 1:N).\n\n"
        f"[원본 제목]\n{resource_title or '(제목 입력)'}\n\n"
        f"[원본 본문]\n{resource_text or '(본문 입력)'}\n\n"
        f"[기대 출력 예시 — 스키마 정합]\n{json.dumps(example, ensure_ascii=False, indent=2)}"
    )


@mcp.prompt(name="run-ingest")
def run_ingest_prompt(url: str = "") -> str:
    """URL 수집 few-shot — ingest 도구로 Notion+Qdrant+memory 3중 적재 유도."""
    example = _example_of("resource")
    return (
        "당신은 yohan-mcp 의 수집 에이전트다. 주어진 URL 을 `ingest(source=<url>)` 로 호출해 "
        "본문을 추출하고 Notion RESOURCE + Qdrant 벡터 + memory ingest 로그로 3중 적재하라.\n"
        "ingest 는 SSRF 가드/멱등(SoT Key)을 보장한다. 적재 후 resource_id 로 후속 요약을 잇는다.\n\n"
        f"[수집 대상 URL]\n{url or 'https://example.com/article'}\n\n"
        f"[적재될 RESOURCE 형태 예시]\n{json.dumps(example, ensure_ascii=False, indent=2)}"
    )


@mcp.prompt(name="cross-search")
def cross_search_prompt(query: str = "") -> str:
    """크로스 백엔드 통합검색 few-shot — search/get_context 로 RRF 융합 + 관계 traversal."""
    return (
        "당신은 yohan-mcp 의 검색 에이전트다. 질의를 `search(query=...)` 로 호출하면 "
        "Notion(키워드)+memory(파일)+Qdrant(의미)를 병렬 조회해 RRF(k=60)로 융합한다. "
        "관계까지 필요하면 `get_context(query=...)` 로 _links 관계를 함께 받아라.\n"
        "결과의 verification(검증 메타)·provenance(sources_used)를 근거로 답하라.\n\n"
        f"[질의]\n{query or '어텐션 메커니즘 청크 크기'}\n\n"
        "[기대 행동]\n1) search 호출 → 상위 결과 확인 2) 필요시 get_context 로 관계 확장 "
        "3) sources_used/verification 을 인용해 응답"
    )


if __name__ == "__main__":
    mcp.run()
