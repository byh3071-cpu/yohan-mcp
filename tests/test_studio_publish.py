# -*- coding: utf-8 -*-
"""StudioAdapter — SUMMARY→POST 변환 + 발행(드라이런/실) (P3)."""
import json
import subprocess

import httpx
import pytest

from adapters.studio_adapter import StudioAdapter, StudioPublishJournal
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


def test_publish_journal_default_under_memory_dir(tmp_path, monkeypatch):
    # 발행 멱등 저널도 다른 런타임 저널(runs/approvals/created/links/policy)과 동일하게
    # 격리 스위치를 따른다 — brain-linked(MEMORY_DIR 설정) 시 ops/mcp-runtime 하위.
    # (E4-01이 런타임 저널을 ops/mcp-runtime로 이동; ROOT/data 예외 제거 의도는 유지.)
    monkeypatch.delenv("MCP_RUNTIME_DIR", raising=False)  # MEMORY_DIR 기준 해소를 검증
    monkeypatch.setenv("MEMORY_DIR", str(tmp_path))
    expected = tmp_path / "ops" / "mcp-runtime" / "studio_published.jsonl"
    assert StudioPublishJournal().path == expected
    # 명시 journal_path 없는 기본 어댑터도 같은 base 사용
    assert StudioAdapter(url="", api_key="")._journal.path == expected
    # 명시 journal_path 는 그대로 우선(하위호환)
    explicit = tmp_path / "custom.jsonl"
    assert StudioPublishJournal(explicit).path == explicit


def _fake_git_run(fail_cmd: str, seen: list | None = None):
    """studio_adapter.subprocess.run 대역 — fail_cmd(예: 'push','gh') 가 걸린 호출은
    TimeoutExpired 를 즉시 던지고(정지 상황 시뮬), 나머지 git 은 성공으로 처리한다.
    diff --cached --quiet 는 '스테이지 변경 있음'(exit 1)으로 응답해 commit 경로를 태운다.
    seen 이 주어지면 모든 호출의 timeout kwarg 를 기록해 '상한 지정' 자체를 회귀 고정한다."""
    def fake_run(cmd, *args, **kw):
        if seen is not None:
            seen.append(kw.get("timeout"))
        argv = list(cmd)
        program = argv[0] if argv else ""
        if fail_cmd == "gh" and program == "gh":
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout") or 1)
        if fail_cmd in argv:  # 예: 'push' in ['git','push','-u',...]
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout") or 1)
        if "diff" in argv and "--cached" in argv:
            return subprocess.CompletedProcess(cmd, 1, "", "")  # 변경 있음 → commit 진행
        return subprocess.CompletedProcess(cmd, 0, "", "")
    return fake_run


async def test_publish_pr_push_timeout_is_graceful(tmp_path, monkeypatch):
    # (견고성) git push 가 정지(타임아웃)해도 워커가 무한 블록되지 않고 graceful 반환.
    # 타임아웃은 push 지점에서 포착되어 '재시도 가능' 의미(dry_run=False + 로컬 브랜치 잔류)를
    # 보존해야 한다 — 외부 except 로 새면 dry_run=True(무변경 거짓 보고)가 되므로 그 구분을 단정한다.
    seen: list = []
    monkeypatch.setattr("adapters.studio_adapter.subprocess.run", _fake_git_run("push", seen))
    a = StudioAdapter(repo_path=str(tmp_path), mode="pr", journal_path=tmp_path / "pub.jsonl")
    res = await a.publish(SUMMARY, approved=True)
    assert res["published"] is False
    assert res["dry_run"] is False                       # push 지점 포착 증거(외부 폴백이면 True)
    assert res.get("side_effect") == "local_branch_committed"
    assert res["pr"]["pushed"] is False
    # 재시도 가능 — 멱등 저널에 발행 기록이 남지 않아야 한다.
    assert a._journal.all() == []
    # 결함 회귀 고정 — 모든 git 호출(push 포함)에 양수 timeout 이 실제로 전달됐다.
    # (fake 안에서 assert 하면 publish() 의 광범위 except 가 삼켜 false green 이 되므로 호출 후 검사.)
    assert seen and all(isinstance(t, (int, float)) and t > 0 for t in seen)


async def test_publish_pr_gh_timeout_still_published(tmp_path, monkeypatch):
    # (견고성) push 성공 후 gh pr create 가 정지(타임아웃)해도 gh 실패로 흡수 →
    # 발행은 완료(pushed=True, 저널 기록)로 유지되어야 한다(외부 except 로 새면 published=False).
    seen: list = []
    monkeypatch.setattr("adapters.studio_adapter.subprocess.run", _fake_git_run("gh", seen))
    a = StudioAdapter(repo_path=str(tmp_path), mode="pr", journal_path=tmp_path / "pub.jsonl")
    res = await a.publish(SUMMARY, approved=True)
    assert res["published"] is True and res["dry_run"] is False
    assert res["pr"]["pushed"] is True
    assert len(a._journal.all()) == 1                    # 발행 기록됨
    # gh pr create 호출에도 timeout 이 전달됐다(상한 지정 회귀 고정).
    assert seen and all(isinstance(t, (int, float)) and t > 0 for t in seen)
