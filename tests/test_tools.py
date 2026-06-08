# -*- coding: utf-8 -*-
"""의도 기반 도구 — search/create/update/status/check/ingest 통합테스트."""
import httpx
import pytest

from adapters.base import BackendAdapter, health, make_record
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

VALID_DECISION = {
    "decision_id": "dec_test_001",
    "title": "테스트 결정",
    "context": "배경 설명",
    "decision": "이렇게 하기로 함",
    "rationale": "근거 설명",
    "status": "승인",
    "decided_at": "2026-06-08T09:30:00+09:00",
}

VALID_SUMMARY = {
    "summary_id": "s1",
    "resource_id": "r1",
    "title": "제목",
    "summary": "요약 본문",
    "key_insights": ["a", "b"],
    "domain": "AI",
    "status": "완료",
    "created_at": "2026-06-08T09:30:00+09:00",
}


class FakeAdapter(BackendAdapter):
    def __init__(self, name, records=None):
        self.name = name
        self._records = records or []

    async def search(self, query, opts=None):
        return self._records

    async def create(self, type_, data):
        return make_record(str(data.get("id", "x")), type_, self.name, data)

    async def update(self, id_, data, type_=None):
        return make_record(id_, "t", self.name, data)

    async def health_check(self):
        return health(True, 1, "fake")


def _ctx_with_memory(tmp_path):
    adapters = {
        "notion": NotionAdapter(token=""),
        "memory": MemoryAdapter(base_dir=tmp_path),
        "qdrant": QdrantAdapter(url=""),
        "studio": StudioAdapter(url=""),
        "n8n": N8nAdapter(url=""),
    }
    return ToolContext(adapters, SmartRouter(adapters), SchemaValidator())


def _envelope_shape(env):
    assert set(env) >= {"data", "verification", "provenance"}
    assert "schema_valid" in env["verification"]
    assert "sources_used" in env["provenance"]


# ── search (RRF 융합 봉투) ──────────────────────────────────────
async def test_tool_search_rrf_envelope():
    a = FakeAdapter("notion", [make_record("x1", "summary", "notion", {"id": "x1"}),
                               make_record("x2", "summary", "notion", {"id": "x2"})])
    b = FakeAdapter("memory", [make_record("x2", "summary", "memory", {"id": "x2"})])
    adapters = {"notion": a, "memory": b}
    ctx = ToolContext(adapters, SmartRouter(adapters), SchemaValidator())
    env = await T.tool_search(ctx, "q")
    _envelope_shape(env)
    results = env["data"]["results"]
    assert results[0]["id"] == "x2"  # 중복 → 최상위
    assert set(env["provenance"]["sources_used"]) == {"notion", "memory"}


# ── create (타입→백엔드 자동선택 + 검증) ────────────────────────
async def test_tool_create_routes_to_memory(tmp_path):
    ctx = _ctx_with_memory(tmp_path)
    env = await T.tool_create(ctx, "decision", VALID_DECISION)
    _envelope_shape(env)
    assert env["verification"]["schema_valid"] is True
    assert env["provenance"]["sources_used"] == ["memory"]
    assert env["data"]["id"] == "dec_test_001"
    # 실제 파일로 저장됐는지
    found = await ctx.adapters["memory"].search("테스트 결정")
    assert any(r["id"] == "dec_test_001" for r in found)


async def test_tool_create_invalid_rejected(tmp_path):
    ctx = _ctx_with_memory(tmp_path)
    bad = dict(VALID_DECISION, status="확정")  # enum 위반
    env = await T.tool_create(ctx, "decision", bad)
    assert env["verification"]["schema_valid"] is False
    assert env["data"] is None
    assert env["errors"]


async def test_tool_create_missing_required(tmp_path):
    ctx = _ctx_with_memory(tmp_path)
    bad = {"decision_id": "d", "title": "t"}  # 필수 누락
    env = await T.tool_create(ctx, "decision", bad)
    assert env["verification"]["schema_valid"] is False


async def test_tool_create_notion_failure_enveloped(tmp_path):
    """notion 라우팅 + 토큰 미설정 → 예외 대신 errors 봉투(장애 격리)."""
    ctx = _ctx_with_memory(tmp_path)  # notion token=""
    env = await T.tool_create(ctx, "summary", VALID_SUMMARY)  # 유효 데이터 → 어댑터 도달
    _envelope_shape(env)
    assert env["data"] is None
    assert env["errors"]  # RuntimeError 가 봉투로 흡수됨
    assert env["provenance"]["sources_used"] == []


# ── update (부분 검증 + 라우팅) ─────────────────────────────────
async def test_tool_update_partial(tmp_path):
    ctx = _ctx_with_memory(tmp_path)
    await T.tool_create(ctx, "decision", VALID_DECISION)
    env = await T.tool_update(ctx, "dec_test_001", {"status": "보류"}, type_="decision")
    _envelope_shape(env)
    assert env["verification"]["schema_valid"] is True
    assert env["data"]["data"]["status"] == "보류"


async def test_tool_update_no_type_is_unvalidated(tmp_path):
    """type_ 미지정 update 는 schema_valid=None(검증 안 함)으로 정직하게 표기."""
    ctx = _ctx_with_memory(tmp_path)
    await T.tool_create(ctx, "decision", VALID_DECISION)
    env = await T.tool_update(ctx, "dec_test_001", {"status": "재검토"})  # type_ 생략
    assert env["verification"]["schema_valid"] is None
    assert env["data"]["data"]["status"] == "재검토"


# ── status (5개 health 한 줄 요약) ──────────────────────────────
async def test_tool_status_summarizes_five(tmp_path):
    ctx = _ctx_with_memory(tmp_path)
    env = await T.tool_status(ctx)
    _envelope_shape(env)
    summaries = env["data"]["summaries"]
    assert len(summaries) == 5
    assert any(s.startswith("memory:") and "OK" in s for s in summaries)
    assert set(env["data"]["details"]) == {"notion", "memory", "qdrant", "studio", "n8n"}


# ── check (스키마 검증 도구) ────────────────────────────────────
async def test_tool_check(tmp_path):
    ctx = _ctx_with_memory(tmp_path)
    env_ok = await T.tool_check(ctx, "decision", VALID_DECISION)
    assert env_ok["verification"]["schema_valid"] is True
    env_types = await T.tool_check(ctx, "decision", None)
    assert "decision" in " ".join(env_types["data"]["known_types"])


# ── search 부분검증 ─────────────────────────────────────────────
async def test_tool_search_partial_validation():
    """검색 결과는 부분검증 → 부분데이터도 schema_valid True (전체검증이면 False)."""
    rec = make_record("d1", "decision", "memory", {"decision_id": "d1", "title": "t", "status": "승인"})
    adapters = {"memory": FakeAdapter("memory", [rec]), "notion": FakeAdapter("notion", [])}
    ctx = ToolContext(adapters, SmartRouter(adapters), SchemaValidator())
    env = await T.tool_search(ctx, "q")
    assert env["data"]["count"] == 1
    assert env["verification"]["schema_valid"] is True
    # 동일 데이터로 전체검증은 required 누락이라 False 임을 대비 확인
    full_ok, _ = ctx.validator.validate("decision", rec["data"])
    assert full_ok is False


# ── format 검증 (date-time/uri) ─────────────────────────────────
def test_schema_format_validation():
    v = SchemaValidator()
    assert v.validate("decision", VALID_DECISION)[0] is True
    bad, errs = v.validate("decision", dict(VALID_DECISION, decided_at="NOT-A-DATE"))
    assert bad is False and errs
    bad_uri, _ = v.validate("ingest", {
        "ingest_id": "g1", "source": "web", "raw": "x",
        "ingested_at": "2026-06-08T09:30:00+09:00", "source_url": "not a url",
    })
    assert bad_uri is False


# ── ingest 3중 적재 (Notion+Qdrant+memory) ─────────────────────
async def test_tool_ingest_triple(tmp_path, monkeypatch):
    async def fake_fetch(url, client=None):
        return ("테스트 제목", "본문 텍스트 어텐션 메커니즘")
    monkeypatch.setattr(T, "_fetch_url", fake_fetch)

    def handler(request):
        return httpx.Response(200, json={"id": "pg_1", "properties": {}})
    nclient = httpx.AsyncClient(base_url="https://api.notion.com/v1", transport=httpx.MockTransport(handler))
    notion = NotionAdapter(client=nclient, token="t")
    notion.db_ids = {k: "db" for k in notion.db_ids}

    adapters = {
        "notion": notion,
        "memory": MemoryAdapter(base_dir=tmp_path),
        "qdrant": QdrantAdapter(url=None, embedder=HashEmbedder()),
    }
    ctx = ToolContext(adapters, SmartRouter(adapters), SchemaValidator())
    env = await T.tool_ingest(ctx, "https://example.com/post")
    _envelope_shape(env)
    assert set(env["provenance"]["sources_used"]) == {"notion", "qdrant", "memory"}
    assert env["verification"]["schema_valid"] is True  # resource 스키마 통과
    assert env["data"]["resource_id"].startswith("res_")

    # memory ingest 로그가 실제로 저장됐는지
    found = await adapters["memory"].search("본문", {"type": "ingest"})
    assert found and found[0]["data"]["target_resource_id"] == env["data"]["resource_id"]
    # Qdrant 에 벡터 적재됐는지
    qres = await adapters["qdrant"].search("어텐션", {"top_k": 3})
    assert qres
    await nclient.aclose()
    await adapters["qdrant"].aclose()


async def test_tool_ingest_isolates_failure(tmp_path, monkeypatch):
    """notion 실패(토큰 없음)해도 qdrant+memory 는 적재."""
    async def fake_fetch(url, client=None):
        return ("제목", "본문")
    monkeypatch.setattr(T, "_fetch_url", fake_fetch)
    adapters = {
        "notion": NotionAdapter(token=""),
        "memory": MemoryAdapter(base_dir=tmp_path),
        "qdrant": QdrantAdapter(url=None, embedder=HashEmbedder()),
    }
    ctx = ToolContext(adapters, SmartRouter(adapters), SchemaValidator())
    env = await T.tool_ingest(ctx, "https://example.com/x")
    assert "notion" not in env["provenance"]["sources_used"]
    assert {"qdrant", "memory"} <= set(env["provenance"]["sources_used"])
    assert any("notion" in e for e in env["errors"])
    await adapters["qdrant"].aclose()


# ── SSRF / HTML 추출 가드 ───────────────────────────────────────
@pytest.mark.parametrize("bad", [
    "http://localhost/x", "http://127.0.0.1/x",
    "http://169.254.169.254/latest/meta-data/",  # IMDS
    "file:///etc/passwd", "ftp://x/y", "http://[::1]/",
])
async def test_fetch_url_blocks_ssrf(bad):
    with pytest.raises(ValueError):
        await T._fetch_url(bad)


def test_extract_html_no_title_dup_no_script_leak():
    title, text = T._extract_html(
        "<html><head><title>제목</title></head>"
        "<body><script>var k='SECRET'</script><p>본문 단락</p></body></html>"
    )
    assert title == "제목"
    assert "제목" not in text       # 제목이 본문에 중복되지 않음
    assert "SECRET" not in text     # script 내용 누출 없음
    assert "본문 단락" in text


# ── 남은 stub 도구 (plan=P4, run_action 미등록 프로토콜) ──────────
async def test_stub_tools(tmp_path):
    ctx = _ctx_with_memory(tmp_path)
    for coro in [T.tool_run_action(ctx, "unknown_proto"), T.tool_plan(ctx, "g")]:
        env = await coro
        _envelope_shape(env)
        assert env["data"]["status"] == "stub"
    # 미등록 프로토콜은 등록된 프로토콜 목록을 안내
    env = await T.tool_run_action(ctx, "unknown_proto")
    assert "publish_summary" in env["data"]["known_protocols"]
