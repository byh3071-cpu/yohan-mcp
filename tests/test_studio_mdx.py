# -*- coding: utf-8 -*-
"""StudioAdapter MDX 발행 (P6) — frontmatter/슬러그/멱등/contradiction/PR폴백/봉투완전성."""
from adapters.memory_adapter import MemoryAdapter
from adapters.n8n_adapter import N8nAdapter
from adapters.notion_adapter import NotionAdapter
from adapters.qdrant_adapter import QdrantAdapter
from adapters.studio_adapter import StudioAdapter
from core.router import SmartRouter
from core.schema_validator import SchemaValidator
from core import tools as T
from core.tools import ToolContext


def _summary(**over):
    base = {
        "summary_id": "sum_x", "resource_id": "res_x", "title": "어텐션 정리",
        "summary": "어텐션은 토큰 관련도를 계산한다.", "key_insights": ["멀티헤드"],
        "domain": "AI", "status": "완료", "created_at": "2026-06-08T09:30:00+09:00",
    }
    base.update(over)
    return base


def _ctx(tmp_path, studio=None):
    adapters = {
        "notion": NotionAdapter(token=""),
        "memory": MemoryAdapter(base_dir=tmp_path),
        "qdrant": QdrantAdapter(url=""),
        "studio": studio if studio is not None else StudioAdapter(url="", api_key=""),
        "n8n": N8nAdapter(url=""),
    }
    return ToolContext(adapters, SmartRouter(adapters), SchemaValidator())


# ── frontmatter 스키마 ──────────────────────────────────────────
async def test_frontmatter_complete_is_valid():
    a = StudioAdapter(url="", api_key="")
    res = await a.publish(_summary())
    fm = res["frontmatter"]
    assert res["frontmatter_valid"] is True
    assert {"title", "description", "date", "tags", "category", "published"} <= set(fm)
    assert fm["date"] == "2026-06-08"          # created_at → date(YYYY-MM-DD)
    assert fm["published"] is False             # 드라이런 = 초안 = 미노출
    assert fm["tags"] == ["AI"] and fm["category"] == "AI"


async def test_frontmatter_invalid_when_required_missing():
    # domain 없음 → tags 비고 → 필수(tags) 미충족 → schema_valid(frontmatter) False
    s = _summary()
    s.pop("domain")
    a = StudioAdapter(url="", api_key="")
    res = await a.publish(s)
    assert res["frontmatter_valid"] is False
    assert any("tags" in e for e in res["frontmatter_errors"])


async def test_mdx_render_shape():
    a = StudioAdapter(url="", api_key="")
    res = await a.publish(_summary())
    mdx = res["mdx"]
    assert mdx.startswith("---\n")
    assert "\n---\n" in mdx                      # frontmatter 종료 펜스
    assert "# 어텐션 정리" in mdx                  # 본문(POST body) 포함
    assert res["diff"]                           # 신규 unified diff(빈→전문)


# ── 슬러그(한글/특수문자) + 중복 -2 + contradiction ─────────────
def test_slug_ascii_for_korean_and_special():
    post = StudioAdapter.summary_to_post(_summary(title="한글!! @#$ 제목"))
    assert post["slug"] and all(c.islower() or c.isdigit() or c == "-" for c in post["slug"])


async def test_slug_dedup_suffix_and_contradiction(tmp_path):
    a = StudioAdapter(repo_path=str(tmp_path), mode="file", journal_path=tmp_path / "pub.jsonl")
    r1 = await a.publish(_summary(summary="첫 번째 본문"), approved=True)
    r2 = await a.publish(_summary(summary="다른 본문"), approved=True)  # 같은 제목 다른 본문
    assert r1["slug"] != r2["slug"]
    assert r2["slug"].endswith("-2")
    assert r2["contradiction"] is True           # 같은 slug 다른 content-hash
    blog = tmp_path / "src" / "content" / "blog"
    assert (blog / f"{r1['slug']}.mdx").exists()
    assert (blog / f"{r2['slug']}.mdx").exists()


async def test_republish_after_contradiction_is_noop(tmp_path):
    # 회귀: contradiction 으로 접미 슬러그(-2)에 발행된 콘텐츠를 완전히 동일하게 재발행하면
    # already_published(no-op) 여야 한다. base_slug 한정 앵커링이면 -3, -4 로 무한 증식했다.
    a = StudioAdapter(repo_path=str(tmp_path), mode="file", journal_path=tmp_path / "pub.jsonl")
    await a.publish(_summary(summary="첫 번째 본문"), approved=True)          # sum-x
    r2 = await a.publish(_summary(summary="다른 본문"), approved=True)         # sum-x-2 (contradiction)
    assert r2["slug"].endswith("-2") and r2["contradiction"] is True
    r3 = await a.publish(_summary(summary="다른 본문"), approved=True)         # -2 와 동일 → no-op
    assert r3["already_published"] is True
    assert r3["published"] is False and r3["dry_run"] is True
    assert r3["slug"] == r2["slug"]                # 기존 접미 슬러그 재사용
    assert r3["contradiction"] is False
    blog = tmp_path / "src" / "content" / "blog"
    assert len(list(blog.glob("*.mdx"))) == 2      # sum-x.mdx, sum-x-2.mdx — 증식 없음


# ── 멱등 (같은 slug+content-hash 면 no-op) ──────────────────────
async def test_idempotent_no_op_same_content(tmp_path):
    a = StudioAdapter(repo_path=str(tmp_path), mode="file", journal_path=tmp_path / "pub.jsonl")
    r1 = await a.publish(_summary(), approved=True)
    r2 = await a.publish(_summary(), approved=True)   # 완전히 동일
    assert r1["published"] is True
    assert r2["already_published"] is True
    assert r2["published"] is False and r2["dry_run"] is True
    files = list((tmp_path / "src" / "content" / "blog").glob("*.mdx"))
    assert len(files) == 1                        # 중복 파일 안 만듦


# ── PR 모드 graceful 폴백 (git 실패해도 안 죽음) ────────────────
async def test_pr_mode_graceful_fallback_when_not_git(tmp_path):
    a = StudioAdapter(repo_path=str(tmp_path), mode="pr", journal_path=tmp_path / "pub.jsonl")
    res = await a.publish(_summary(), approved=True)   # tmp 는 git repo 아님 → git 실패
    assert res["dry_run"] is True and res["published"] is False
    assert res.get("errors")
    assert "폴백" in res["detail"]


# ── PR 모드: 커밋은 됐지만 push 실패 → 발행 미완료 + 멱등 저널 미오염 ──
def _git_init(repo):
    import subprocess
    for args in (
        ["init"], ["config", "user.email", "t@t.t"], ["config", "user.name", "t"],
        ["commit", "--allow-empty", "-m", "init"],
    ):
        subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)


async def test_pr_mode_push_fail_not_published_and_journal_clean(tmp_path):
    # 실제 git repo(체크아웃/커밋 성공) + origin 없음 → push 만 실패(pushed=False)
    _git_init(tmp_path)
    journal = tmp_path / "pub.jsonl"
    a = StudioAdapter(repo_path=str(tmp_path), mode="pr", journal_path=journal)
    res = await a.publish(_summary(), approved=True)
    # push 실패는 '발행됨'이 아니다
    assert res["published"] is False
    # 로컬 브랜치/커밋이 남는 부작용 → dry_run(무변경) 아님 + side_effect 명시
    assert res["dry_run"] is False
    assert res["side_effect"] == "local_branch_committed"
    assert res.get("errors")
    assert res["pr"]["pushed"] is False
    # 멱등 저널 미오염 → 재발행이 already_published(no-op)로 막히지 않는다
    assert not journal.exists() or journal.read_text(encoding="utf-8").strip() == ""
    res2 = await a.publish(_summary(), approved=True)
    assert res2["already_published"] is False   # no-op 로 막히지 않음 → 재시도 가능
    assert res2["published"] is False


def _git_add_bare_origin(repo, bare):
    """bare repo 를 origin 으로 등록(=원격 장애 복구 시뮬레이션)."""
    import subprocess
    subprocess.run(["git", "init", "--bare", str(bare)], capture_output=True, text=True, check=True)
    subprocess.run(["git", "remote", "add", "origin", str(bare)],
                   cwd=repo, capture_output=True, text=True, check=True)


async def test_pr_mode_retry_reaches_push_after_origin_recovers(tmp_path):
    # 재진입 견고화 회귀: 1차 push 실패로 orphan 발행 커밋만 남고, origin 복구 후
    # 동일-content 재호출이 'nothing to commit' 폴백에 갇히지 않고 실제 push 에 재도달해야 한다.
    import subprocess
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    journal = tmp_path / "pub.jsonl"
    a = StudioAdapter(repo_path=str(repo), mode="pr", journal_path=journal)

    # 1차: origin 없음 → push 실패, 로컬 브랜치 publish/<slug> + 발행 커밋 잔류
    res1 = await a.publish(_summary(), approved=True)
    assert res1["published"] is False and res1["pr"]["pushed"] is False
    slug = res1["slug"]
    branch = f"publish/{slug}"

    # 원격 장애 복구: bare repo 를 origin 으로 등록
    bare = tmp_path / "origin.git"
    _git_add_bare_origin(repo, bare)

    # 2차: 동일 summary(=동일 content-hash) 재호출 → commit 단계 건너뛰고 push 재도달
    res2 = await a.publish(_summary(), approved=True)
    assert res2["already_published"] is False        # 저널 미오염 → no-op 아님
    assert res2["pr"]["pushed"] is True               # 실제 push 재도달(핵심 회귀 검증)
    assert res2["published"] is True                  # 발행 완료로 승격
    assert res2["dry_run"] is False

    # origin 에 실제 발행 브랜치 ref 가 존재해야 한다
    ls = subprocess.run(["git", "ls-remote", "origin", branch],
                        cwd=repo, capture_output=True, text=True, check=True)
    assert branch in ls.stdout                        # origin refs 존재 확인

    # 멱등 저널이 push 성공 시에만 기록됨 → 재재호출은 already_published(no-op)
    res3 = await a.publish(_summary(), approved=True)
    assert res3["already_published"] is True and res3["published"] is False


# ── tool_publish 봉투 완전성(불변 봉투 + MDX 신호) ─────────────
async def test_tool_publish_envelope_completeness(tmp_path):
    ctx = _ctx(tmp_path)
    env = await T.tool_publish(ctx, _summary())
    # 표준 불변 봉투
    assert set(env) >= {"data", "verification", "provenance"}
    for k in ("schema_valid", "rulebook_pass", "cross_links_intact",
              "contradiction_detected", "quality_score"):
        assert k in env["verification"]
    # MDX 발행 신호 노출
    for k in ("mode", "slug", "target_path", "mdx", "frontmatter_valid",
              "diff", "already_published", "contradiction"):
        assert k in env, k
    assert env["mode"] == "dry_run"
    assert env["data"]["status"] == "초안"           # data=POST 유지
    assert env["dry_run"] is True and env["published"] is False
