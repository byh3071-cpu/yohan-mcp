# -*- coding: utf-8 -*-
"""완결 루프 (P6) — RESOURCE → SUMMARY(create) → POST(publish) 한 바퀴.

(a) 드라이런: 외부 없이 전 구간 ok + dry_run 표식.
(b) 실데이터: create 는 notion 미설정→드라이런, studio 는 always_gate 승인 후 실제 MDX 파일 쓰기.
"""
from adapters.memory_adapter import MemoryAdapter
from adapters.n8n_adapter import N8nAdapter
from adapters.notion_adapter import NotionAdapter
from adapters.qdrant_adapter import QdrantAdapter
from adapters.studio_adapter import StudioAdapter
from core.embeddings import HashEmbedder
from core.router import SmartRouter
from core.schema_validator import SchemaValidator
from core import tools as T
from core.tools import ToolContext


def _ctx(tmp_path, studio=None):
    adapters = {
        "notion": NotionAdapter(token=""),
        "memory": MemoryAdapter(base_dir=tmp_path),
        "qdrant": QdrantAdapter(url=None, embedder=HashEmbedder()),
        "studio": studio if studio is not None else StudioAdapter(url="", api_key=""),
        "n8n": N8nAdapter(url=""),
    }
    return ToolContext(adapters, SmartRouter(adapters), SchemaValidator())


async def _fake_fetch(url, client=None):
    return ("어텐션 메커니즘 정리", "본문 트랜스포머 어텐션 메커니즘")


# ── (a) 완결 루프 드라이런 — 외부 없이 전 구간 ok + dry_run ─────
async def test_full_loop_dry_run(tmp_path, monkeypatch):
    monkeypatch.setattr(T, "_fetch_url", _fake_fetch)
    ctx = _ctx(tmp_path)  # notion 토큰 X, studio dry_run

    pend = await T.tool_run_action(ctx, "ingest_summarize_publish",
                                   {"url": "https://example.com/x"})
    assert pend["data"]["status"] == "pending_approval"     # 발행 직전 게이트
    run_id = pend["data"]["run_id"]

    done = await T.tool_approve(ctx, run_id, "approve")
    assert done["data"]["status"] == "completed"
    assert done["dry_run"] is True                          # studio 드라이런(파일 미작성)
    # 각 step 표준봉투 누적 + 마지막 POST 가 원본 SUMMARY 를 FK 로 연결(cross-link)
    labels = [s["step"] for s in done["data"]["steps"]]
    assert labels == ["ingest", "draft", "publish"]
    assert done["data"]["result"]["summary_id"]             # POST.summary_id (published_as FK)
    # 실제로 어떤 파일도 발행되지 않음 — POST 가 '초안' 상태로 머무름
    assert done["data"]["result"]["status"] == "초안"


# ── (b) 완결 루프 실데이터 — 승인 후 studio 실제 MDX 파일 쓰기 ──
async def test_full_loop_real_publish_file(tmp_path, monkeypatch):
    monkeypatch.setattr(T, "_fetch_url", _fake_fetch)
    studio = StudioAdapter(repo_path=str(tmp_path / "repo"), mode="file",
                           journal_path=tmp_path / "pub.jsonl")
    ctx = _ctx(tmp_path, studio=studio)

    pend = await T.tool_run_action(ctx, "ingest_summarize_publish",
                                   {"url": "https://example.com/real"})
    # always_gate — can_publish=True(repo+file) → 무조건 사람 승인 폴백
    assert pend["data"]["status"] == "pending_approval"
    assert ctx.links_store.find(relation="published_as") == []   # 게이트 정지 — 아직 발행 안 됨

    done = await T.tool_approve(ctx, pend["data"]["run_id"], "approve")
    assert done["data"]["status"] == "completed"
    assert done.get("published") is True                   # 실발행됨
    # 실제 MDX 파일이 src/content/blog 에 써졌는지
    blog = tmp_path / "repo" / "src" / "content" / "blog"
    files = list(blog.glob("*.mdx"))
    assert files, "발행 MDX 파일이 생성돼야 함"
    body = files[0].read_text(encoding="utf-8")
    assert body.startswith("---") and "published: true" in body
    # published_as 인스턴스 링크 기록(cross-link)
    assert ctx.links_store.find(relation="published_as")


# ── 멱등: 실발행 후 같은 run 재실행 → 중복 발행 없음 ────────────
async def test_full_loop_real_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(T, "_fetch_url", _fake_fetch)
    studio = StudioAdapter(repo_path=str(tmp_path / "repo"), mode="file",
                           journal_path=tmp_path / "pub.jsonl")
    ctx = _ctx(tmp_path, studio=studio)
    pend = await T.tool_run_action(ctx, "ingest_summarize_publish",
                                   {"url": "https://example.com/idem"})
    await T.tool_approve(ctx, pend["data"]["run_id"], "approve")
    files_after_1 = list((tmp_path / "repo" / "src" / "content" / "blog").glob("*.mdx"))

    # 같은 run 재호출 → RunStore 멱등 replay(완료 step 재실행 없음)
    again = await T.tool_run_action(ctx, "ingest_summarize_publish",
                                    {"url": "https://example.com/idem"})
    assert again.get("idempotent_replay") is True
    files_after_2 = list((tmp_path / "repo" / "src" / "content" / "blog").glob("*.mdx"))
    assert len(files_after_1) == len(files_after_2) == 1   # 중복 발행 없음
