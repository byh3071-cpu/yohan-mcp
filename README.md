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

## 다음 단계 — P2: Adapter

타입 시스템 위에 **백엔드별 Adapter 계층**을 올린다. 각 Adapter 는 `schemas/` 를 단일 출처로 삼아 (1) Notion DB ↔ 스키마 매핑, (2) memory 파일(yaml/md) ↔ 스키마 직렬화, (3) Qdrant 임베딩 동기화를 담당하고, `_links.json` 을 읽어 백엔드 간 전파(예: SUMMARY 생성 → POST 발행 후보 큐잉)를 수행한다. 코드 로직은 P2 에서 시작한다.
