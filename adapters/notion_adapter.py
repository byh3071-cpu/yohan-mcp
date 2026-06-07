# -*- coding: utf-8 -*-
"""Notion 백엔드 어댑터 (P2 실동작).

Notion API v1 (https://api.notion.com/v1) 를 httpx 로 호출.
- 토큰: env NOTION_TOKEN
- DB ID: env NOTION_<TYPE>_DB_ID (예: NOTION_RESOURCE_DB_ID)
- 테스트를 위해 httpx.AsyncClient 를 주입 가능(client 인자).

Notion property 매핑은 필드명 휴리스틱 기반의 best-effort 이며,
실제 DB 스키마에 맞춰 점진 보정한다(P2.5+).
"""
from __future__ import annotations

import os

import httpx

from adapters.base import BackendAdapter, _Timer, health, make_record

API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

# 타입 → DB ID 환경변수 이름
_DB_ENV = {
    "resource": "NOTION_RESOURCE_DB_ID",
    "summary": "NOTION_SUMMARY_DB_ID",
    "triple": "NOTION_TRIPLE_DB_ID",
    "ai-dict": "NOTION_AIDICT_DB_ID",
    "execution-log": "NOTION_EXECLOG_DB_ID",
}
# 타입 → Notion title 컬럼에 들어갈 필드
_TITLE_FIELD = {
    "resource": "title", "summary": "title", "triple": "subject",
    "ai-dict": "name", "execution-log": "content",
}
# 타입별 PK 필드(반환 레코드 id)
_ID_FIELD = {
    "resource": "resource_id", "summary": "summary_id", "triple": "triple_id",
    "ai-dict": "term_id", "execution-log": "log_id",
}

_SELECT_FIELDS = {"status", "domain", "category", "difficulty", "work_type", "result", "resource_type"}
_MULTI_FIELDS = {"tags", "key_insights", "related_terms", "alternatives"}
_URL_FIELDS = {"source_url", "url", "cover_image"}
_NUMBER_FIELDS = {"confidence", "price"}


class NotionAdapter(BackendAdapter):
    name = "notion"

    def __init__(self, client: httpx.AsyncClient | None = None, token: str | None = None) -> None:
        self.token = token if token is not None else os.getenv("NOTION_TOKEN")
        self._client = client  # 주입 시 그대로, 없으면 lazy 생성
        self._owns_client = client is None  # 자가 생성분만 aclose 대상
        self.db_ids = {t: os.getenv(env) for t, env in _DB_ENV.items()}

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=API_BASE,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Notion-Version": NOTION_VERSION,
                    "Content-Type": "application/json",
                },
                timeout=15.0,
            )
        return self._client

    # ── property 매핑 ──────────────────────────────────────────
    def _to_notion_properties(self, type_: str, data: dict) -> dict:
        title_field = _TITLE_FIELD.get(type_, "title")
        props: dict = {}
        for key, val in data.items():
            if val is None:
                continue
            if key == title_field:
                props[key] = {"title": [{"text": {"content": str(val)}}]}
            elif key in _SELECT_FIELDS:
                props[key] = {"select": {"name": str(val)}}
            elif key in _MULTI_FIELDS and isinstance(val, list):
                props[key] = {"multi_select": [{"name": str(v)} for v in val]}
            elif key in _URL_FIELDS:
                props[key] = {"url": str(val)}
            elif key in _NUMBER_FIELDS and isinstance(val, (int, float)):
                props[key] = {"number": val}
            elif key.endswith("_at"):
                props[key] = {"date": {"start": str(val)}}
            else:
                props[key] = {"rich_text": [{"text": {"content": str(val)}}]}
        return props

    def _from_notion_page(self, type_: str, page: dict) -> dict:
        """Notion page → 평문 data dict (best-effort)."""
        out: dict = {}
        for key, prop in (page.get("properties") or {}).items():
            ptype = prop.get("type")
            if ptype == "title":
                out[key] = "".join(t.get("plain_text", t.get("text", {}).get("content", "")) for t in prop["title"])
            elif ptype == "rich_text":
                out[key] = "".join(t.get("plain_text", t.get("text", {}).get("content", "")) for t in prop["rich_text"])
            elif ptype == "select":
                out[key] = (prop.get("select") or {}).get("name")
            elif ptype == "multi_select":
                out[key] = [o.get("name") for o in prop.get("multi_select", [])]
            elif ptype == "number":
                out[key] = prop.get("number")
            elif ptype == "url":
                out[key] = prop.get("url")
            elif ptype == "date":
                out[key] = (prop.get("date") or {}).get("start")
        return out

    # ── 검색 ────────────────────────────────────────────────────
    async def search(self, query: str, opts: dict | None = None) -> list[dict]:
        if not self.token:
            return []  # 토큰 없으면 graceful skip
        opts = opts or {}
        types = [opts["type"]] if opts.get("type") else list(_DB_ENV.keys())
        client = self._get_client()
        q = (query or "").lower()
        records: list[dict] = []
        for type_ in types:
            db_id = self.db_ids.get(type_)
            if not db_id:
                continue
            resp = await client.post(f"/databases/{db_id}/query", json={"page_size": 10})
            resp.raise_for_status()
            for page in resp.json().get("results", []):
                data = self._from_notion_page(type_, page)
                blob = " ".join(str(v) for v in data.values()).lower()
                if q and q not in blob:
                    continue
                rid = str(data.get(_ID_FIELD.get(type_, "id"), page.get("id", "")))
                records.append(make_record(rid, type_, self.name, data))
        return records

    async def fetch_all(self, type_: str, limit: int | None = None) -> list[dict]:
        """DB 전체 페이지를 커서 페이지네이션으로 로드(시딩용). 토큰/DB 없으면 []."""
        if not self.token:
            return []
        db_id = self.db_ids.get(type_)
        if not db_id:
            return []
        client = self._get_client()
        out: list[dict] = []
        cursor = None
        while True:
            body: dict = {"page_size": 100}
            if cursor:
                body["start_cursor"] = cursor
            resp = await client.post(f"/databases/{db_id}/query", json=body)
            resp.raise_for_status()
            j = resp.json()
            for page in j.get("results", []):
                out.append(self._from_notion_page(type_, page))
                if limit and len(out) >= limit:
                    return out[:limit]
            if j.get("has_more") and j.get("next_cursor"):
                cursor = j["next_cursor"]
            else:
                break
        return out

    # ── 생성/갱신 ───────────────────────────────────────────────
    async def create(self, type_: str, data: dict) -> dict:
        if not self.token:
            raise RuntimeError("NOTION_TOKEN 미설정 — create 불가")
        db_id = self.db_ids.get(type_)
        if not db_id:
            raise ValueError(f"{type_} DB ID 미설정({_DB_ENV.get(type_)})")
        client = self._get_client()
        payload = {"parent": {"database_id": db_id}, "properties": self._to_notion_properties(type_, data)}
        resp = await client.post("/pages", json=payload)
        resp.raise_for_status()
        page = resp.json()
        rid = str(data.get(_ID_FIELD.get(type_, "id"), page.get("id", "")))
        return make_record(rid, type_, self.name, data)

    async def update(self, id_: str, data: dict, type_: str | None = None) -> dict:
        if not self.token:
            raise RuntimeError("NOTION_TOKEN 미설정 — update 불가")
        # 검증에 쓰인 type_ 우선, 없으면 추론 (검증 타입 = 매핑 타입)
        t = type_ or (data or {}).get("_type") or self._guess_type(data)
        # 외부 입력 id_ 의 URL 경로 주입 차단
        if any(c in str(id_) for c in "/\\") or ".." in str(id_):
            raise ValueError(f"잘못된 Notion 페이지 ID: {id_!r}")
        client = self._get_client()
        resp = await client.patch(f"/pages/{id_}", json={"properties": self._to_notion_properties(t, data)})
        resp.raise_for_status()
        return make_record(str(id_), t, self.name, data)

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    def _guess_type(self, data: dict) -> str:
        for type_, idf in _ID_FIELD.items():
            if idf in data:
                return type_
        return "resource"

    # ── health ──────────────────────────────────────────────────
    async def health_check(self) -> dict:
        with _Timer() as t:
            if not self.token:
                return health(False, t.elapsed_ms, "NOTION_TOKEN 미설정")
            try:
                resp = await self._get_client().get("/users/me")
                ok = resp.status_code == 200
                detail = "Notion API 인증 OK" if ok else f"HTTP {resp.status_code}"
            except Exception as exc:
                ok, detail = False, f"Notion 연결 실패: {type(exc).__name__}: {exc}"
        return health(ok, t.elapsed_ms, detail)
