# -*- coding: utf-8 -*-
"""Studio 백엔드 어댑터 (P6 — MDX 파일 발행).

발행 방식 확정(P6): 요한스튜디오 발행은 HTTP API(POST /posts) 가 아니라
**yohan-studio 레포의 `src/content/blog/{slug}.mdx` 파일을 쓰는 것**이다.
(근거: yohan-studio `src/lib/blog.ts` 의 CONTENT_DIR=src/content/blog, 로더가 `${slug}.mdx`만 읽음)

따라서 실발행 경로는 `STUDIO_REPO_PATH/src/content/blog/{slug}.mdx`. P3 의 HTTP 발행(_send/POST /posts)은 폐기.

모드(STUDIO_PUBLISH_MODE):
- dry_run(기본): 파일 안 씀. 렌더된 MDX 전문 + 타겟 경로 + unified diff 반환(ok, dry_run).
- file: STUDIO_REPO_PATH 하위에 {slug}.mdx 실제 쓰기. **always_gate** — 명시적 승인(approved) 없으면 차단→드라이런 폴백.
- pr: git 새 브랜치(publish/{slug}) → 파일 쓰기 → add/commit/push → PR(base=STUDIO_BASE_BRANCH). master 직푸시 금지.

불변 원칙: 외부 연결/경로 없으면 graceful 폴백(드라이런), 예외로 죽지 않음.
멱등: `data/studio_published.jsonl` fold → 같은 slug+content-hash 면 no-op(already_published).
변환 결과 POST 는 P1 `schemas/studio/post.schema.json` 에 정합(검증/품질점수 재사용).
"""
from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from adapters.base import BackendAdapter, _Timer, health, make_record

ROOT = Path(__file__).resolve().parent.parent
_PHASE = "P6"
_KST = timezone(timedelta(hours=9))

# 발행 서브경로 기본값(yohan-studio 글 로더 기준). env 로 덮어쓸 수 있다.
_DEFAULT_SUBDIR = "src/content/blog"
# 실발행 경로 템플릿(문서/근거용 — 실제 경로는 _target_path 가 OS-safe 하게 합성).
_PUBLISH_PATH = "{STUDIO_REPO_PATH}/src/content/blog/{slug}.mdx"

# frontmatter 필수/선택 필드 (yohan-studio 글 스키마)
_FM_REQUIRED = ("title", "description", "date", "tags", "category", "published")
_FM_OPTIONAL = ("thumbnail", "relatedProjects", "source_urls")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}$")


def _now_iso() -> str:
    return datetime.now(_KST).isoformat(timespec="seconds")


def _now_date() -> str:
    return datetime.now(_KST).date().isoformat()


def _nonempty(val) -> bool:
    if val is None:
        return False
    if isinstance(val, str):
        return val.strip() != ""
    if isinstance(val, (list, dict)):
        return len(val) > 0
    return True


class StudioPublishJournal:
    """발행 멱등 저널 — append-only JSONL(`data/studio_published.jsonl`).

    한 줄 = 한 발행 기록 {slug, content_hash, target_path, mode, summary_id, ts}.
    같은 slug+content_hash 면 이미 발행됨(no-op), 같은 slug 다른 hash 면 contradiction.
    """

    def __init__(self, path: str | os.PathLike | None = None) -> None:
        self.path = Path(path) if path else (ROOT / "data" / "studio_published.jsonl")

    def all(self) -> list[dict]:
        if not self.path.exists():
            return []
        out: list[dict] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def record(self, entry: dict) -> dict:
        entry = {**entry, "ts": _now_iso()}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry


class StudioAdapter(BackendAdapter):
    name = "studio"

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        url: str | None = None,
        api_key: str | None = None,
        *,
        repo_path: str | os.PathLike | None = None,
        subdir: str | None = None,
        base_branch: str | None = None,
        mode: str | None = None,
        journal_path: str | os.PathLike | None = None,
    ) -> None:
        # url/api_key 는 health_check 연결 점검 용도로만 남는다(발행은 MDX 파일).
        self.url = url if url is not None else os.getenv("STUDIO_API_URL")
        self.api_key = api_key if api_key is not None else os.getenv("STUDIO_API_KEY")
        # P6 — MDX 파일 발행 설정
        rp = repo_path if repo_path is not None else os.getenv("STUDIO_REPO_PATH")
        self.repo_path = str(rp) if rp else None
        self.subdir = subdir if subdir is not None else os.getenv("STUDIO_PUBLISH_SUBDIR", _DEFAULT_SUBDIR)
        self.base_branch = base_branch if base_branch is not None else os.getenv("STUDIO_BASE_BRANCH", "master")
        self.mode = (mode if mode is not None else os.getenv("STUDIO_PUBLISH_MODE", "dry_run")).lower()
        self._journal = StudioPublishJournal(journal_path)
        self._client = client
        self._owns_client = client is None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
            self._client = httpx.AsyncClient(base_url=self.url or "", headers=headers, timeout=10.0)
        return self._client

    @property
    def can_publish(self) -> bool:
        """실발행(파일/PR 쓰기) 가능 여부 — repo 경로가 있고 모드가 file|pr 일 때.

        always_gate 는 별개다: can_publish 여도 명시적 승인(approved) 없으면 실제로 안 쓴다.
        """
        return bool(self.repo_path) and self.mode in ("file", "pr")

    @property
    def can_create(self) -> bool:
        """tool_create('post') 라우팅용 — 실발행 가능 여부와 동일."""
        return self.can_publish

    # ── 변환 (SUMMARY → POST) ───────────────────────────────────
    @staticmethod
    def _slugify(text: str, fallback: str) -> str:
        """제목 → URL 슬러그(영문 소문자·하이픈). 한글 등으로 비면 fallback 사용."""
        s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
        if not s:
            s = re.sub(r"[^a-z0-9]+", "-", str(fallback).lower()).strip("-")
        return s or "post"

    @classmethod
    def summary_to_post(cls, summary: dict, *, status: str = "초안") -> dict:
        """SUMMARY dict → POST dict (post.schema 정합). status 기본 '초안'."""
        sid = str(summary.get("summary_id") or "").strip()
        title = str(summary.get("title") or "제목 없음")
        insights = summary.get("key_insights") or []
        body_parts = [f"# {title}", "", str(summary.get("summary") or "")]
        if insights:
            body_parts += ["", "## 핵심 인사이트"] + [f"- {i}" for i in insights]
        body_parts += ["", "---", f"> 출처 요약: `{sid or '미상'}` · 발행: Yohan Studio"]
        post: dict = {
            "post_id": f"post_{sid}" if sid else "post_untitled",
            "title": title,
            "slug": cls._slugify(title, sid or title),
            "body": "\n".join(body_parts),
            "status": status,
        }
        if sid:
            post["summary_id"] = sid  # published_as FK
        domain = summary.get("domain")
        if domain:
            post["tags"] = [domain]
        return post

    # ── frontmatter / MDX 렌더 ──────────────────────────────────
    def _frontmatter(self, summary: dict, post: dict, *, published: bool) -> dict:
        """SUMMARY/POST → yohan-studio frontmatter dict.

        필수: title, description, date(YYYY-MM-DD), tags(list[str]), category(str), published(bool).
        선택: thumbnail, relatedProjects(list[str]), source_urls(list[str]).
        """
        created = str(summary.get("created_at") or "")
        date = created[:10] if _DATE_RE.match(created[:10]) else _now_date()
        desc = re.sub(r"\s+", " ", str(summary.get("summary") or "")).strip()[:200]
        tags = post.get("tags") or ([summary["domain"]] if summary.get("domain") else [])
        fm: dict = {
            "title": post.get("title") or summary.get("title") or "제목 없음",
            "description": desc,
            "date": date,
            "tags": [str(t) for t in tags],
            "category": str(summary.get("domain") or "기타"),
            "published": bool(published),
        }
        if _nonempty(summary.get("cover_image")):
            fm["thumbnail"] = str(summary["cover_image"])
        if _nonempty(summary.get("relatedProjects")):
            fm["relatedProjects"] = [str(p) for p in summary["relatedProjects"]]
        # source_urls — 원본 RESOURCE 추적(cross-link). source_url 직접 or resource_id 표식.
        srcs = []
        if _nonempty(summary.get("source_url")):
            srcs.append(str(summary["source_url"]))
        if srcs:
            fm["source_urls"] = srcs
        return fm

    @staticmethod
    def _validate_frontmatter(fm: dict) -> tuple[bool, list[str]]:
        """frontmatter 필수 충족 + 형식 검사 → (valid, errors). schema_valid 매핑 근거."""
        errors: list[str] = []
        for k in _FM_REQUIRED:
            if k not in fm:
                errors.append(f"필수 누락: {k}")
        if _nonempty(fm.get("title")) is False:
            errors.append("title 비어있음")
        if _nonempty(fm.get("description")) is False:
            errors.append("description 비어있음")
        if not _DATE_RE.match(str(fm.get("date", ""))):
            errors.append("date 형식(YYYY-MM-DD) 위반")
        if not isinstance(fm.get("tags"), list) or not fm.get("tags"):
            errors.append("tags 는 비어있지 않은 list[str]")
        if _nonempty(fm.get("category")) is False:
            errors.append("category 비어있음")
        if not isinstance(fm.get("published"), bool):
            errors.append("published 는 bool")
        # cross-link 형식 — relatedProjects/source_urls 는 list[str]
        for k in ("relatedProjects", "source_urls"):
            if k in fm and (not isinstance(fm[k], list) or any(not isinstance(x, str) for x in fm[k])):
                errors.append(f"{k} 는 list[str] 형식")
        return (len(errors) == 0), errors

    @staticmethod
    def _render_mdx(fm: dict, body: str) -> str:
        """frontmatter dict + body → MDX 전문. frontmatter 는 JSON 스칼라(=유효 YAML)로 직렬화."""
        lines = ["---"]
        for k, v in fm.items():
            lines.append(f"{k}: {json.dumps(v, ensure_ascii=False)}")
        lines += ["---", "", body.rstrip("\n"), ""]
        return "\n".join(lines)

    # ── 경로 / slug / diff ──────────────────────────────────────
    def _target_path(self, slug: str) -> Path:
        """발행 타겟 경로. repo 있으면 절대경로, 없으면(드라이런 표시용) 상대경로."""
        if self.repo_path:
            return Path(self.repo_path) / self.subdir / f"{slug}.mdx"
        return Path(self.subdir) / f"{slug}.mdx"

    def _used_slugs(self, entries: list[dict]) -> set[str]:
        used = {e.get("slug") for e in entries if e.get("slug")}
        # 디스크 상의 기존 .mdx 도 충돌 후보로 본다(있으면)
        if self.repo_path:
            d = Path(self.repo_path) / self.subdir
            if d.exists():
                used |= {p.stem for p in d.glob("*.mdx")}
        return {s for s in used if s}

    def _next_slug(self, base: str, entries: list[dict]) -> str:
        """충돌 시 -2, -3 … 첫 빈 슬러그."""
        used = self._used_slugs(entries)
        if base not in used:
            return base
        i = 2
        while f"{base}-{i}" in used:
            i += 1
        return f"{base}-{i}"

    def _diff(self, target: Path, mdx: str) -> str:
        """타겟 파일 대비 unified diff(신규면 빈→전문)."""
        old = ""
        if target.exists():
            old = target.read_text(encoding="utf-8")
        rel = f"{self.subdir}/{target.name}"
        diff = difflib.unified_diff(
            old.splitlines(keepends=True), mdx.splitlines(keepends=True),
            fromfile=f"a/{rel}", tofile=f"b/{rel}",
        )
        return "".join(diff)

    # ── 발행 ────────────────────────────────────────────────────
    async def publish(self, summary: dict, *, approved: bool = False) -> dict:
        """SUMMARY → MDX 렌더 후 발행(드라이런/실파일/PR).

        반환(상위호환 — 기존 키 유지):
            {"post", "published", "dry_run", "detail",
             "mode", "slug", "target_path", "mdx", "frontmatter", "frontmatter_valid",
             "diff", "content_hash", "already_published", "contradiction", "errors", "pr"?}

        always_gate: mode 가 file|pr 라도 approved=False 면 실제로 쓰지 않고 드라이런 폴백.
        """
        # 실발행 의사 = 모드 file|pr + 명시 승인 + repo 경로 존재
        will_publish = self.mode in ("file", "pr") and approved and bool(self.repo_path)
        post = self.summary_to_post(summary, status=("발행" if will_publish else "초안"))
        base_slug = post["slug"]

        fm = self._frontmatter(summary, post, published=will_publish)
        fm_valid, fm_errors = self._validate_frontmatter(fm)
        mdx = self._render_mdx(fm, post["body"])
        content_hash = hashlib.sha256(mdx.encode("utf-8")).hexdigest()[:16]

        entries = self._journal.all()
        same_slug = [e for e in entries if e.get("slug") == base_slug]
        already = any(e.get("content_hash") == content_hash for e in same_slug)
        contradiction = (not already) and any(e.get("content_hash") != content_hash for e in same_slug)

        slug = base_slug if not contradiction else self._next_slug(base_slug, entries)
        if slug != base_slug:
            post = {**post, "slug": slug, "post_id": post.get("post_id")}
        target = self._target_path(slug)
        diff = self._diff(target, mdx)

        base = {
            "post": post, "mode": self.mode, "slug": slug, "target_path": str(target),
            "mdx": mdx, "frontmatter": fm, "frontmatter_valid": fm_valid,
            "frontmatter_errors": fm_errors, "diff": diff, "content_hash": content_hash,
            "already_published": already, "contradiction": contradiction,
        }

        # 멱등 — 같은 slug+hash 이미 발행 → no-op
        if already:
            return {**base, "published": False, "dry_run": True,
                    "detail": f"이미 발행됨(동일 content-hash) — no-op: {slug}.mdx"}

        # always_gate / 폴백 사유
        want_real = self.mode in ("file", "pr")
        if not want_real:
            return {**base, "published": False, "dry_run": True,
                    "detail": "드라이런 — 파일 미작성(STUDIO_PUBLISH_MODE=dry_run)"}
        if not self.repo_path:
            return {**base, "published": False, "dry_run": True,
                    "detail": "STUDIO_REPO_PATH 미설정 — 드라이런 폴백"}
        if not approved:
            return {**base, "published": False, "dry_run": True,
                    "detail": f"always_gate: 미승인 — 실발행({self.mode}) 차단, 드라이런 폴백"}

        # 실발행 — 예외로 죽지 않고 드라이런 폴백
        try:
            if self.mode == "file":
                await asyncio.to_thread(self._write_file, target, mdx)
                self._journal.record({
                    "slug": slug, "content_hash": content_hash, "target_path": str(target),
                    "mode": "file", "summary_id": post.get("summary_id"),
                })
                return {**base, "published": True, "dry_run": False,
                        "detail": f"MDX 파일 발행 완료: {target}"}
            # pr 모드
            pr = await asyncio.to_thread(self._publish_pr, slug, target, mdx)
            self._journal.record({
                "slug": slug, "content_hash": content_hash, "target_path": str(target),
                "mode": "pr", "summary_id": post.get("summary_id"), "branch": pr.get("branch"),
            })
            return {**base, "published": True, "dry_run": False,
                    "detail": f"PR 발행: {pr.get('branch')}", "pr": pr}
        except Exception as exc:
            return {**base, "published": False, "dry_run": True,
                    "detail": f"실발행 실패 → 드라이런 폴백: {type(exc).__name__}: {exc}",
                    "errors": [f"{type(exc).__name__}: {exc}"]}

    def _write_file(self, target: Path, mdx: str) -> str:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(mdx, encoding="utf-8")
        return str(target)

    def _git(self, *args: str) -> subprocess.CompletedProcess:
        """git 서브프로세스 — cwd=repo_path. 실패 시 CalledProcessError 로 상위에서 폴백 처리."""
        return subprocess.run(
            ["git", *args], cwd=self.repo_path,
            capture_output=True, text=True, check=True,
        )

    def _publish_pr(self, slug: str, target: Path, mdx: str) -> dict:
        """git 새 브랜치 → 파일 쓰기 → add/commit/push → PR(base=base_branch). master 직푸시 금지."""
        branch = f"publish/{slug}"
        # 새 브랜치(존재하면 체크아웃) — 절대 base_branch 에 직접 쓰지 않는다
        try:
            self._git("checkout", "-b", branch)
        except subprocess.CalledProcessError:
            self._git("checkout", branch)
        self._write_file(target, mdx)
        rel = str(target.relative_to(self.repo_path)) if self.repo_path else str(target)
        self._git("add", rel)
        self._git("commit", "-m", f"publish: {slug}")
        pushed = False
        pr_url = None
        try:
            self._git("push", "-u", "origin", branch)
            pushed = True
        except subprocess.CalledProcessError as exc:
            # 원격 없음 등 — 브랜치/커밋은 남기고 push 만 실패로 보고(죽지 않음)
            pr_url = f"push 실패: {exc.stderr or exc}"
        if pushed:
            try:  # gh 있으면 PR 생성(없으면 브랜치만 push)
                res = subprocess.run(
                    ["gh", "pr", "create", "--base", self.base_branch, "--head", branch,
                     "--title", f"publish: {slug}", "--body", "yohan-mcp 자동 발행 초안"],
                    cwd=self.repo_path, capture_output=True, text=True, check=True,
                )
                pr_url = (res.stdout or "").strip() or pr_url
            except (FileNotFoundError, subprocess.CalledProcessError):
                pr_url = pr_url or "gh 미설치/실패 — 브랜치 push 만 완료"
        return {"branch": branch, "base": self.base_branch, "pushed": pushed, "pr_url": pr_url}

    # ── Adapter 계약 ────────────────────────────────────────────
    async def search(self, query: str, opts: dict | None = None) -> list[dict]:
        raise NotImplementedError(f"studio.search — {_PHASE} 미지원")

    async def create(self, type_: str, data: dict) -> dict:
        """type_='post' 이면 완성된 POST 를 레코드화(발행은 publish 경로). 그 외는 미지원."""
        if type_ != "post":
            raise NotImplementedError(f"studio.create({type_}) — post 만 지원")
        return make_record(str(data.get("post_id") or "post"), "post", self.name, dict(data))

    async def update(self, id_: str, data: dict, type_: str | None = None) -> dict:
        raise NotImplementedError(f"studio.update — {_PHASE} 미지원")

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def health_check(self) -> dict:
        """연결 점검 — STUDIO_API_URL 의 /health(있으면). 발행과 무관한 가용성 probe."""
        with _Timer() as t:
            if not self.url:
                return health(False, t.elapsed_ms, "STUDIO_API_URL 미설정")
            try:
                resp = await self._get_client().get("/health")
                ok = resp.status_code == 200
                key = "KEY 있음" if self.api_key else "KEY 없음"
                detail = f"Studio OK ({key})" if ok else f"HTTP {resp.status_code}"
            except Exception as exc:
                ok, detail = False, f"Studio 연결 실패: {type(exc).__name__}: {exc}"
        return health(ok, t.elapsed_ms, detail)
