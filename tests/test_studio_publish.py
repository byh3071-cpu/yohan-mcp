# -*- coding: utf-8 -*-
"""StudioAdapter — SUMMARY→POST 변환 + 발행(드라이런/실) (P3)."""
import json

import httpx
import pytest

from adapters.studio_adapter import StudioAdapter
from core.schema_validator import SchemaValidator

VALIDATOR = SchemaValidator()
SUMMARY = {
    "summary_id": "sum_2026_0608", "resource_id": "res_1", "title": "어텐션 메커니즘 정리",
    "summary": "어텐션은 토큰 관련도를 계산해 가중 평균한다.",
    "key_insights": ["멀티헤드는 표현 다양성을 높인다", "스케일링은 그래디언트 안정화"],
    "domain": "AI", "status": "완료", "created_at": "2026-06-08T09:30:00+09:00",
}


def test_summary_to_post_schema_conformant():
    post = StudioAdapter.summary_to_post(SUMMARY)
    ok, errs = VALIDATOR.validate("post", post)
    assert ok, errs
    assert post["summary_id"] == "sum_2026_0608"  # published_as FK
    assert post["status"] == "초안"
    assert "## 핵심 인사이트" in post["body"]
    assert post["tags"] == ["AI"]


def test_slug_ascii_even_for_korean_title():
    # 전부 한글 제목 → 슬러그가 summary_id 기반 ascii 로 폴백, 비지 않음
    post = StudioAdapter.summary_to_post(dict(SUMMARY, title="한글만 있는 제목"))
    assert post["slug"] and all(c.islower() or c.isdigit() or c == "-" for c in post["slug"])


async def test_publish_dry_run_when_no_credentials():
    a = StudioAdapter(url="", api_key="")
    res = await a.publish(SUMMARY)
    assert res["dry_run"] is True and res["published"] is False
    assert "보류" in res["detail"]
    assert res["post"]["status"] == "초안"
    ok, errs = VALIDATOR.validate("post", res["post"])
    assert ok, errs


async def test_publish_real_when_credentials_set():
    captured = {}

    def handler(request):
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"id": "post_remote_123"})

    client = httpx.AsyncClient(base_url="https://studio.example.com",
                               headers={"Authorization": "Bearer k"},
                               transport=httpx.MockTransport(handler))
    a = StudioAdapter(client=client, url="https://studio.example.com", api_key="k")
    res = await a.publish(SUMMARY)
    assert res["dry_run"] is False and res["published"] is True
    assert captured["path"].endswith("/posts")
    assert res["post"]["status"] == "발행"
    assert res["post"]["post_id"] == "post_remote_123"  # API id 로 보정
    assert "published_at" in res["post"]
    await client.aclose()


async def test_create_post_dry_run_and_real():
    # 드라이런: 발행 안 하고 레코드화
    a = StudioAdapter(url="", api_key="")
    post = StudioAdapter.summary_to_post(SUMMARY)
    rec = await a.create("post", post)
    assert rec["backend"] == "studio" and rec["type"] == "post"

    # 비-post 타입 + search 는 여전히 미지원
    with pytest.raises(NotImplementedError):
        await a.create("x", {})
    with pytest.raises(NotImplementedError):
        await a.search("q")


async def test_health_no_url():
    a = StudioAdapter(url="", api_key="")
    h = await a.health_check()
    assert set(h) == {"ok", "latency_ms", "detail"} and h["ok"] is False
