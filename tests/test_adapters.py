# -*- coding: utf-8 -*-
"""Adapter — health_check 형태 + memory 실CRUD + notion 모킹 테스트."""
import json

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


async def test_memory_corrupt_yaml_isolated(tmp_path):
    """손상 YAML 1개가 memory 검색 전체를 죽이면 안 됨 — 해당 파일만 skip (격리)."""
    m = MemoryAdapter(base_dir=tmp_path)
    await m.create("decision", {"decision_id": "good", "title": "킵미", "status": "제안"})
    ddir = tmp_path / "decisions"
    (ddir / "broken.yaml").write_text("title: 'unterminated\nstatus: [", encoding="utf-8")  # ScannerError
    (ddir / "scalar.yaml").write_text("그냥 문자열", encoding="utf-8")  # dict 아닌 스칼라

    found = await m.search("킵미")  # ParserError/AttributeError 로 죽지 않아야 함
    assert [r["id"] for r in found] == ["good"]
    all_ = await m.search("")  # 전건 나열도 정상 파일만
    assert any(r["id"] == "good" for r in all_)
    assert all(r["id"] not in ("broken", "scalar") for r in all_)


async def test_memory_corrupt_yaml_update_no_clobber(tmp_path):
    """손상 YAML 을 {} 취급해 update 로 덮어쓰면 데이터 소실 → 명시적 에러 + 원본 보존."""
    m = MemoryAdapter(base_dir=tmp_path)
    ddir = tmp_path / "decisions"
    ddir.mkdir()
    original = "title: 'unterminated\nstatus: ["
    (ddir / "broken.yaml").write_text(original, encoding="utf-8")

    with pytest.raises(ValueError, match="손상 YAML"):
        await m.update("broken", {"status": "승인"}, type_="decision")
    assert (ddir / "broken.yaml").read_text(encoding="utf-8") == original  # 클로버 금지


async def test_memory_path_traversal_blocked(tmp_path):
    m = MemoryAdapter(base_dir=tmp_path)
    for bad in ["../evil", "../../evil", "a/b", "C:/Windows/Temp/evil"]:
        with pytest.raises(ValueError):
            await m.create("decision", {"decision_id": bad, "title": "x"})
    with pytest.raises(ValueError):
        await m.update("../evil", {"status": "승인"}, type_="decision")
    # base 밖에 어떤 파일도 생성되지 않았는지
    assert not (tmp_path.parent / "evil.yaml").exists()


# ── YOHA-4 회귀 — 매칭 blob 이 yaml.safe_dump 직렬화 부작용에 깨지면 안 됨 ──
async def test_memory_search_multiword_across_dump_fold_point(tmp_path):
    """원문에 실재하는 연속 다어절 질의가 safe_dump width=80 접기(공백→개행+들여쓰기)
    지점에 걸쳐도 매칭되어야 한다 — 과거엔 접점 질의가 무음 0건(YOHA-4)."""
    import re

    import yaml

    rationale = (
        "벡터 검색 도입 근거는 다음과 같다. 기존 substring 매칭은 다어절 질의에 약했고 "
        "실측 골든쿼리 리콜 측정에서 상위권 손실이 확인되어 RRF 결합으로 보완하기로 했다."
    )
    m = MemoryAdapter(base_dir=tmp_path)
    await m.create(
        "decision", {"decision_id": "d-fold", "title": "접기 재현", "rationale": rationale}
    )

    # 구 blob 레시피(safe_dump)가 실제로 접는 지점의 인접 어절 쌍을 질의로 채택
    # (pyyaml 버전이 바뀌어 접점이 이동해도 항상 '진짜 접점'을 겨냥한다)
    folded = yaml.safe_dump({"rationale": rationale}, allow_unicode=True)
    pair = re.search(r"(\S+)\n  (\S+)", folded)
    assert pair, "전제: 80자 초과 한 줄은 safe_dump 기본 width 에서 접혀야 함"
    query = f"{pair.group(1)} {pair.group(2)}"
    assert query in rationale  # 파일 원문(값)에 실재
    assert query not in folded  # 구 레시피 blob 에선 접혀서 소실되던 질의

    found = await m.search(query)
    assert [r["id"] for r in found] == ["d-fold"]  # 무음 실패 금지
    assert found[0]["score"] >= 1.0  # 빈도점수도 원문 기준


async def test_memory_search_quoted_scalar_verbatim(tmp_path):
    """같은 클래스 회귀 — 따옴표 강제 스칼라(': ' 포함)의 작은따옴표 이중화('')가
    원문 매칭을 깨면 안 됨. blob 은 직렬화 출력이 아니라 값 원문이어야 한다."""
    m = MemoryAdapter(base_dir=tmp_path)
    await m.create("decision", {"decision_id": "d-quote", "title": "결론: 'A안' 채택"})
    found = await m.search("'A안' 채택")  # 구 blob: ''A안'' 으로 변형되어 0건이던 질의
    assert [r["id"] for r in found] == ["d-quote"]


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


# ── stub 어댑터(studio/n8n) health + NotImplemented ─────────────
@pytest.mark.parametrize("Adapter", [StudioAdapter, N8nAdapter])
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
    a = StudioAdapter(client=client, url="http://fake")
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


def test_notion_resource_props_map_to_real_db_schema():
    """resource 생성 매핑 — 실 RESOURCE DB(한국어 프로퍼티) 정합 핀.

    스키마 필드명(영문)을 그대로 프로퍼티명으로 보내면 실DB 에서 전 필드
    validation_error(400). 구조 매핑 + status/select 옵션 번역을 고정한다.
    """
    n = NotionAdapter(token="t")
    data = {
        "resource_id": "res_x", "title": "제목", "source_url": "https://e.com/a",
        "resource_type": "아티클", "domain": "기타", "status": "신규",
        "raw_content": "본문", "captured_at": "2026-07-13T01:00:00+09:00",
    }
    props = n._to_notion_properties("resource", data)
    assert set(props) == {"이름", "원본 URL", "유형", "카테고리", "상태", "수집일"}
    assert props["이름"]["title"][0]["text"]["content"] == "제목"
    assert props["원본 URL"] == {"url": "https://e.com/a"}
    assert props["유형"] == {"select": {"name": "아티클"}}
    assert props["카테고리"] == {"select": {"name": "일반"}}   # 기타 → 실DB 옵션 번역
    assert props["상태"] == {"status": {"name": "미처리"}}     # 신규 → status 는 자동생성 불가라 필수
    assert props["수집일"] == {"date": {"start": "2026-07-13T01:00:00+09:00"}}
    # resource_id 는 대응 프로퍼티 없음 — 미전송(멱등키는 CreateStore 로컬), raw_content 는 본문 블록으로
    children = n._to_notion_children("resource", data)
    assert len(children) == 1
    assert children[0]["paragraph"]["rich_text"][0]["text"]["content"] == "본문"


def test_notion_children_split_long_text():
    # Notion rich_text 2000자 제한 — 1900자 단위 분할
    n = NotionAdapter(token="t")
    blocks = n._to_notion_children("resource", {"raw_content": "a" * 4000})
    assert len(blocks) == 3
    assert sum(len(b["paragraph"]["rich_text"][0]["text"]["content"]) for b in blocks) == 4000


def test_notion_non_overridden_type_keeps_heuristic():
    # 매핑 미등록 타입(summary)은 기존 휴리스틱(필드명 = 프로퍼티명) 유지 — 회귀 방지
    n = NotionAdapter(token="t")
    props = n._to_notion_properties("summary", {"summary_id": "s1", "title": "요약", "domain": "AI"})
    assert "title" in props and props["domain"] == {"select": {"name": "AI"}}


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


async def test_notion_search_paginates_past_first_page():
    """10행 초과분 회수 결함 회귀 핀 — page_size/start_cursor 를 존중하는 목.

    타깃이 120번째 행(첫 페이지 밖, 커서 2페이지째)이라 과거 page_size:10
    단발 쿼리로는 조용히 회수 0. 커서 페이지네이션으로 반드시 회수돼야 한다.
    """
    dataset = [{
        "id": f"p{i}",
        "properties": {
            "summary_id": {"type": "rich_text", "rich_text": [{"plain_text": f"s{i}"}]},
            "title": {"type": "title",
                      "title": [{"plain_text": "타깃요약" if i == 120 else "잡음"}]},
        },
    } for i in range(150)]

    def handler(request):
        body = json.loads(request.content or b"{}")
        size = int(body.get("page_size", 100))
        start = int(body.get("start_cursor") or 0)
        chunk = dataset[start:start + size]
        nxt = start + size
        has_more = nxt < len(dataset)
        return httpx.Response(200, json={
            "results": chunk,
            "has_more": has_more,
            "next_cursor": str(nxt) if has_more else None,
        })

    client = httpx.AsyncClient(base_url="https://api.notion.com/v1",
                               transport=httpx.MockTransport(handler))
    a = NotionAdapter(client=client, token="t")
    a.db_ids = {k: "db" for k in a.db_ids}
    res = await a.search("타깃요약", {"type": "summary"})
    assert [r["id"] for r in res] == ["s120"]
    # limit 조기 종료: 매칭이 두 건이어도 상한만큼만 (전건 스캔 비용 제어)
    a2 = NotionAdapter(client=client, token="t")
    a2.db_ids = {k: "db" for k in a2.db_ids}
    res2 = await a2.search("", {"type": "summary", "limit": 5})
    assert len(res2) == 5
    await client.aclose()
