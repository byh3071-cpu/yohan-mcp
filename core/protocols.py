# -*- coding: utf-8 -*-
"""yohan-mcp v2 — Protocol Engine (P4, 자율성 L1→L2).

P3 까지 도구는 **단발 실행(L1)** 이었다. P4 는 여러 타입 도구를 **프로토콜 =
순차 step 체인**으로 묶어 자동 실행하되, 외부 발행처럼 되돌리기 어려운 step
직전에 **사람 승인 게이트(L2)** 를 둔다.

오케스트레이션은 yohan-mcp 내부의 이 엔진이 담당하고, 구동은 Claude Code / VHK CLI
가 한다. **n8n·외부 스케줄러·외부 큐 의존성 없음** — 무인 always-on 은 P5+ 별도 트랙.

프로토콜 / step 구조 ::

    PROTOCOL = {"name": str, "steps": [STEP, ...]}
    STEP = {
        "tool":  "ingest"|"create"|"publish"|"search"|"get_context"|...,  # 위임할 도구
        "params": {...},          # 정적 파라미터(선택)
        "map":   {"<param>": "<dotted.path>" | ["<path1>", "<path2>"]},   # 이전 step 출력 매핑
        "build": "summary_from_ingest" | "decision_from_context",        # 파라미터 합성기(선택)
        "gate":  True,            # 실행 전 사람 승인 대기(L2 게이트)
        "as":    "draft",         # 컨텍스트 저장 라벨(기본=tool 명)
        "optional": True,         # data=None(연성 실패)이어도 체인 계속(초안은 map 으로 전달)
        "desc":  "...",           # plan 미리보기용 한 줄 설명
    }

실행 결과는 P3 의 표준 검증 봉투로 누적되며(provenance.reasoning_steps 에 step 로그),
중간 상태/멱등/재개를 위해 RunStore(`memory/runs.jsonl`)에 스냅샷을 남긴다.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core import verify as V
from core.approval import ApprovalQueue

ROOT = Path(__file__).resolve().parent.parent
_KST = timezone(timedelta(hours=9))


def _now_iso() -> str:
    return datetime.now(_KST).isoformat(timespec="seconds")


# ── 실행 저널 (멱등/재개) ────────────────────────────────────────────
class RunStore:
    """프로토콜 실행 상태 append-only JSONL 저널.

    한 줄 = 한 스냅샷. run_id 별 마지막 스냅샷이 현재 상태(running/pending/done/aborted/rejected).
    멱등(완료 step 건너뜀)과 게이트 재개(컨텍스트 복원)를 모두 이 저널로 지원한다.
    """

    def __init__(self, path: str | os.PathLike | None = None) -> None:
        if path is not None:
            self.path = Path(path)
        else:
            base = Path(os.getenv("MEMORY_DIR", ROOT / "memory"))
            self.path = base / "runs.jsonl"

    def save(self, state: dict) -> dict:
        state = {**state, "ts": _now_iso()}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(state, ensure_ascii=False) + "\n")
        return state

    def all(self) -> list[dict]:
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

    def latest(self, run_id: str) -> dict | None:
        found = None
        for st in self.all():
            if st.get("run_id") == run_id:
                found = st
        return found


# ── 파라미터 합성기(build) — 결정적, LLM 호출 없음 ──────────────────
def _build_summary_from_ingest(context: dict, params: dict) -> dict:
    """ingest 출력 → SUMMARY 초안(드래프트). 발행 전 사람 검토를 전제로 한 자동 초안."""
    ing = _dig(context, "ingest.output.data") or {}
    rid = ing.get("resource_id") or "res_unknown"
    title = ing.get("title") or "자동 수집 자료"
    sid = "sum_" + (rid.split("_", 1)[1] if "_" in rid else rid)
    summary = {
        "summary_id": sid,
        "resource_id": rid,
        "title": title,
        "summary": f"'{title}' 자동 요약 초안. 원본 {rid} 에서 수집·생성됨. 발행 전 사람 검토 필요.",
        "key_insights": [f"{title} 핵심 정리(초안)"],
        "domain": params.get("domain", "기타"),
        "status": "신규",
        "created_at": _now_iso(),
    }
    return {"type": "summary", "data": summary}


def _build_decision_from_context(context: dict, params: dict) -> dict:
    """search + get_context 결과 → DECISION 초안. status='제안'(미확정)."""
    query = params.get("query") or params.get("goal") or "미상"
    ctx_data = _dig(context, "context.output.data") or {}
    matches = ctx_data.get("matches") or []
    related = ctx_data.get("related_links") or []
    digest = hashlib.sha256(str(query).encode("utf-8")).hexdigest()[:12]
    decision = {
        "decision_id": f"dec_{digest}",
        "title": f"{query} 관련 결정(초안)",
        "context": f"search {len(matches)}건 + get_context 관계 {len(related)}건 기반 자동 초안",
        "decision": params.get("decision") or "(사람이 결정 내용을 확정)",
        "rationale": params.get("rationale") or f"수집·검색 결과 {len(matches)}건을 근거로 한 초안",
        "status": "제안",
        "decided_at": _now_iso(),
    }
    return {"type": "decision", "data": decision}


_BUILDERS = {
    "summary_from_ingest": _build_summary_from_ingest,
    "decision_from_context": _build_decision_from_context,
}


# ── 내장 프로토콜 ───────────────────────────────────────────────────
PROTOCOLS: dict[str, dict] = {
    # 수집 → 요약(초안) → [GATE] → 발행. 발행 직전에 사람이 초안을 승인.
    "ingest_summarize_publish": {
        "name": "ingest_summarize_publish",
        "desc": "URL 수집 → SUMMARY 초안 생성 → [승인] → Studio 발행",
        "keywords": ["발행", "publish", "수집", "ingest", "요약", "블로그", "글", "url", "포스트", "글쓰기"],
        "steps": [
            {"tool": "ingest", "map": {"source": ["params.url", "params.source"]},
             "as": "ingest", "desc": "URL 본문 추출 → Notion·Qdrant·memory 3중 적재"},
            {"tool": "create", "params": {"type": "summary"}, "build": "summary_from_ingest",
             "as": "draft", "optional": True,
             "desc": "수집 자료에서 SUMMARY 초안 합성(가능하면 노션 적재)"},
            {"tool": "publish", "gate": True, "map": {"summary": "draft.input.data"},
             "as": "publish", "desc": "[GATE] 승인 후 SUMMARY 초안을 Studio POST 로 발행"},
        ],
    },
    # 검색 → 컨텍스트 → 결정(초안). 게이트 없음(memory 기록은 가역적).
    "resource_to_decision": {
        "name": "resource_to_decision",
        "desc": "검색 → 관계 컨텍스트 수집 → memory DECISION 초안 기록",
        "keywords": ["결정", "decision", "의사결정", "판단", "검토", "선택", "정하"],
        "steps": [
            {"tool": "search", "map": {"query": ["params.query", "params.goal"]},
             "as": "search", "desc": "질의로 크로스 백엔드 검색(RRF 융합)"},
            {"tool": "get_context", "map": {"query": ["params.query", "params.goal"]},
             "as": "context", "desc": "_links 관계까지 포함한 컨텍스트 수집"},
            {"tool": "create", "params": {"type": "decision"}, "build": "decision_from_context",
             "as": "decision", "desc": "컨텍스트 기반 DECISION 초안(status='제안') 기록"},
        ],
    },
}


def list_protocols() -> list[str]:
    return sorted(PROTOCOLS.keys())


def preview(name: str) -> list[dict]:
    """프로토콜의 step 미리보기(실행 안 함) — plan 도구가 사용."""
    proto = PROTOCOLS.get(name)
    if not proto:
        return []
    out = []
    for i, step in enumerate(proto["steps"]):
        out.append({
            "index": i,
            "tool": step["tool"],
            "gate": bool(step.get("gate")),
            "desc": step.get("desc", step["tool"]),
        })
    return out


def recommend(goal: str) -> list[tuple[str, int]]:
    """goal 문자열 → (프로토콜명, 점수) 내림차순. 키워드 겹침으로 단순 채점."""
    g = (goal or "").lower()
    scored: list[tuple[str, int]] = []
    for name, proto in PROTOCOLS.items():
        score = sum(1 for kw in proto.get("keywords", []) if kw.lower() in g)
        scored.append((name, score))
    scored.sort(key=lambda t: (-t[1], t[0]))
    return scored


# ── 컨텍스트 경로 해소 ──────────────────────────────────────────────
def _dig(obj, path: str):
    """dotted path 로 dict 트리 탐색. '$.' 는 'params.' 별칭. 없으면 None."""
    if path.startswith("$."):
        path = "params." + path[2:]
    cur = obj
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
        if cur is None:
            return None
    return cur


def _resolve_one(context: dict, spec):
    """map 값(단일 경로 or 후보 경로 리스트) → 첫 non-None."""
    paths = spec if isinstance(spec, list) else [spec]
    for p in paths:
        val = _dig(context, p)
        if val is not None:
            return val
    return None


def _resolve_params(step: dict, context: dict, params: dict) -> dict:
    """정적 params < map < build 순으로 호출 파라미터 합성."""
    call: dict = dict(step.get("params") or {})
    for key, spec in (step.get("map") or {}).items():
        val = _resolve_one(context, spec)
        if val is not None:
            call[key] = val
    build = step.get("build")
    if build and build in _BUILDERS:
        call.update(_BUILDERS[build](context, params))
    return call


# ── 도구 디스패치 (지연 임포트로 순환참조 회피) ─────────────────────
async def _dispatch(ctx, tool: str, params: dict) -> dict:
    from core import tools as T  # 지연 임포트: tools ↔ protocols 순환 방지
    if tool == "ingest":
        return await T.tool_ingest(ctx, params.get("source") or params.get("url", ""))
    if tool == "create":
        return await T.tool_create(ctx, params.get("type", ""), params.get("data") or {})
    if tool == "update":
        return await T.tool_update(ctx, params.get("id", ""), params.get("data") or {}, params.get("type"))
    if tool == "search":
        return await T.tool_search(ctx, params.get("query", ""), params.get("opts"))
    if tool == "get_context":
        return await T.tool_get_context(ctx, params.get("query", ""), params.get("opts"))
    if tool == "publish":
        return await T.tool_publish(ctx, params.get("summary"))
    if tool == "check":
        return await T.tool_check(ctx, params.get("type", ""), params.get("data"))
    if tool == "status":
        return await T.tool_status(ctx)
    raise ValueError(f"프로토콜이 모르는 step tool: {tool!r}")


def _step_failed(env: dict) -> bool:
    """엔진의 하드 실패 기준 — 결과 데이터가 없으면(None) 실패로 본다."""
    return not isinstance(env, dict) or env.get("data") is None


# ── 봉투 누적 헬퍼 ──────────────────────────────────────────────────
def _collect(context: dict) -> tuple[list[str], list[dict]]:
    """실행된 step 들의 sources_used 합집합 + step 요약 목록."""
    sources: list[str] = []
    summaries: list[dict] = []
    for label, slot in context.items():
        if not isinstance(slot, dict) or "output" not in slot:
            continue
        env = slot["output"]
        used = (env.get("provenance") or {}).get("sources_used") or []
        for s in used:
            if s not in sources:
                sources.append(s)
        summaries.append({
            "step": label,
            "ok": env.get("data") is not None,
            "errors": env.get("errors"),
        })
    return sources, summaries


def _make_run_id(name: str, params: dict) -> str:
    """프로토콜+파라미터 결정적 해시 → run_id(멱등 기본키). 호출자가 명시하면 우선."""
    rid = (params or {}).get("run_id")
    if rid:
        return str(rid)
    blob = name + "::" + json.dumps(params or {}, sort_keys=True, ensure_ascii=False, default=str)
    return "run_" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


# ── 실행기 ──────────────────────────────────────────────────────────
async def run_protocol(
    ctx,
    name: str,
    params: dict | None = None,
    *,
    run_id: str | None = None,
    resume: bool = False,
    approved_index: int | None = None,
    policy=None,
) -> dict:
    """프로토콜 순차 실행. gate 도달 시 **정책 평가**로 자동승인/사람폴백을 결정한다(P5).

    - policy(또는 ctx.policy)가 자동승인하면 게이트를 통과해 그대로 진행(봉투에 policy_rule).
    - 한도 초과/always_gate/매칭 없음이면 P4 승인큐로 폴백(pending 봉투, run_id).
    멱등: 같은 run_id 가 이미 done/pending/aborted/rejected 면 저장된 봉투를 그대로 돌려준다
    (완료 step 재실행 없음). resume=True 면 게이트 승인 후 저장된 컨텍스트로 재개한다.
    """
    proto = PROTOCOLS.get(name)
    params = params or {}
    if proto is None:
        return V.make_envelope(
            {"status": "unknown_protocol", "protocol": name, "known_protocols": list_protocols()},
            sources_used=[], schema_valid=None,
            reasoning_steps=[f"등록되지 않은 프로토콜: {name}"],
            errors=[f"알 수 없는 프로토콜: {name}"],
        )

    run_id = run_id or _make_run_id(name, params)
    journal = ctx.runs_store.latest(run_id)

    # 멱등 재실행: 종결/대기 상태면 저장된 봉투 그대로(완료 step 건너뜀)
    if journal and not resume:
        st = journal.get("status")
        if st in ("done", "rejected", "aborted", "pending"):
            env = dict(journal.get("envelope") or {})
            env["idempotent_replay"] = True
            return env
        if st == "running":  # 중단 복구 — 저장된 다음 step 부터 재개
            resume = True

    if resume and journal:
        context = journal.get("context") or {"params": params, "_reasoning": []}
        start = int(journal.get("next_index", 0))
    else:
        context = {"params": params, "_reasoning": []}
        start = 0

    reasoning: list[str] = list(context.get("_reasoning", []))
    steps = proto["steps"]

    for i in range(start, len(steps)):
        step = steps[i]
        tool = step["tool"]
        call_params = _resolve_params(step, context, params)

        # ── 게이트(P5): 정책 평가 → 자동승인 통과 or 사람 승인 폴백 ──
        if step.get("gate") and not (resume and i == approved_index):
            facts = _gate_facts(ctx, name, i, step, call_params)
            facts["run_id"] = run_id
            engine = policy if policy is not None else getattr(ctx, "policy", None)
            decision = engine.decide(facts) if engine is not None else {
                "auto_approve": False, "rule": None, "reason": "정책 엔진 없음"}

            if decision.get("auto_approve"):
                # 정책 자동승인 — 게이트 통과(아래 일반 실행 경로로 진행)
                context.setdefault("_policy_decisions", []).append({**decision, "step_index": i})
                reasoning.append(
                    f"GATE step{i} ({tool}) — 정책 자동승인(rule={decision.get('rule')}): {decision.get('reason')}")
            else:
                # 한도 초과/always_gate/매칭 없음 → P4 승인큐로 폴백(pending)
                payload = {
                    "protocol": name, "params": params, "step_index": i,
                    "step": {"tool": tool, "desc": step.get("desc")},
                    "context": {**context, "_reasoning": reasoning},
                    "preview": _build_summary_preview(step, context, params),
                    "policy_decision": decision,
                }
                ctx.approvals_store.enqueue(run_id, name, i, payload)
                sources, _ = _collect(context)
                env = V.make_envelope(
                    {
                        "status": "pending_approval",
                        "run_id": run_id,
                        "protocol": name,
                        "step_index": i,
                        "awaiting_tool": tool,
                        "awaiting_desc": step.get("desc"),
                        "policy_decision": decision,
                        "note": f"approve('{run_id}', 'approve'|'reject') 로 진행/취소",
                    },
                    sources_used=sources, schema_valid=None,
                    reasoning_steps=reasoning + [
                        f"GATE step{i} ({tool}) — 정책 폴백(rule={decision.get('rule')}): "
                        f"{decision.get('reason')} → 사람 승인 대기"],
                    run_id=run_id, pending=True,
                )
                ctx.runs_store.save({
                    "run_id": run_id, "protocol": name, "status": "pending",
                    "next_index": i, "context": {**context, "_reasoning": reasoning},
                    "envelope": env,
                })
                return env

        # ── step 실행 ──
        # 게이트 step 이 여기 도달했다면 게이트를 통과한 것(자동승인 or 사람승인).
        # always_gate 준수: publish 실발행은 이 승인 표식이 있을 때만 실제로 쓴다(tool_publish 가 읽음).
        prev_approved = getattr(ctx, "_publish_approved", False)
        ctx._publish_approved = bool(step.get("gate"))
        try:
            step_env = await _dispatch(ctx, tool, call_params)
        except Exception as exc:  # 디스패치/도구 자체 예외 격리 → 부분결과 봉투
            return _abort(ctx, run_id, name, i, tool, reasoning, context,
                          [f"step{i} {tool} 예외: {type(exc).__name__}: {exc}"])
        finally:
            ctx._publish_approved = prev_approved

        context[step.get("as", tool)] = {"input": call_params, "output": step_env}
        ok = step_env.get("data") is not None
        reasoning.append(f"step{i} {tool}: {'완료' if ok else '연성 실패(data=None)'}")
        sub = (step_env.get("provenance") or {}).get("reasoning_steps") or []
        reasoning += [f"  └ {s}" for s in sub]

        # ── 실패 판정(optional 이면 통과) ──
        if _step_failed(step_env) and not step.get("optional"):
            return _abort(ctx, run_id, name, i, tool, reasoning, context,
                          step_env.get("errors") or [f"step{i} {tool} 실패(data=None)"])

        ctx.runs_store.save({
            "run_id": run_id, "protocol": name, "status": "running",
            "next_index": i + 1, "context": {**context, "_reasoning": reasoning},
        })

    # ── 완주 ──
    final = _final_envelope(run_id, name, context, reasoning)
    ctx.runs_store.save({
        "run_id": run_id, "protocol": name, "status": "done",
        "next_index": len(steps), "context": {**context, "_reasoning": reasoning},
        "envelope": final,
    })
    return final


def _build_summary_preview(step: dict, context: dict, params: dict) -> dict:
    """게이트에서 사람이 볼 '곧 실행될 것' 미리보기(되돌리기 어려운 행위 가시화)."""
    try:
        call = _resolve_params(step, context, params)
    except Exception:
        call = {}
    summ = call.get("summary")
    preview = {"tool": step["tool"], "desc": step.get("desc")}
    if isinstance(summ, dict):
        preview["publish_target"] = {
            "summary_id": summ.get("summary_id"),
            "title": summ.get("title"),
        }
    return preview


def _gate_facts(ctx, name: str, i: int, step: dict, call_params: dict) -> dict:
    """게이트 step 의 정책 판단 근거 수집(부작용 없음 — publish 는 변환·검증만 미리 수행).

    publish: studio.can_publish 로 외부 실발행/드라이런 구분 + 발행될 POST 의 품질점수 산출.
    """
    facts = {
        "protocol": name, "step_index": i, "tool": step["tool"],
        "dry_run": True, "external_publish": False, "quality_score": None,
    }
    if step["tool"] == "publish":
        studio = ctx.adapters.get("studio")
        can_pub = bool(getattr(studio, "can_publish", False))
        facts["external_publish"] = can_pub
        facts["dry_run"] = not can_pub
        summary = call_params.get("summary")
        if isinstance(summary, dict) and studio is not None:
            try:  # SUMMARY→POST 변환(발행 안 함) 후 품질점수만 측정
                post = studio.summary_to_post(summary)
                ver = V.verify_entity("post", post, ctx.validator, ctx._links())
                facts["quality_score"] = ver.get("quality_score")
            except Exception:
                facts["quality_score"] = None
    return facts


def _abort(ctx, run_id, name, i, tool, reasoning, context, errors) -> dict:
    """한 step 실패 → 부분결과 봉투(중단 지점 명시) + 저널에 aborted 기록."""
    sources, summaries = _collect(context)
    env = V.make_envelope(
        {
            "status": "aborted",
            "run_id": run_id,
            "protocol": name,
            "failed_step_index": i,
            "failed_tool": tool,
            "completed_steps": summaries,
        },
        sources_used=sources, schema_valid=False,
        reasoning_steps=reasoning + [f"step{i} ({tool}) 실패 → 체인 중단(부분결과 반환)"],
        errors=list(errors or []),
        run_id=run_id, aborted=True,
    )
    ctx.runs_store.save({
        "run_id": run_id, "protocol": name, "status": "aborted",
        "next_index": i, "context": {**context, "_reasoning": reasoning},
        "envelope": env,
    })
    return env


def _final_envelope(run_id, name, context, reasoning) -> dict:
    """완주 봉투 — 마지막 step 출력 요지 + 전체 step 요약을 동봉."""
    sources, summaries = _collect(context)
    # 마지막으로 실행된 step 의 출력에서 발행 결과 같은 핵심 신호를 끌어올린다
    last_env = None
    for slot in context.values():
        if isinstance(slot, dict) and "output" in slot:
            last_env = slot["output"]
    result = (last_env or {}).get("data")
    data = {
        "status": "completed",
        "run_id": run_id,
        "protocol": name,
        "steps": summaries,
        "result": result,
    }
    extra = {}
    if isinstance(last_env, dict):
        for k in ("published", "dry_run", "published_as", "detail"):
            if k in last_env:
                extra[k] = last_env[k]
    # P5 — 정책 자동승인으로 통과한 게이트가 있으면 봉투에 표시(policy_rule)
    decisions = context.get("_policy_decisions") or []
    if decisions:
        data["auto_approved"] = True
        data["policy_rule"] = decisions[-1].get("rule")
        extra["auto_approved"] = True
        extra["policy"] = decisions
    return V.make_envelope(
        data, sources_used=sources, schema_valid=None,
        reasoning_steps=reasoning + [f"프로토콜 '{name}' 완주"],
        run_id=run_id, completed=True, **extra,
    )


def rejected_envelope(run_id, name, step_index, reasoning, note) -> dict:
    """거부 종료 봉투 — 게이트 step 미실행을 명시(예: 발행 안 됨)."""
    return V.make_envelope(
        {
            "status": "rejected",
            "run_id": run_id,
            "protocol": name,
            "rejected_step_index": step_index,
            "note": note,
        },
        sources_used=[], schema_valid=None,
        reasoning_steps=list(reasoning or []) + [f"step{step_index} 게이트 거부 — 종료(사유: {note or '미기재'})"],
        run_id=run_id, rejected=True,
    )
