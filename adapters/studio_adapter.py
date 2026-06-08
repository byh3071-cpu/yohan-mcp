# -*- coding: utf-8 -*-
"""Studio 백엔드 어댑터 (P3 실동작).

SUMMARY → 블로그 POST 변환 + 발행. `_links.json` 의 published_as(1:N) 경로.
- env STUDIO_API_URL + STUDIO_API_KEY 둘 다 있으면 실발행(POST /posts).
- 하나라도 없으면 드라이런: 변환 결과(POST dict)만 돌려주고 '발행 보류' 플래그.
  실엔드포인트 확정 전 안전 기본값(코드 변경 없이 .env 만 채우면 실발행 전환).
- 변환 결과는 P1 schemas/studio/post.schema.json 에 정합.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone

import httpx

from adapters.base import BackendAdapter, _Timer, health, make_record

_PHASE = "P3"
_KST = timezone(timedelta(hours=9))

# TODO(P3+): 실 Studio 발행 엔드포인트/페이로드 스펙 확정 시 _send() 의 path·필드 매핑 보정.
_PUBLISH_PATH = "/posts"


def _now_iso() -> str:
    return datetime.now(_KST).isoformat(timespec="seconds")


class StudioAdapter(BackendAdapter):
    name = "studio"

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        url: str | None = None,
        api_key: str | None = None,
    ) -> None:
        self.url = url if url is not None else os.getenv("STUDIO_API_URL")
        self.api_key = api_key if api_key is not None else os.getenv("STUDIO_API_KEY")
        self._client = client
        self._owns_client = client is None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
            self._client = httpx.AsyncClient(base_url=self.url or "", headers=headers, timeout=10.0)
        return self._client

    @property
    def can_publish(self) -> bool:
        """실발행 가능 여부 — URL+KEY 둘 다 있어야 한다(아니면 드라이런)."""
        return bool(self.url and self.api_key)

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

    # ── 발행 ────────────────────────────────────────────────────
    async def publish(self, summary: dict) -> dict:
        """SUMMARY → POST 변환 후 발행(실/드라이런).

        반환: {"post": <post dict>, "published": bool, "dry_run": bool, "detail": str}
        """
        if not self.can_publish:
            post = self.summary_to_post(summary, status="초안")
            return {
                "post": post, "published": False, "dry_run": True,
                "detail": "STUDIO_API_URL/STUDIO_API_KEY 미설정 — 발행 보류(드라이런)",
            }
        post = self.summary_to_post(summary, status="발행")
        sent = await self._send(post)
        return {"post": sent, "published": True, "dry_run": False, "detail": "Studio 발행 완료"}

    async def _send(self, post: dict) -> dict:
        """실 Studio API 로 POST 전송. 응답 id 가 오면 post_id 보정."""
        post = dict(post)
        post.setdefault("status", "발행")
        post.setdefault("published_at", _now_iso())
        resp = await self._get_client().post(_PUBLISH_PATH, json=post)
        resp.raise_for_status()
        try:
            body = resp.json()
        except Exception:
            body = {}
        api_id = body.get("id") or body.get("post_id")
        if api_id:
            post["post_id"] = str(api_id)
        return post

    # ── Adapter 계약 ────────────────────────────────────────────
    async def search(self, query: str, opts: dict | None = None) -> list[dict]:
        raise NotImplementedError(f"studio.search — {_PHASE} 미지원")

    async def create(self, type_: str, data: dict) -> dict:
        """type_='post' 이면 이미 완성된 POST 를 발행(실/드라이런). 그 외는 미지원."""
        if type_ != "post":
            raise NotImplementedError(f"studio.create({type_}) — post 만 지원")
        if self.can_publish:
            sent = await self._send(dict(data))
            return make_record(str(sent.get("post_id") or "post"), "post", self.name, sent)
        # 드라이런 — 발행하지 않고 변환/입력 그대로 레코드화
        return make_record(str(data.get("post_id") or "post"), "post", self.name, dict(data))

    async def update(self, id_: str, data: dict, type_: str | None = None) -> dict:
        raise NotImplementedError(f"studio.update — {_PHASE} 미지원")

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def health_check(self) -> dict:
        with _Timer() as t:
            if not self.url:
                return health(False, t.elapsed_ms, "STUDIO_API_URL 미설정")
            try:
                resp = await self._get_client().get("/health")
                ok = resp.status_code == 200
                key = "KEY 있음" if self.api_key else "KEY 없음(드라이런)"
                detail = f"Studio OK ({key})" if ok else f"HTTP {resp.status_code}"
            except Exception as exc:
                ok, detail = False, f"Studio 연결 실패: {type(exc).__name__}: {exc}"
        return health(ok, t.elapsed_ms, detail)
