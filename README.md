# yohan-mcp v2

> "팔란티어 뼈대 × SW 3.0 피부" — 5개 백엔드(Notion·memory·Qdrant·Studio·n8n)를 하나의 타입 시스템으로 묶는 통합 MCP.

이 `schemas/`는 **VHK CLI 와 yohan-mcp v2 가 공유하는 코어**다. 다른 프로젝트에서 상대경로(`..\vhk` 등)로 참조할 수 있도록 자기완결적(self-contained)으로 구성했다.

## P1 산출물 (타입 시스템 골격)

P1 목표는 코드 로직 없이 **타입 시스템(`schemas/`) + 크로스 백엔드 관계(`_links.json`)** 골격을 세우는 것. 모든 스키마는 JSON Schema **draft 2020-12** 이며, SW 3.0 원칙에 따라 **에이전트가 스키마만 읽고 데이터를 생성**할 수 있도록 모든 필드에 한국어 `description` 과 `examples` 를 갖는다.

- **스키마 11종** — Notion 5 + memory 3 + Studio 2 + 공유 enum 1
- **크로스 백엔드 관계 7종** — `_links.json` (source/target/relation/cardinality/description)
- **검증 스크립트** — `scripts/validate_schemas.py` 한 줄로 전부 통과

## 디렉토리 구조

```
yohan-mcp/
├─ schemas/
│  ├─ _shared-enums.json          # 백엔드 공유 enum ($defs: Status·Domain·ResourceType·Confidence·Difficulty·DictStatus·WorkType·ResultStatus·DecisionStatus·PostStatus·ProductStatus)
│  ├─ _links.json                 # 크로스 백엔드 관계 7종
│  ├─ notion/
│  │  ├─ resource.schema.json     # RESOURCE DB — 원본 자료
│  │  ├─ summary.schema.json      # SUMMARY DB — 요약/인사이트
│  │  ├─ triple.schema.json       # 지식 트리플 맵 (S-R-O)
│  │  ├─ ai-dict.schema.json      # AI 사전
│  │  └─ execution-log.schema.json# EXECUTION LOG — 작업 실행 로그
│  ├─ memory/
│  │  ├─ profile.schema.json      # profile.yaml — 사용자 프로필
│  │  ├─ decision.schema.json     # decisions/ — 의사결정 기록
│  │  └─ ingest.schema.json       # ingest/ — 수집 원본
│  └─ studio/
│     ├─ post.schema.json         # 블로그 포스트
│     └─ product.schema.json      # 제품
└─ scripts/
   └─ validate_schemas.py         # 메타스키마·문서·링크 정합성 검증기
```

## 크로스 백엔드 관계 (`_links.json`)

노드 형식 `<backend>:<entity>` (`notion:*` 는 와일드카드).

| source | target | relation | cardinality |
| --- | --- | --- | --- |
| notion:resource | notion:summary | summarized_by | 1:N |
| notion:summary | memory:decision | triggers_decision | 1:N |
| notion:resource | qdrant:embedding | has_vector | 1:1 |
| notion:summary | studio:post | published_as | 1:N |
| notion:summary | notion:ai-dict | promoted_to | 1:N |
| notion:triple | notion:* | connects | N:N |
| memory:ingest | notion:resource | ingested_from | 1:1 |

> `qdrant`·`n8n` 은 외부 백엔드로 P1 에선 스키마 파일을 두지 않는다(검증기가 허용).

## 검증

```powershell
python scripts/validate_schemas.py
```

검사 항목: ① 모든 `*.schema.json` 이 draft 2020-12 메타스키마로 유효 ② 최상위 `title`·`description` 및 모든 property 의 `description`·`examples` 완비 ③ 공유 enum `$defs` 문서화 ④ `_links.json` 의 source/target 노드가 실재 스키마(또는 허용된 외부 백엔드)를 가리킴.

---

# P2 — Adapter 5 + Smart Router + 의도 기반 도구 10 (완료)

타입 시스템을 **실제로 움직이게** 하는 런타임 계층. 팔란티어 뼈대(Adapter·라우팅·스키마 검증) × SW 3.0 피부(의도 기반 도구·검증 메타).

## P2 산출물

- **공통 인터페이스** `adapters/base.py` — `BackendAdapter` ABC (`search`/`create`/`update`/`health_check`) + 표준 레코드/health 헬퍼.
- **Adapter 5종** `adapters/`
  - `notion_adapter.py` — Notion API v1 **실동작**(search/create/update, httpx 주입 가능).
  - `memory_adapter.py` — 로컬 `memory/`(yaml) **실동작**(profile/decision/ingest CRUD).
  - `qdrant_adapter.py` / `studio_adapter.py` / `n8n_adapter.py` — health_check 실동작, search/create 는 `NotImplementedError` stub(P2.5/P3/P4).
- **Smart Router** `core/router.py` — query→백엔드 선택→병렬 search→**RRF(k=60)** 융합. 백엔드 장애·미구현은 격리해 전체 검색을 깨지 않음.
- **Schema Validator** `core/schema_validator.py` — P1 `schemas/` 로딩(상대 `$ref` → `referencing` Registry 해소) + `validate`/`validate_partial`/`backend_of`. `FormatChecker` 로 `date-time`·`uri` format 까지 실검증(rfc3339/rfc3986-validator).
- **강건성(적대적 리뷰 반영)** — memory id 경로 탈출 차단(`_safe_id` + base 봉쇄), 도구 예외는 모두 `{errors}` 봉투로 격리(MCP 크래시 방지), RRF 융합키는 `타입::id`(서로 다른 엔티티 오융합 방지), httpx 클라이언트는 lifespan 에서 정리.
- **의도 기반 도구 10개** `core/tools.py`(로직) + `server.py`(MCP 등록): `search`·`create`·`update`·`get_context`·`status` 실동작, `run_action`·`publish`·`ingest`·`plan`·`check` 중 `check` 는 검증 실동작, 나머지는 P3/P4 stub. 모든 응답은 `{ data, verification:{schema_valid}, provenance:{sources_used} }` 봉투.

## 실행

```powershell
# 1) 의존성
pip install -r requirements.txt
# 2) 시크릿 (.env 는 .gitignore 제외됨)
copy .env.example .env   # NOTION_TOKEN, DB ID 등 채움
# 3) MCP 서버 (stdio)
python server.py
# 4) 테스트
python -m pytest -q
```

> `.env` 없이도 서버는 뜬다 — Notion/Qdrant/Studio/n8n 은 `status` 에서 "미설정/FAIL" 로 표시되고 memory 만 즉시 실동작한다.

## 다음 단계 — P2.5: Qdrant 시딩

`qdrant_adapter` 의 `search`/`create` 를 구현한다. P1 `schemas/notion/resource` ↔ `_links.json` 의 `has_vector(1:1)` 관계대로 RESOURCE 본문을 임베딩해 Qdrant 컬렉션에 적재하고, Router 의 `select_backends` 에 의미유사도 경로(qdrant)를 활성화한다. 이후 RRF 가 키워드(Notion)+의미(Qdrant)+파일(memory)을 진짜 하이브리드로 융합한다.
