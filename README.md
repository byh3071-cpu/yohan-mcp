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

---

# P2.5 — Qdrant 시딩 + ingest 파이프라인 + Headroom 연결 (완료)

"Ship first, infra through shipping" — 서빙(도구)보다 시딩(데이터)이 먼저. Qdrant 에 벡터가 있어야 의미검색이 의미를 가진다.

## P2.5 산출물

- **QdrantAdapter 실동작** `adapters/qdrant_adapter.py` — `create`(벡터+payload upsert), `search`(쿼리 임베딩→top-k 유사도), `health_check`(컬렉션·포인트 수). `point_id = uuid5(resource_url)` 로 **재적재 멱등**. `QDRANT_URL` 없으면 임베디드(`:memory:`) 폴백.
- **임베딩 추상화** `core/embeddings.py` — `EMBEDDING_BACKEND`: `auto`(local→hash) / `local`(sentence-transformers, 한국어 OK) / `openai`(text-embedding-3-small) / `hash`(의존성 0 결정적 폴백, dim 384=MiniLM 호환). torch 없이도 파이프라인 동작.
- **시딩 스크립트** `scripts/seed_qdrant.py` — Notion RESOURCE 전체(커서 페이지네이션) → 임베딩 → Qdrant upsert. `--limit N`, 배치 진행률, 실패 스킵. 멱등.
- **ingest 도구 실동작** `core/tools.py` — `ingest(url)`: 본문 추출(stdlib HTML 파서) → ① Notion RESOURCE ② Qdrant 벡터 ③ memory ingest 로그(`ingested_from` 1:1). 백엔드별 격리.
- **search 3중 융합** — `select_backends` 에 qdrant 활성화 → **Notion(키워드)+memory(파일)+Qdrant(의미)** 3중 RRF.
- **인프라** `docker-compose.yml`(Qdrant 6333) + `docs/patterns/env-windows-console-utf8.md`(P1·P2 콘솔 UTF-8 패턴 문서화).

## 시딩 / 검증 결과

- 실 Qdrant(`localhost:6333`) 시딩 멱등 실증: 적재 3건 → 재실행 후에도 3건(중복 0).
- 실 URL ingest 실증: `https://example.com` → title "Example Domain" 추출, Qdrant+memory 적재(Notion 은 토큰 없으면 격리), `search` 3중 `sources_used=[notion, memory, qdrant]`.
- 테스트 37 passed (qdrant upsert/search/멱등, ingest 3중 적재·격리, 3소스 RRF 포함).

```powershell
docker compose up -d                       # Qdrant 기동 (없으면 :memory: 폴백)
python scripts/seed_qdrant.py --limit 50   # RESOURCE → 벡터 시딩 (멱등)
```

## Headroom 압축 레이어 (설계서 (a) MCP 병렬)

`headroom` 은 전역 `wrap claude`(proxy 8787) 상태. yohan-mcp 도구 출력에 압축을 자동 적용하려면 MCP 서버를 headroom 에 등록한다.

```powershell
headroom mcp install        # yohan-mcp MCP 도구 출력에 압축 자동 적용
```

- 현재 stats 스냅샷: headroom **active**, `compressions: 0` (yohan-mcp 미등록 상태 — search/ingest 같은 큰 출력은 아직 비압축).
- 검증: `headroom mcp install` 후 `mcp__headroom__headroom_stats` 의 `compressions` 가 증가(0→N)하면 yohan-mcp 출력이 LLM 도달 전 압축됨을 의미.
- `status` 도구는 `HEADROOM_URL` 설정 시 headroom 헬스 한 줄을 함께 보고한다.

---

# P3 — Studio 발행 + MCP Resources/Prompts + Verifiability Engine (완료)

SW 3.0 "피부" 완성. ① Studio 발행 실동작 ② 에이전트가 스키마/few-shot 을 읽는 MCP Resources·Prompts ③ 모든 응답에 검증 메타를 동봉하는 Verifiability Engine.

## P3 산출물

- **임베딩 ollama 실모델 전환** `core/embeddings.py` — `OllamaEmbedder`(REST `/api/embed`, 기본 `bge-m3` dim 1024, 다국어/한국어 강함). env `EMBED_BACKEND`(구명칭 `EMBEDDING_BACKEND` 호환): `auto`(ollama→local→hash)·`ollama`·`local`·`openai`·`hash`. ollama 다운/모델 미설치면 **hash(384) graceful 폴백**. 차원 변경 대응 위해 `seed_qdrant.py --rebuild`(컬렉션 삭제→재생성) + `--demo`(Notion 없이 내장 RESOURCE 적재) 추가.
- **StudioAdapter 실동작** `adapters/studio_adapter.py` — `summary_to_post`(SUMMARY→POST, `post.schema` 정합, 한글 제목도 ASCII 슬러그) + `publish`. `STUDIO_API_URL`+`STUDIO_API_KEY` 둘 다 있으면 실발행(`POST /posts`), 없으면 **드라이런**(변환 결과 + "발행 보류" 플래그). 실엔드포인트는 `_PUBLISH_PATH` TODO 로 표시(코드 변경 없이 `.env` 만 채우면 전환).
- **publish/run_action 도구 실동작** `core/tools.py` — `publish(summary)`: SUMMARY→발행 + **인스턴스 링크 기록**. `run_action(publish_summary|ingest, ...)` 최소 1개 크로스 백엔드 프로토콜 실동작(나머지 등록만, P4).
- **인스턴스 링크 저장소** `core/links.py` — `published_as` 같은 **개별** 관계는 런타임 JSONL(`memory/links.jsonl`)에 적재. 스키마 타입수준 `_links.json` 은 **불변**(오염 금지).
- **MCP Resources** `server.py` — `resource://schemas/{backend}/{entity}`(스키마 원문+examples), `resource://schemas/_links`(관계맵), `resource://schemas/_index`(타입 색인), `resource://status/current`(5개 백엔드 실시간 상태).
- **MCP Prompts(few-shot)** — `create-summary`·`run-ingest`·`cross-search`. P1 `examples` 를 재활용한 기대 출력 예시 포함.
- **Verifiability Engine** `core/verify.py` — 모든 도구 응답을 표준 봉투로 확장:
  `{data, diff{before,after}, verification{schema_valid, rulebook_pass, cross_links_intact, contradiction_detected, quality_score 0~6}, provenance{sources_used, reasoning_steps}}`.
  품질 체크리스트 6항목(schema_valid·required_present·enums_valid·provenance_present·timestamps_valid·content_nonempty). `check` 도구가 엔진을 직접 호출해 6항목 점수를 반환. P2 최소형 봉투의 상위호환(기존 키 보존).

## 검증 결과

- 테스트 **81 passed** (임베딩 ollama mock/폴백, Verifiability 6항목·룰북·크로스링크·모순, Studio 변환/발행 드라이런·실, publish→published_as 링크기록·schema 불오염, run_action 프로토콜, Resources/Prompts 노출 포함).
- OllamaEmbedder 실서버 실증: `bge-m3` dim **1024**, L2 정규화, 배치 임베딩 OK.
- `seed_qdrant.py --rebuild --demo` 로 컬렉션 재생성 + 내장 RESOURCE 3건 적재 실증(멱등).

```powershell
# ollama 임베딩 모델 준비 (한 번)
ollama pull bge-m3
# .env: EMBED_BACKEND=ollama, EMBEDDING_MODEL=bge-m3, QDRANT_URL=http://localhost:6333
# 차원이 바뀌므로 컬렉션 재생성하며 재시딩 (Notion 없으면 --demo)
python scripts/seed_qdrant.py --rebuild --demo
```

> ollama 가 Docker/WSL 컨테이너로 떠 있을 때 호스트 포트포워딩이 일시적으로 끊기면(`Server disconnected`) 임베더는 hash 로 폴백한다. 컨테이너를 재기동(`docker restart ollama`)하면 복구되며, 위 명령으로 1024차원 재시딩이 가능하다.

---

# P4 — Protocol Engine + 승인큐 (자율성 L1→L2, n8n 없음) (완료)

P3 까지 도구는 **단발 실행(L1)** 이었다. P4 는 여러 타입 도구를 **프로토콜 = 순차 step 체인**으로 묶어 자동 실행하되, 외부 발행처럼 **되돌리기 어려운 step 직전에 사람 승인 게이트(L2)** 를 둔다. 오케스트레이션은 yohan-mcp 내부 **Protocol Engine** 이 담당하고, 구동은 Claude Code / VHK CLI 가 한다. **n8n·외부 스케줄러·외부 큐 의존성 없음** — 큐는 로컬 JSONL. 무인 always-on 자동화는 **P5+ 별도 트랙**(데이터 플레인 이전 포함)으로 분리한다.

## 자율성 레벨

| 레벨 | 의미 | yohan-mcp |
| --- | --- | --- |
| **L1** | 단발 도구 실행 — 사람이 매 호출을 지시 | P2~P3 도구(search/create/publish/ingest…) |
| **L2** | 프로토콜 체인 자동 실행 + **되돌리기 어려운 step 직전 사람 승인 게이트** | **P4 — run_action → [GATE] → approve** |
| L3+ | 무인 always-on(스케줄/트리거 기반 데이터 플레인) | **P5 별도 트랙(미착수)** |

## 등록 프로토콜

엔진 프로토콜(멀티스텝, `core/protocols.py`):

| 프로토콜 | step 체인 | 게이트 |
| --- | --- | --- |
| `ingest_summarize_publish` | `ingest(url)` → `create(summary 초안)` → **[GATE]** → `publish(summary)` | 발행 직전 |
| `resource_to_decision` | `search(q)` → `get_context(q)` → `create(decision 초안)` | 없음(가역적) |

P3 호환 단발 경로도 유지: `publish_summary`·`summary_to_post`(SUMMARY→발행), `ingest`(수집).

> step = `{tool, params?, map?, build?, gate?, as?, optional?}`. `map` 은 이전 step 출력을 이번 step 파라미터로 잇고(예: `draft.input.data` → `publish.summary`), `build` 는 LLM 없이 결정적으로 초안을 합성한다(`summary_from_ingest`·`decision_from_context`). 모든 step 결과는 P3 표준 검증 봉투로 누적되고(`provenance.reasoning_steps`), 멱등·재개를 위해 `memory/runs.jsonl` 저널에 스냅샷을 남긴다.

## 승인 흐름 (L2 게이트)

```
run_action(ingest_summarize_publish, {url})
        │
        ▼
   ingest(url) ──► create(summary 초안)          # 자동 실행 (가역적)
        │
        ▼
   [ GATE: publish 직전 ]  ──► 승인큐 적재(memory/approvals.jsonl)
        │                        + pending 봉투 반환(run_id) → 정지
        │
   ┌────┴───────────────────────────────────────┐
   │ approve(run_id, "approve")                  │  approve(run_id, "reject", note)
   ▼                                             ▼
 publish(summary) 완주 → completed 봉투     게이트 step 미실행 → rejected 봉투(사유)
 (드라이런이면 '발행 보류'로 완주)           (발행 안 됨)
```

- **pending 봉투**: `{status:"pending_approval", run_id, protocol, step_index, awaiting_tool, note}` — `run_id` 로 재개.
- **멱등**: 같은 `run_id` 재실행 시 완료 step 건너뜀(저장된 봉투 replay). 같은 게이트 중복 승인/거부 무시. 승인큐 `(run_id, step_index)` 중복 적재 안 함.
- **부분결과**: 한 step 실패(`data=None`)면 체인 중단 + `{status:"aborted", failed_step_index, failed_tool, completed_steps}` 봉투로 **중단 지점 명시**.
- **조회**: `approval.ApprovalQueue.list_pending()` 으로 대기 게이트 목록.

## 도구 (P4 신규/갱신)

- `run_action(protocol, params)` — Protocol Engine 위임. 게이트 만나면 pending 봉투(run_id) 반환.
- `approve(run_id, decision, note?)` — **신설**. `approve` 면 다음 step 부터 재개, `reject` 면 종료 봉투.
- `plan(goal)` — 목표 문자열 → 적합 프로토콜 추천 + step 미리보기(**dry plan, 실행 안 함**). 실행은 `run_action`.

## 검증 결과

- 테스트 **그린**(회귀 0, 새 외부 의존성 0). 신규: `test_protocols`(체인 성공/중단/부분결과/멱등), `test_approval`(pending→approve→재개·reject 종료·멱등), `test_plan`(추천+dry preview), `test_run_action_gate`(게이트 pending 반환).
- 실증: `run_action(ingest_summarize_publish, {url})` → 게이트에서 pending(run_id) 반환·정지 → `approve` 시 publish 완주(드라이런 '발행 보류') / `reject` 시 발행 안 됨.

## 다음 단계 — P5: 무인 always-on(별도 트랙)

스케줄/트리거 기반 데이터 플레인(이전 포함)으로 사람 개입 없는 상시 자동화를 **분리 트랙**에서 다룬다. P4 의 L2 게이트는 그 전 단계의 안전장치다. (n8n 채택 여부도 이 트랙에서 결정 — 현재 코어는 외부 의존성 0.)
