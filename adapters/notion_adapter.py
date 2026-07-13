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

# 역방향 회수(PHASE 3) — Dev Log 에서 끌어올 유형(과거 맥락)
_DEVLOG_TYPES = ("세션로그", "상태요약", "핸드오프", "ADR")

_SELECT_FIELDS = {"status", "domain", "category", "difficulty", "work_type", "result", "resource_type"}
_MULTI_FIELDS = {"tags", "key_insights", "related_terms", "alternatives"}
_URL_FIELDS = {"source_url", "url", "cover_image"}
_NUMBER_FIELDS = {"confidence", "price"}

# 실 Notion DB 프로퍼티 보정 — 스키마 필드명(영문)과 실DB 프로퍼티명(한국어)이 다르다.
# 값: (실 프로퍼티명, notion 타입). 프로퍼티명 None 은 페이지 프로퍼티로 보내지 않음
# ("children" = 본문 블록으로 적재, "skip" = 미전송 — 멱등키는 CreateStore 가 로컬 보장).
# 여기 없는 타입/필드는 기존 휴리스틱(필드명 = 프로퍼티명) 폴백.
_PROP_OVERRIDES: dict[str, dict[str, tuple[str | None, str]]] = {
    "resource": {
        "title": ("이름", "title"),
        "source_url": ("원본 URL", "url"),
        "resource_type": ("유형", "select"),
        "domain": ("카테고리", "select"),
        "status": ("상태", "status"),
        "captured_at": ("수집일", "date"),
        "raw_content": (None, "children"),
        "resource_id": (None, "skip"),
    },
}
# 스키마 enum → 실DB 옵션 번역(확실한 대응만). Notion status 타입은 select 와 달리
# 미존재 옵션을 자동 생성하지 못해 미번역 값은 API 400 으로 명시 실패한다(무음 오기록 방지).
_VALUE_MAP: dict[tuple[str, str], dict[str, str]] = {
    ("resource", "status"): {"신규": "미처리", "완료": "요약완료"},
    ("resource", "domain"): {"기타": "일반", "AI": "AI/자동화"},
}


class NotionAdapter(BackendAdapter):
    name = "notion"

    def __init__(self, client: httpx.AsyncClient | None = None, token: str | None = None) -> None:
        self.token = token if token is not None else os.getenv("NOTION_TOKEN")
        self._client = client  # 주입 시 그대로, 없으면 lazy 생성
        self._owns_client = client is None  # 자가 생성분만 aclose 대상
        self.db_ids = {t: os.getenv(env) for t, env in _DB_ENV.items()}
        # 역방향 회수(PHASE 3) — 엔티티 5종과 별개인 Dev Log/패턴 DB
        self.devlog_db_id = os.getenv("NOTION_DEVLOG_DB_ID")
        self.pattern_db_id = os.getenv("NOTION_PATTERN_DB_ID")

    @property
    def can_create(self) -> bool:
        """실생성 가능 여부 — 토큰이 있어야 한다(없으면 tool_create 가 드라이런 폴백)."""
        return bool(self.token)

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
        over = _PROP_OVERRIDES.get(type_, {})
        props: dict = {}
        for key, val in data.items():
            if val is None:
                continue
            if key in over:
                name, ptype = over[key]
                if name is None:  # children/skip — 페이지 프로퍼티 아님
                    continue
                v = _VALUE_MAP.get((type_, key), {}).get(str(val), val)
                if ptype == "title":
                    props[name] = {"title": [{"text": {"content": str(v)}}]}
                elif ptype == "select":
                    props[name] = {"select": {"name": str(v)}}
                elif ptype == "status":
                    props[name] = {"status": {"name": str(v)}}
                elif ptype == "url":
                    props[name] = {"url": str(v)}
                elif ptype == "date":
                    props[name] = {"date": {"start": str(v)}}
                elif ptype == "number":
                    props[name] = {"number": v}
                else:
                    props[name] = {"rich_text": [{"text": {"content": str(v)}}]}
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

    def _to_notion_children(self, type_: str, data: dict) -> list[dict]:
        """본문 블록 대상 필드(raw_content 등) → paragraph 블록. rich_text 2000자 제한 분할."""
        blocks: list[dict] = []
        for key, (name, ptype) in _PROP_OVERRIDES.get(type_, {}).items():
            if ptype != "children" or not data.get(key):
                continue
            text = str(data[key])
            for i in range(0, len(text), 1900):
                blocks.append({
                    "object": "block", "type": "paragraph",
                    "paragraph": {"rich_text": [{"text": {"content": text[i:i + 1900]}}]},
                })
        return blocks

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
        # 매칭 상한(있으면 조기 종료). 없으면 DB 전체를 스캔 — 10행 컷으로 초과분이
        # 조용히 회수 0 이 되던 결함 방지(summary_id 해소도 전체 스캔에 의존).
        limit = int(opts.get("limit") or 0)
        records: list[dict] = []
        for type_ in types:
            db_id = self.db_ids.get(type_)
            if not db_id:
                continue
            cursor = None
            while True:
                body: dict = {"page_size": 100}
                if cursor:
                    body["start_cursor"] = cursor
                resp = await client.post(f"/databases/{db_id}/query", json=body)
                resp.raise_for_status()
                j = resp.json()
                for page in j.get("results", []):
                    data = self._from_notion_page(type_, page)
                    blob = " ".join(str(v) for v in data.values()).lower()
                    if q and q not in blob:
                        continue
                    rid = str(data.get(_ID_FIELD.get(type_, "id"), page.get("id", "")))
                    records.append(make_record(rid, type_, self.name, data))
                if limit and len(records) >= limit:
                    return records[:limit]
                if j.get("has_more") and j.get("next_cursor"):
                    cursor = j["next_cursor"]
                else:
                    break
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

    # ── 역방향 회수 (PHASE 3) — Dev Log / 패턴 DB 쿼리 ──────────
    async def devlog_query(self, project_name: str | None, limit: int = 10) -> list[dict]:
        """Dev Log DB 에서 프로젝트별 최근 행 조회(과거 맥락 회수).

        프로젝트=project_name, 유형 IN(세션로그/상태요약/핸드오프/ADR), 실행일 DESC, LIMIT.
        토큰/DB ID/프로젝트명 없으면 [] (에러 아님 — graceful).
        반환 필드: {이름, 유형, 교훈, 관련 파일, SoT Key}.
        """
        if not self.token or not self.devlog_db_id or not project_name:
            return []
        client = self._get_client()
        body = {
            "filter": {"and": [
                {"property": "프로젝트", "select": {"equals": project_name}},
                {"or": [{"property": "유형", "select": {"equals": t}} for t in _DEVLOG_TYPES]},
            ]},
            "sorts": [{"property": "실행일", "direction": "descending"}],
            "page_size": limit,
        }
        resp = await client.post(f"/databases/{self.devlog_db_id}/query", json=body)
        resp.raise_for_status()
        out: list[dict] = []
        for page in resp.json().get("results", []):
            d = self._from_notion_page("devlog", page)
            out.append({k: d.get(k) for k in ("이름", "유형", "교훈", "관련 파일", "SoT Key")})
        return out

    async def pattern_query(
        self, category: str | None = None, tags: list[str] | None = None, limit: int = 10
    ) -> list[dict]:
        """패턴 DB 에서 재사용 가능한 패턴 조회.

        카테고리=category(있으면) + 태그 contains any of tags(있으면), 발견일 DESC, LIMIT.
        필터 둘 다 없으면 최근순. 토큰/DB ID 없으면 [] (graceful).
        반환 필드: {패턴명, 증상, 원인, 해결, 적용 조건}.
        """
        if not self.token or not self.pattern_db_id:
            return []
        client = self._get_client()
        conds: list[dict] = []
        if category:
            conds.append({"property": "카테고리", "select": {"equals": category}})
        if tags:
            conds.append({"or": [{"property": "태그", "multi_select": {"contains": t}} for t in tags]})
        body: dict = {"sorts": [{"property": "발견일", "direction": "descending"}], "page_size": limit}
        if len(conds) == 1:
            body["filter"] = conds[0]
        elif conds:
            body["filter"] = {"and": conds}
        resp = await client.post(f"/databases/{self.pattern_db_id}/query", json=body)
        resp.raise_for_status()
        out: list[dict] = []
        for page in resp.json().get("results", []):
            d = self._from_notion_page("pattern", page)
            out.append({k: d.get(k) for k in ("패턴명", "증상", "원인", "해결", "적용 조건")})
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
        children = self._to_notion_children(type_, data)
        if children:
            payload["children"] = children
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
