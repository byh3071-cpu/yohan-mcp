# -*- coding: utf-8 -*-
"""승인큐 (P4) — ApprovalQueue 단위 + approve→재개 / reject 종료 / 멱등 통합."""
import httpx

from adapters.memory_adapter import MemoryAdapter
from adapters.n8n_adapter import N8nAdapter
from adapters.notion_adapter import NotionAdapter
from adapters.qdrant_adapter import QdrantAdapter
from adapters.studio_adapter import StudioAdapter
from core.embeddings import HashEmbedder
from core.approval import ApprovalQueue
from core.router import SmartRouter
from core.schema_validator import SchemaValidator
from core import tools as T
from core.tools import ToolContext


def _ctx(tmp_path):
    adapters = {
        "notion": NotionAdapter(token=""),
        "memory": MemoryAdapter(base_dir=tmp_path),
        "qdrant": QdrantAdapter(url=None, embedder=HashEmbedder()),
        "studio": StudioAdapter(url="", api_key=""),  # 드라이런
        "n8n": N8nAdapter(url=""),
    }
    return ToolContext(adapters, SmartRouter(adapters), SchemaValidator())


async def _fake_fetch(url, client=None):
    return ("어텐션 메커니즘 정리", "본문 텍스트 트랜스포머 어텐션")


# ── ApprovalQueue 단위 ──────────────────────────────────────────
def test_queue_enqueue_and_list(tmp_path):
    q = ApprovalQueue(tmp_path / "approvals.jsonl")
    q.enqueue("run_1", "proto", 2, {"k": "v"})
    pending = q.list_pending()
    assert len(pending) == 1
    assert pending[0]["run_id"] == "run_1" and pending[0]["step_index"] == 2
    assert q.status_of("run_1", 2) == "pending"
    assert q.payload_for("run_1") == {"k": "v"}


def test_queue_enqueue_idempotent(tmp_path):
    """같은 (run_id, step_index) 중복 적재 무시 — 큐가 부풀지 않는다."""
    q = ApprovalQueue(tmp_path / "approvals.jsonl")
    q.enqueue("run_1", "proto", 2, {"k": "v"})
    q.enqueue("run_1", "proto", 2, {"k": "다른값"})
    assert len(q.list_pending()) == 1
    assert q.payload_for("run_1") == {"k": "v"}  # 최초 payload 보존


def test_queue_resolve_approve_then_idempotent(tmp_path):
    q = ApprovalQueue(tmp_path / "approvals.jsonl")
    q.enqueue("run_1", "proto", 2, {})
    r1 = q.resolve("run_1", "approve")
    assert r1["status"] == "approve" and r1["idempotent"] is False
    assert q.list_pending() == []                 # 더 이상 대기 아님
    r2 = q.resolve("run_1", "approve")            # 중복 결정
    assert r2["status"] == "approve" and r2["idempotent"] is True


def test_queue_resolve_reject_and_unknown(tmp_path):
    q = ApprovalQueue(tmp_path / "approvals.jsonl")
    q.enqueue("run_1", "proto", 1, {})
    r = q.resolve("run_1", "reject", note="사유")
    assert r["status"] == "reject" and r["note"] == "사유"
    # 존재하지 않는 run
    assert q.resolve("nope", "approve")["status"] == "unknown"
    # 잘못된 decision
    q.enqueue("run_2", "proto", 0, {})
    assert q.resolve("run_2", "maybe")["status"] == "invalid"


# ── approve → 재개(publish 완주) ────────────────────────────────
async def test_approve_resumes_to_publish(tmp_path, monkeypatch):
    monkeypatch.setattr(T, "_fetch_url", _fake_fetch)
    ctx = _ctx(tmp_path)
    pend = await T.tool_run_action(ctx, "ingest_summarize_publish", {"url": "https://example.com/a"})
    rid = pend["data"]["run_id"]
    assert pend["data"]["status"] == "pending_approval"
    assert len(ctx.approvals_store.list_pending()) == 1

    done = await T.tool_approve(ctx, rid, "approve")
    assert done["data"]["status"] == "completed"
    assert done["dry_run"] is True and done["published"] is False  # 드라이런이라도 완주
    assert done["data"]["result"]["post_id"].startswith("post_")   # 발행 산출물(POST)
    assert ctx.approvals_store.list_pending() == []                # 게이트 해소


async def test_reject_terminates_without_publish(tmp_path, monkeypatch):
    monkeypatch.setattr(T, "_fetch_url", _fake_fetch)
    ctx = _ctx(tmp_path)
    pend = await T.tool_run_action(ctx, "ingest_summarize_publish", {"url": "https://example.com/b"})
    rid = pend["data"]["run_id"]

    rej = await T.tool_approve(ctx, rid, "reject", note="품질 미달")
    assert rej["data"]["status"] == "rejected"
    assert rej["data"]["note"] == "품질 미달"
    assert rej.get("published") is None          # 발행 산출물 없음
    assert "result" not in rej["data"]           # publish step 미실행
    assert ctx.links_store.find(relation="published_as") == []  # 발행 링크 없음


# ── 멱등: 종결된 run 재호출 무시 ────────────────────────────────
async def test_approve_idempotent_after_done(tmp_path, monkeypatch):
    monkeypatch.setattr(T, "_fetch_url", _fake_fetch)
    ctx = _ctx(tmp_path)
    pend = await T.tool_run_action(ctx, "ingest_summarize_publish", {"url": "https://example.com/c"})
    rid = pend["data"]["run_id"]
    first = await T.tool_approve(ctx, rid, "approve")
    second = await T.tool_approve(ctx, rid, "approve")   # 이미 완료
    assert second.get("idempotent_replay") is True
    assert second["data"]["status"] == "completed"
    # 두 번째 승인이 재발행하지 않음(published_as 링크 1건 유지)
    assert len(ctx.links_store.find(relation="published_as")) == 1


async def test_reject_then_approve_is_ignored(tmp_path, monkeypatch):
    """거부로 종결된 뒤 approve 해도 발행되지 않는다(멱등 종결)."""
    monkeypatch.setattr(T, "_fetch_url", _fake_fetch)
    ctx = _ctx(tmp_path)
    pend = await T.tool_run_action(ctx, "ingest_summarize_publish", {"url": "https://example.com/d"})
    rid = pend["data"]["run_id"]
    await T.tool_approve(ctx, rid, "reject", note="중단")
    after = await T.tool_approve(ctx, rid, "approve")    # 뒤늦은 승인
    assert after.get("idempotent_replay") is True
    assert after["data"]["status"] == "rejected"
    assert ctx.links_store.find(relation="published_as") == []


async def test_approve_unknown_run(tmp_path):
    ctx = _ctx(tmp_path)
    env = await T.tool_approve(ctx, "run_does_not_exist", "approve")
    assert env["data"]["status"] == "unknown_run"
    assert env["errors"]
