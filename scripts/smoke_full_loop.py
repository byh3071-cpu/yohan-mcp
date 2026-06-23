# -*- coding: utf-8 -*-
"""C+심장 E2E 스모크 (점검 전용, 프로덕션 코드 불변).

이 스크립트는 새 기능이 아니라 **이미 만든 두 본체**를 더미 자료로 한 바퀴 돌려 그린인지 확인한다:
  (1) 완결 루프 : RESOURCE → SUMMARY(초안) → [always_gate] → POST(발행)
  (2) 맥락 공급기 : MCP Resources(에이전트가 읽는 지식 구조) + MCP Prompts(작업 대본)

불변 원칙(절대 준수):
  - 외부 실발행 0 (전부 드라이런, always_gate 경유, 미승인이면 실발행 0)
  - 새 외부 의존성 0 (stdlib + 프로젝트 모듈만). ollama/qdrant 미가동에도 폴백 완주.
  - 프로덕션 코드 변경 0. 격리는 env + 임시 디렉터리로만.
  - 멱등: 같은 더미 두 번 넣어도 created/links 저널 라인 불변.

격리 스위치:
  - MemoryAdapter.base = MEMORY_DIR env  → approvals/runs/created/links/policy/trigger 저널이 전부 이 base 하위로 격리.
  - studio: STUDIO_PUBLISH_MODE=dry_run + STUDIO_REPO_PATH=임시레포 → 실파일 0.
  - EMBED_BACKEND=hash + QDRANT_URL='' (:memory:) → 인프라 의존 0.
  - NOTION_TOKEN='' → notion 실호출 0(드라이런/스킵).

실행:  python scripts/smoke_full_loop.py   (인자 없음, exit 0=전체 PASS / 1=하나라도 FAIL)
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

# ── UTF-8 콘솔(Windows cp949 한글 깨짐/크래시 방지) — PYTHONUTF8 미설정에도 안전 ──
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8")

# ── 리포 루트를 import 경로에 추가(어디서 실행하든 server/core import 가능) ──
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ── 격리 임시 루트 1개(끝나면 통째로 정리). 실 프로젝트 data/·memory/·스튜디오 레포 안 건드림 ──
TMP = Path(tempfile.mkdtemp(prefix="yohan_smoke_"))
MEM_DIR = TMP / "memory"
REPO_DIR = TMP / "repo"                 # dry_run 루프용 더미 스튜디오 레포(파일 안 써짐)
DUMMY_URL = "https://example.com/dummy"
QS_THRESHOLD = 5                        # SUMMARY 품질 임계(권장 자동화 프리셋의 dry_run_high_quality 기준)

# ★ 프로젝트 모듈 import **전에** 환경 격리를 끝낸다(어댑터가 생성자에서 env 를 읽으므로).
#   server.py 는 import 시 load_dotenv() 하지만 override=False 라 아래 값이 우선 유지된다.
os.environ["MEMORY_DIR"] = str(MEM_DIR)
os.environ["EMBED_BACKEND"] = "hash"
os.environ["QDRANT_URL"] = ""           # :memory: 폴백(서버 미가동 무관)
os.environ["NOTION_TOKEN"] = ""         # 토큰 없음 → notion 실호출 0
os.environ["STUDIO_PUBLISH_MODE"] = "dry_run"
os.environ["STUDIO_REPO_PATH"] = str(REPO_DIR)
os.environ["STUDIO_API_URL"] = ""       # health probe 네트워크 0
os.environ["N8N_URL"] = ""              # health probe 네트워크 0
os.environ["HEADROOM_URL"] = ""         # 압축 프록시 헬스 스킵

from adapters.studio_adapter import StudioAdapter  # noqa: E402
from core import tools as T  # noqa: E402


# ── 더미 RESOURCE 주입 — _fetch_url 갈아끼우기(외부 네트워크 0, 결정적) ──
async def _fake_fetch(url, client=None):
    return ("더미 자료: 어텐션 메커니즘 정리",
            "트랜스포머의 어텐션은 쿼리·키·값 내적으로 토큰 관련도를 계산해 가중 평균한다. (더미 본문)")


# ── PASS/FAIL 리포터 ────────────────────────────────────────────────
class Reporter:
    def __init__(self) -> None:
        self.rows: list[tuple[str, bool, str]] = []

    def check(self, label: str, ok: bool, detail: str = "") -> bool:
        ok = bool(ok)
        self.rows.append((label, ok, detail))
        mark = "PASS" if ok else "FAIL"
        line = f"  [{mark}] {label}"
        if detail:
            line += f" — {detail}"
        print(line)
        return ok

    @property
    def all_ok(self) -> bool:
        return all(ok for _, ok, _ in self.rows)

    @property
    def n_fail(self) -> int:
        return sum(1 for _, ok, _ in self.rows if not ok)


def _count_lines(p: Path) -> int:
    if not p.exists():
        return 0
    return sum(1 for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip())


def _count_mdx(d: Path) -> int:
    return len(list(d.glob("*.mdx"))) if d.exists() else 0


def _raises_value_error(fn, *args) -> bool:
    try:
        fn(*args)
        return False
    except ValueError:
        return True
    except Exception:
        return False


# ── 메인 ────────────────────────────────────────────────────────────
async def main() -> int:
    rep = Reporter()
    T._fetch_url = _fake_fetch  # 더미 본문 주입(SSRF/실네트워크 없이 결정적)

    ctx = T.ToolContext.from_env()  # 위 env 격리 반영(memory base=임시, studio dry_run)
    blog_dir = REPO_DIR / "src" / "content" / "blog"
    created_p = MEM_DIR / "created.jsonl"
    links_p = MEM_DIR / "links.jsonl"

    # ════════ 작업 2 — 완결 루프 실행 + 단계별 봉투 검증 ════════
    print("\n[작업 2] 완결 루프 (RESOURCE→SUMMARY→[gate]→POST, dry_run)")
    params = {"url": DUMMY_URL}
    pend = await T.tool_run_action(ctx, "ingest_summarize_publish", params)
    run_id = (pend.get("data") or {}).get("run_id")
    rep.check("게이트 도달(pending_approval) + run_id 확보",
              (pend.get("data") or {}).get("status") == "pending_approval" and bool(run_id),
              f"run_id={run_id}")

    # 게이트 정지 시점: 실발행 0 분기 확인(미승인이면 publish 안 됨)
    n_pending = len(ctx.approvals_store.list_pending())
    rep.check("always_gate — 승인큐 pending 1건", n_pending == 1, f"pending={n_pending}")
    rep.check("게이트 정지 — published_as 링크 0", ctx.links_store.find(relation="published_as") == [])
    rep.check("게이트 정지 — 실발행 .mdx 0", _count_mdx(blog_dir) == 0)

    # 승인 → 게이트 다음 step(publish) 재개
    done = await T.tool_approve(ctx, run_id, "approve")
    rep.check("승인 후 완주(completed)", (done.get("data") or {}).get("status") == "completed")
    rep.check("PUBLISH 드라이런(published=false, dry_run=true)",
              done.get("published") in (False, None) and done.get("dry_run") is True,
              f"published={done.get('published')} dry_run={done.get('dry_run')}")

    # 단계별 표준봉투 — RunStore 저널의 저장된 context 에서 각 step output 추출
    journal = ctx.runs_store.latest(run_id) or {}
    sctx = journal.get("context") or {}
    ing = (sctx.get("ingest") or {}).get("output") or {}
    draft = (sctx.get("draft") or {}).get("output") or {}
    pub = (sctx.get("publish") or {}).get("output") or {}

    # INGEST
    print("  · INGEST")
    rep.check("INGEST schema_valid=true", (ing.get("verification") or {}).get("schema_valid") is True)
    stored = (ing.get("data") or {}).get("stored") or {}
    used = (ing.get("provenance") or {}).get("sources_used") or []
    rep.check("INGEST 3중 적재 시도(qdrant+memory 적재, notion 토큰無→스킵)",
              "qdrant" in stored and "memory" in stored,
              f"stored={sorted(stored)} sources_used={used} errors={ing.get('errors')}")

    # SUMMARY
    print("  · SUMMARY")
    v = draft.get("verification") or {}
    qs = v.get("quality_score")
    rep.check("SUMMARY schema_valid=true", v.get("schema_valid") is True)
    rep.check("SUMMARY rulebook_pass=true", v.get("rulebook_pass") is True)
    rep.check("SUMMARY cross_links_intact=true", v.get("cross_links_intact") is True)
    rep.check("SUMMARY contradiction_detected=false", v.get("contradiction_detected") is False)
    rep.check(f"SUMMARY quality_score>={QS_THRESHOLD}",
              isinstance(qs, int) and qs >= QS_THRESHOLD, f"quality_score={qs}")

    # PUBLISH(봉투)
    print("  · PUBLISH(봉투)")
    rep.check("PUBLISH 봉투 dry_run=true + published=false",
              pub.get("dry_run") is True and pub.get("published") is False)
    rep.check("PUBLISH 후 실발행 .mdx 0(dry_run)", _count_mdx(blog_dir) == 0)
    rep.check("published_as 인스턴스 링크 1건(드라이런 기록)",
              len(ctx.links_store.find(relation="published_as")) == 1)

    # ════════ 작업 3 — 맥락 공급기(Resources/Prompts) 호출 검증 ════════
    print("\n[작업 3] 맥락 공급기 — MCP Resources/Prompts (server 핸들러 직접 호출)")
    import server as S  # env 격리 후 import → server.ctx 도 임시 memory base 사용

    # Resources READ
    try:
        doc = json.loads(S.schema_resource("notion", "summary"))
        rep.check("RES schema_resource('notion','summary')",
                  str(doc.get("title", "")).startswith("SUMMARY") and "examples" in doc["properties"]["summary_id"])
    except Exception as e:
        rep.check("RES schema_resource('notion','summary')", False, f"{type(e).__name__}: {e}")
    try:
        rels = {l["relation"] for l in json.loads(S.links_resource())["links"]}
        rep.check("RES links_resource()", {"published_as", "summarized_by"} <= rels)
    except Exception as e:
        rep.check("RES links_resource()", False, f"{type(e).__name__}: {e}")
    try:
        kt = json.loads(S.schema_index_resource())["known_types"]
        rep.check("RES schema_index_resource()", "notion:summary" in kt and "studio:post" in kt)
    except Exception as e:
        rep.check("RES schema_index_resource()", False, f"{type(e).__name__}: {e}")
    try:
        st = json.loads(await S.status_resource())
        rep.check("RES status_resource() — 5백엔드",
                  len(st["summaries"]) == 5 and {"notion", "memory", "qdrant", "studio", "n8n"} <= set(st["details"]))
    except Exception as e:
        rep.check("RES status_resource() — 5백엔드", False, f"{type(e).__name__}: {e}")

    # 경로탈출 가드 — 둘 다 ValueError 차단되어야 통과
    rep.check("가드 schema_resource('..','x') → ValueError", _raises_value_error(S.schema_resource, "..", "x"))
    rep.check("가드 schema_resource('notion','../secret') → ValueError",
              _raises_value_error(S.schema_resource, "notion", "../secret"))

    # Prompts GET
    try:
        p1 = S.create_summary_prompt(resource_title="더미", resource_text="더미 본문")
        rep.check("PROMPT create-summary", isinstance(p1, str) and "더미" in p1 and "key_insights" in p1)
    except Exception as e:
        rep.check("PROMPT create-summary", False, f"{type(e).__name__}: {e}")
    try:
        p2 = S.run_ingest_prompt(url=DUMMY_URL)
        rep.check("PROMPT run-ingest", isinstance(p2, str) and "ingest" in p2 and DUMMY_URL in p2)
    except Exception as e:
        rep.check("PROMPT run-ingest", False, f"{type(e).__name__}: {e}")
    try:
        p3 = S.cross_search_prompt(query="더미질의")
        rep.check("PROMPT cross-search", isinstance(p3, str) and "더미질의" in p3)
    except Exception as e:
        rep.check("PROMPT cross-search", False, f"{type(e).__name__}: {e}")
    try:
        ex = S._example_of("summary")
        rep.check("_example_of('summary') 비어있지 않은 스키마 예시",
                  isinstance(ex, dict) and len(ex) > 0, f"필드={sorted(ex)}")
    except Exception as e:
        rep.check("_example_of('summary')", False, f"{type(e).__name__}: {e}")

    # ════════ 작업 4 — 멱등 · 안전 재검증 ════════
    print("\n[작업 4] 멱등 · 드라이런 안전")
    c1, l1 = _count_lines(created_p), _count_lines(links_p)
    again = await T.tool_run_action(ctx, "ingest_summarize_publish", params)  # 동일 더미 재실행
    c2, l2 = _count_lines(created_p), _count_lines(links_p)
    rep.check("멱등 — 재실행 idempotent_replay", again.get("idempotent_replay") is True)
    rep.check("멱등 — created.jsonl 라인 불변(중복 0)", c1 == c2, f"{c1} → {c2}")
    rep.check("멱등 — links.jsonl 라인 불변(중복 0)", l1 == l2, f"{l1} → {l2}")
    rep.check("드라이런 안전 — 실발행 .mdx 0", _count_mdx(blog_dir) == 0)

    # ════════ 작업 4(선택) — 격리 file 모드: 승인 통과 → 임시 레포에 실 .mdx 1건 ════════
    print("\n[작업 4-선택] 격리 file 모드 실발행(승인 통과, 임시 레포 한정)")
    file_repo = TMP / "repo_file"
    # 실 STUDIO_REPO_PATH/실 data 저널 안 건드림 — 임시 레포 + 임시 저널로 완전 격리
    ctx.adapters["studio"] = StudioAdapter(
        repo_path=str(file_repo), mode="file", journal_path=TMP / "pub_file.jsonl",
    )
    fparams = {"url": "https://example.com/dummy-file"}
    pend2 = await T.tool_run_action(ctx, "ingest_summarize_publish", fparams)
    rep.check("FILE 모드 — always_gate pending(can_publish=true여도 승인 강제)",
              (pend2.get("data") or {}).get("status") == "pending_approval")
    rep.check("FILE 모드 — 승인 전 .mdx 0", _count_mdx(file_repo / "src" / "content" / "blog") == 0)
    done2 = await T.tool_approve(ctx, (pend2.get("data") or {})["run_id"], "approve")
    rep.check("FILE 모드 — 승인 후 published=true", done2.get("published") is True)
    rep.check("FILE 모드 — 임시 레포에 실 .mdx 1건 생성",
              _count_mdx(file_repo / "src" / "content" / "blog") == 1)

    # ── 정리 ──
    await ctx.aclose_all()
    try:
        await S.ctx.aclose_all()
    except Exception:
        pass

    # ── 작업 5 — 최종 리포트 ──
    qs_final = qs
    print("\n" + "=" * 64)
    print("리포트 — C+심장 완결 루프 + 맥락 공급기 E2E 스모크")
    print("=" * 64)
    print(f"  루프: INGEST/SUMMARY(quality_score={qs_final})/PUBLISH(dry_run)")
    print(f"  실 프로젝트 외부 발행: 0 (.mdx in dry-run repo = {_count_mdx(blog_dir)})")
    print(f"  체크 합계: {len(rep.rows)}건 / FAIL {rep.n_fail}건")
    print(f"  결과: {'전체 PASS ✅' if rep.all_ok else 'FAIL 있음 ❌'}")
    print("=" * 64)
    return 0 if rep.all_ok else 1


if __name__ == "__main__":
    code = 1
    try:
        code = asyncio.run(main())
    finally:
        shutil.rmtree(TMP, ignore_errors=True)  # 임시 루트 통째 정리(실 프로젝트 무영향)
    sys.exit(code)
