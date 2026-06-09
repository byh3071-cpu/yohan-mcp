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


async def test_publish_dry_run_when_no_repo():
    # (P6) STUDIO_REPO_PATH 없음 → 드라이런(파일 미작성, MDX 전문 + 타겟 경로 반환)
    a = StudioAdapter(url="", api_key="")
    res = await a.publish(SUMMARY)
    assert res["dry_run"] is True and res["published"] is False
    assert res["mode"] == "dry_run"
    assert res["post"]["status"] == "초안"
    assert res["mdx"].startswith("---")              # frontmatter 포함 MDX 전문
    assert "published: false" in res["mdx"]           # 초안 = 미노출
    assert res["target_path"].endswith(".mdx")
    assert res["frontmatter_valid"] is True
    ok, errs = VALIDATOR.validate("post", res["post"])
    assert ok, errs


async def test_publish_file_mode_writes_when_approved(tmp_path):
    # (P6) file 모드 + 승인 → 실제 {slug}.mdx 파일 쓰기(published: true)
    a = StudioAdapter(repo_path=str(tmp_path), mode="file",
                      journal_path=tmp_path / "pub.jsonl")
    res = await a.publish(SUMMARY, approved=True)
    assert res["dry_run"] is False and res["published"] is True
    target = tmp_path / "src" / "content" / "blog" / f"{res['slug']}.mdx"
    assert target.exists()
    body = target.read_text(encoding="utf-8")
    assert body.startswith("---") and "published: true" in body
    assert res["post"]["status"] == "발행"


async def test_publish_file_mode_blocked_without_approval(tmp_path):
    # (P6) always_gate — file 모드라도 미승인이면 실파일 안 씀(드라이런 폴백)
    a = StudioAdapter(repo_path=str(tmp_path), mode="file",
                      journal_path=tmp_path / "pub.jsonl")
    res = await a.publish(SUMMARY, approved=False)
    assert res["dry_run"] is True and res["published"] is False
    assert "미승인" in res["detail"] or "always_gate" in res["detail"]
    blog = tmp_path / "src" / "content" / "blog"
    assert not blog.exists() or not list(blog.glob("*.mdx"))   # 실파일 없음


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
