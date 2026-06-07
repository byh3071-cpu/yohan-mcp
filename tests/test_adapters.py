# -*- coding: utf-8 -*-
"""Adapter — health_check 형태 + memory 실CRUD + notion 모킹 테스트."""
import httpx
import pytest

from adapters.memory_adapter import MemoryAdapter
from adapters.n8n_adapter import N8nAdapter
from adapters.notion_adapter import NotionAdapter
from adapters.qdrant_adapter import QdrantAdapter
from adapters.studio_adapter import StudioAdapter

HEALTH_KEYS = {"ok", "latency_ms", "detail"}


# ── memory (실동작) ─────────────────────────────────────────────
async def test_memory_health(tmp_path):
    m = MemoryAdapter(base_dir=tmp_path)
    h = await m.health_check()
    assert set(h) == HEALTH_KEYS
    assert h["ok"] is True


async def test_memory_create_search_update(tmp_path):
    m = MemoryAdapter(base_dir=tmp_path)
    rec = await m.create("decision", {"decision_id": "d1", "title": "알파", "status": "제안"})
    assert rec["id"] == "d1" and rec["backend"] == "memory" and rec["type"] == "decision"
    await m.create("ingest", {"ingest_id": "g1", "source": "web", "raw": "베타 본문"})

    found = await m.search("알파")
    assert any(r["id"] == "d1" for r in found)
    found2 = await m.search("베타", {"type": "ingest"})
    assert found2 and found2[0]["id"] == "g1"

    upd = await m.update("d1", {"status": "승인"})
    assert upd["data"]["status"] == "승인"
    again = await m.search("알파")
    assert next(r for r in again if r["id"] == "d1")["data"]["status"] == "승인"


async def test_memory_update_no_profile_pollution(tmp_path):
    """profile.yaml 이 있어도 다른 엔티티 update 가 profile 을 오염시키면 안 됨."""
    m = MemoryAdapter(base_dir=tmp_path)
    await m.create("profile", {"name": "yohan", "role": "운영자"})
    await m.create("decision", {"decision_id": "d1", "title": "알파", "status": "제안"})

    rec = await m.update("d1", {"status": "승인"})  # type_ 미지정 스캔 경로
    assert rec["type"] == "decision"  # profile 로 오인되지 않음
    assert rec["data"]["status"] == "승인"

    prof = m._read_yaml(tmp_path / "profile.yaml")
    assert "status" not in prof and prof["name"] == "yohan"  # profile 미오염


async def test_memory_path_traversal_blocked(tmp_path):
    m = MemoryAdapter(base_dir=tmp_path)
    for bad in ["../evil", "../../evil", "a/b", "C:/Windows/Temp/evil"]:
        with pytest.raises(ValueError):
            await m.create("decision", {"decision_id": bad, "title": "x"})
    with pytest.raises(ValueError):
        await m.update("../evil", {"status": "승인"}, type_="decision")
    # base 밖에 어떤 파일도 생성되지 않았는지
    assert not (tmp_path.parent / "evil.yaml").exists()


async def test_notion_update_mocked():
    captured = {}

    def handler(request):
        captured["path"] = request.url.path
        return httpx.Response(200, json={"id": "page_123", "properties": {}})

    client = httpx.AsyncClient(base_url="https://api.notion.com/v1", transport=httpx.MockTransport(handler))
    a = NotionAdapter(client=client, token="t")
    rec = await a.update("page_123", {"title": "수정", "domain": "AI"}, type_="summary")
    assert rec["id"] == "page_123" and rec["type"] == "summary"
    assert captured["path"].endswith("/pages/page_123")
    # id URL 경로 주입 차단
    with pytest.raises(ValueError):
        await a.update("../databases/x/query", {"title": "x"}, type_="summary")
    await client.aclose()


# ── stub 어댑터 health + NotImplemented ─────────────────────────
@pytest.mark.parametrize("Adapter", [QdrantAdapter, StudioAdapter, N8nAdapter])
async def test_stub_health_no_url(Adapter):
    a = Adapter(url="")  # URL 미설정 강제
    h = await a.health_check()
    assert set(h) == HEALTH_KEYS
    assert h["ok"] is False
    with pytest.raises(NotImplementedError):
        await a.search("q")
    with pytest.raises(NotImplementedError):
        await a.create("x", {})


async def test_stub_health_mocked_ok():
    def handler(request):
        return httpx.Response(200)
    client = httpx.AsyncClient(base_url="http://fake", transport=httpx.MockTransport(handler))
    a = QdrantAdapter(client=client, url="http://fake")
    h = await a.health_check()
    assert h["ok"] is True
    await client.aclose()


# ── notion (모킹) ───────────────────────────────────────────────
async def test_notion_health_no_token():
    a = NotionAdapter(token="")
    h = await a.health_check()
    assert set(h) == HEALTH_KEYS
    assert h["ok"] is False
    assert "미설정" in h["detail"]
    # 토큰 없으면 search 는 graceful 빈 결과
    assert await a.search("q") == []


async def test_notion_create_mocked():
    captured = {}

    def handler(request):
        captured["path"] = request.url.path
        return httpx.Response(200, json={"id": "page_123", "properties": {}})

    client = httpx.AsyncClient(base_url="https://api.notion.com/v1", transport=httpx.MockTransport(handler))
    a = NotionAdapter(client=client, token="t")
    a.db_ids["summary"] = "db_sum"
    rec = await a.create("summary", {"summary_id": "s1", "title": "제목", "domain": "AI"})
    assert rec["id"] == "s1" and rec["backend"] == "notion"
    assert captured["path"].endswith("/pages")
    await client.aclose()


async def test_notion_search_mocked():
    def handler(request):
        return httpx.Response(200, json={"results": [{
            "id": "p1",
            "properties": {
                "summary_id": {"type": "rich_text", "rich_text": [{"plain_text": "s1"}]},
                "title": {"type": "title", "title": [{"plain_text": "알파"}]},
            },
        }]})

    client = httpx.AsyncClient(base_url="https://api.notion.com/v1", transport=httpx.MockTransport(handler))
    a = NotionAdapter(client=client, token="t")
    a.db_ids = {k: "db" for k in a.db_ids}
    res = await a.search("알파", {"type": "summary"})
    assert res and res[0]["id"] == "s1" and res[0]["data"]["title"] == "알파"
    await client.aclose()
