# 프로젝트 컨텍스트

> 이 파일은 `vhk context`로 자동 생성되었습니다.
> AI 어시스턴트에게 프로젝트 맥락을 제공합니다.

## 원본 지도 (Source of Truth)

> 같은 사실은 원본 한 곳에서만 고치세요. 스냅샷은 원본을 읽어 다시 만듭니다.

- **규칙(원본)**: `RULES.md` — 규칙은 여기 한 곳에서만 수정
- **작업 정의·수용 기준**: `RULES.md`나 프로젝트 문서가 지정한 추적 원본 — 경로를 추측하지 않음
- **로컬 Goal 실행 상태**: `goals/*.md` frontmatter — 원본에서 만든 비추적 실행 카드
- **Goal 검사 스크립트(파생)**: `scripts/check-goal-<번호>.mjs` — 직접 수정 금지, `vhk goal sync`로 재생성
- **파생 스냅샷**: `.vhk/context.md`, `docs/state/next-task.md` — 원본 아님
- **로컬 차단 기록**: `docs/state/blockers.md` — append-only, 작업 정의 원본 아님
- **버전·릴리스**: `package.json`, `CHANGELOG.md`
- **명령 목록**: `COMMANDS.md` (+ `vhk help`)
- **파생본(직접 수정 금지)**: `.cursorrules`·`.windsurfrules`·`.github/copilot-instructions.md`·`AGENTS.md`·`GEMINI.md` 등 7종 + `CLAUDE.md` 규칙 영역 → `vhk sync` 로 생성

## 기술 스택

> 기술 스택 상태: 확정

### 선언된 기술 스택 (RULES.md)

- Python 3.11+
- MCP SDK · pytest
- brain `memory/` = SoT (로컬 memory/ 는 런타임 캐시만)

### 실제 감지된 기술 스택 (package.json)

- (감지 결과 없음)

## 헌법(core-rules) 소스

- configured — 사용자 규칙 파일 (v0.1.5)

## 디렉토리 구조

```text
├── .env.example
├── adapters/
│   ├── base.py
│   ├── memory_adapter.py
│   ├── n8n_adapter.py
│   ├── notion_adapter.py
│   ├── qdrant_adapter.py
│   ├── studio_adapter.py
│   ├── __init__.py
│   └── __pycache__/
│       ├── base.cpython-311.pyc
│       ├── memory_adapter.cpython-311.pyc
│       ├── n8n_adapter.cpython-311.pyc
│       ├── notion_adapter.cpython-311.pyc
│       ├── qdrant_adapter.cpython-311.pyc
│       ├── studio_adapter.cpython-311.pyc
│       └── __init__.cpython-311.pyc
├── AGENTS.md
├── CLAUDE.md
├── core/
│   ├── agent_roster.py
│   ├── approval.py
│   ├── chunking.py
│   ├── context_resolver.py
│   ├── core_rules.py
│   ├── embeddings.py
│   ├── knowledge.py
│   ├── links.py
│   ├── paths.py
│   ├── policy.py
│   ├── protocols.py
│   ├── router.py
│   ├── scheduler.py
│   ├── schema_validator.py
│   ├── tools.py
│   ├── triggers.py
│   ├── triple_map.py
│   ├── verify.py
│   ├── __init__.py
│   └── __pycache__/
│       ├── agent_roster.cpython-311.pyc
│       ├── approval.cpython-311.pyc
│       ├── chunking.cpython-311.pyc
│       ├── context_resolver.cpython-311.pyc
│       ├── core_rules.cpython-311.pyc
│       ├── embeddings.cpython-311.pyc
│       ├── knowledge.cpython-311.pyc
│       ├── links.cpython-311.pyc
│       ├── paths.cpython-311.pyc
│       ├── policy.cpython-311.pyc
│       ├── protocols.cpython-311.pyc
│       ├── router.cpython-311.pyc
│       ├── scheduler.cpython-311.pyc
│       ├── schema_validator.cpython-311.pyc
│       ├── tools.cpython-311.pyc
│       ├── triggers.cpython-311.pyc
│       ├── triple_map.cpython-311.pyc
│       ├── verify.cpython-311.pyc
│       └── __init__.cpython-311.pyc
├── docker-compose.yml
├── docs/
│   ├── audits/
│   │   └── overnight-2026-07-01.md
│   ├── patterns/
│   │   └── env-windows-console-utf8.md
│   └── state/
│       ├── blockers.md
│       ├── learnings.md
│       └── next-task.md
├── GEMINI.md
├── goals/
│   ├── 1-graph-aware-context-resolver.md
│   └── _meta.md
├── pytest.ini
├── README.md
├── requirements.txt
├── RULES.md
├── schemas/
│   ├── memory/
│   │   ├── decision.schema.json
│   │   ├── ingest.schema.json
│   │   └── profile.schema.json
│   ├── notion/
│   │   ├── ai-dict.schema.json
│   │   ├── execution-log.schema.json
│   │   ├── resource.schema.json
│   │   ├── summary.schema.json
│   │   └── triple.schema.json
│   ├── studio/
│   │   ├── post.schema.json
│   │   └── product.schema.json
│   ├── _links.json
│   └── _shared-enums.json
├── scripts/
│   ├── check-goal-1.mjs
│   ├── eval_recall.py
│   ├── knowledge.py
│   ├── notebooklm_source_identity.py
│   ├── query_context.py
│   ├── reseed-brain.ps1
│   ├── seed_brain_memory.py
│   ├── seed_qdrant.py
│   ├── smoke_full_loop.py
│   ├── validate_schemas.py
│   └── __pycache__/
│       ├── eval_recall.cpython-311.pyc
│       ├── knowledge.cpython-311.pyc
│       ├── notebooklm_source_identity.cpython-311.pyc
│       └── seed_brain_memory.cpython-311.pyc
├── server.py
├── tests/
│   ├── conftest.py
│   ├── test_adapters.py
│   ├── test_agent_roster.py
│   ├── test_approval.py
│   ├── test_chunking.py
│   ├── test_context_resolver.py
│   ├── test_core_rules.py
│   ├── test_devlog_pattern_query.py
│   ├── test_embeddings.py
│   ├── test_eval_recall.py
│   ├── test_full_loop.py
│   ├── test_get_context_vector.py
│   ├── test_integration_qdrant.py
│   ├── test_knowledge.py
│   ├── test_memory_brain_md.py
│   ├── test_paths.py
│   ├── test_plan.py
│   ├── test_policy.py
│   ├── test_protocols.py
│   ├── test_publish_tools.py
│   ├── test_qdrant.py
│   ├── test_resources_prompts.py
│   ├── test_router.py
│   ├── test_run_action_gate.py
│   ├── test_scheduler.py
│   ├── test_search_weighting.py
│   ├── test_seed_brain_memory.py
│   ├── test_studio_mdx.py
│   ├── test_studio_publish.py
│   ├── test_tools.py
│   ├── test_triggers.py
│   ├── test_triple_map.py
│   ├── test_verify.py
│   ├── __init__.py
│   └── __pycache__/
│       ├── conftest.cpython-311-pytest-9.1.1.pyc
│       ├── conftest.cpython-311.pyc
│       ├── test_adapters.cpython-311-pytest-9.1.1.pyc
│       ├── test_adapters.cpython-311.pyc
│       ├── test_agent_roster.cpython-311-pytest-9.1.1.pyc
│       ├── test_agent_roster.cpython-311.pyc
│       ├── test_approval.cpython-311-pytest-9.1.1.pyc
│       ├── test_approval.cpython-311.pyc
│       ├── test_chunking.cpython-311-pytest-9.1.1.pyc
│       ├── test_chunking.cpython-311.pyc
│       ├── test_context_resolver.cpython-311-pytest-9.1.1.pyc
│       ├── test_context_resolver.cpython-311.pyc
│       ├── test_core_rules.cpython-311-pytest-9.1.1.pyc
│       ├── test_core_rules.cpython-311.pyc
│       ├── test_devlog_pattern_query.cpython-311-pytest-9.1.1.pyc
│       ├── test_devlog_pattern_query.cpython-311.pyc
│       ├── test_embeddings.cpython-311-pytest-9.1.1.pyc
│       ├── test_embeddings.cpython-311.pyc
│       ├── test_eval_recall.cpython-311-pytest-9.1.1.pyc
│       ├── test_eval_recall.cpython-311.pyc
│       ├── test_full_loop.cpython-311-pytest-9.1.1.pyc
│       ├── test_full_loop.cpython-311.pyc
│       ├── test_get_context_vector.cpython-311-pytest-9.1.1.pyc
│       ├── test_get_context_vector.cpython-311.pyc
│       ├── test_integration_qdrant.cpython-311-pytest-9.1.1.pyc
│       ├── test_integration_qdrant.cpython-311.pyc
│       ├── test_knowledge.cpython-311-pytest-9.1.1.pyc
│       ├── test_knowledge.cpython-311.pyc
│       ├── test_memory_brain_md.cpython-311-pytest-9.1.1.pyc
│       ├── test_memory_brain_md.cpython-311.pyc
│       ├── test_paths.cpython-311-pytest-9.1.1.pyc
│       ├── test_paths.cpython-311.pyc
│       ├── test_plan.cpython-311-pytest-9.1.1.pyc
│       ├── test_plan.cpython-311.pyc
│       ├── test_policy.cpython-311-pytest-9.1.1.pyc
│       ├── test_policy.cpython-311.pyc
│       ├── test_protocols.cpython-311-pytest-9.1.1.pyc
│       ├── test_protocols.cpython-311.pyc
│       ├── test_publish_tools.cpython-311-pytest-9.1.1.pyc
│       ├── test_publish_tools.cpython-311.pyc
│       ├── test_qdrant.cpython-311-pytest-9.1.1.pyc
│       ├── test_qdrant.cpython-311.pyc
│       ├── test_resources_prompts.cpython-311-pytest-9.1.1.pyc
│       ├── test_resources_prompts.cpython-311.pyc
│       ├── test_router.cpython-311-pytest-9.1.1.pyc
│       ├── test_router.cpython-311.pyc
│       ├── test_run_action_gate.cpython-311-pytest-9.1.1.pyc
│       ├── test_run_action_gate.cpython-311.pyc
│       ├── test_scheduler.cpython-311-pytest-9.1.1.pyc
│       ├── test_scheduler.cpython-311.pyc
│       ├── test_search_weighting.cpython-311-pytest-9.1.1.pyc
│       ├── test_search_weighting.cpython-311.pyc
│       ├── test_seed_brain_memory.cpython-311-pytest-9.1.1.pyc
│       ├── test_seed_brain_memory.cpython-311.pyc
│       ├── test_studio_mdx.cpython-311-pytest-9.1.1.pyc
│       ├── test_studio_mdx.cpython-311.pyc
│       ├── test_studio_publish.cpython-311-pytest-9.1.1.pyc
│       ├── test_studio_publish.cpython-311.pyc
│       ├── test_tools.cpython-311-pytest-9.1.1.pyc
│       ├── test_tools.cpython-311.pyc
│       ├── test_triggers.cpython-311-pytest-9.1.1.pyc
│       ├── test_triggers.cpython-311.pyc
│       ├── test_triple_map.cpython-311-pytest-9.1.1.pyc
│       ├── test_triple_map.cpython-311.pyc
│       ├── test_verify.cpython-311-pytest-9.1.1.pyc
│       ├── test_verify.cpython-311.pyc
│       └── __init__.cpython-311.pyc
├── triggers.json
└── __pycache__/
    └── server.cpython-311.pyc
```

## VHK CLI 명령어

- `vhk gate — 아이디어 검증`
- `vhk start — 새 프로젝트 시작 마법사`
- `vhk bootstrap — Cursor/에이전트 배선 bootstrap (cursor)`
- `vhk init — 하네스 파일 생성`
- `vhk recap — 오늘 한 일 정리 + ADR 분리`
- `vhk sync — RULES.md → 규칙 파일 동기화`
- `vhk check — RULES.md 규칙 점검`
- `vhk secure — 보안 스캔 (시크릿 유출 검사)`
- `vhk cloud — .vhk 클라우드 백업·복원 (push/pull)`
- `vhk ship — 배포 체크리스트 + 회고`
- `vhk doctor — 개발 환경 점검 (+ --strict 드리프트 게이트)`
- `vhk save — git 저장 (add → commit → push)`
- `vhk undo — 최근 커밋 되돌리기`
- `vhk restore — sync 백업 복원`
- `vhk status — 프로젝트 상태 대시보드`
- `vhk stats — 통계 대시보드 — 패스율/차단율/진화 적용율 (읽기 전용)`
- `vhk diff — Git 변경사항 한국어 요약`
- `vhk diff-cover — 이번 변경이 테스트로 커버됐는지 측정 (자문형)`
- `vhk mcp — MCP 서버 시작 (stdio)`
- `vhk mcp-init — Cursor·Claude Desktop MCP 설정 생성`
- `vhk inject-bootstrap — tier S harness (ecosystem · CORE-RULES · context · mcp.example)`
- `vhk deploy — 프로덕션 배포 (자동 감지)`
- `vhk env — .env → .env.example 동기화`
- `vhk env-check — 필수 환경변수 누락 검사`
- `vhk publish — npm 배포 (버전 범프 → 빌드 → 테스트)`
- `vhk design — 디자인 토큰 생성`
- `vhk design-palette — 컬러 팔레트 프리셋 선택`
- `vhk theme — 다크/라이트 모드 CSS 생성`
- `vhk ref — 레퍼런스 URL 관리 (add/list/open)`
- `vhk harness — 통합 품질 점검 (lint+type+test+build)`
- `vhk audit — 보안 취약점 감사 (npm audit)`
- `vhk migrate — 패키지 매니저 전환 (npm/yarn/pnpm)`
- `vhk update — VHK CLI 셀프 업데이트`
- `vhk context — 프로젝트 맥락 파일 생성 (.vhk/context.md)`
- `vhk mode — Safety Mode 조회/변경 (lite|standard|strict)`
- `vhk verify — 검증 게이트 실행 + 증거 기록`
- `vhk cost — 비용·예산 가드 — add/check/budget (자문형)`
- `vhk preflight — 출고 전 안전점검 (2FA·shim·env·lint·타입·테스트·git, 치명 시 차단)`
- `vhk testmap — test-first 매핑 점검 (변경 기능 ↔ 테스트 누락 경고)`
- `vhk worktree — worktree 가드 — 생성 시 env/설정 자동 복사·누락 점검 (add/check)`
- `vhk standup — 아침 브리핑 (어제 한 일 + 오늘 추천 goal + 미해결)`
- `vhk today — 저녁 자축·회고 (오늘 커밋·완료 goal 카운트 + 격려)`
- `vhk review — 적대적 자기검증 (거짓완료 의심 탐지)`
- `vhk receipt — 증거 영수증 — 4대 기계증거로 거짓완료 판정 (block/caution/pass)`
- `vhk mission — 미션 계약 — 작업 목표·허용/금지 범위 선언·검증`
- `vhk context-show — 컨텍스트 파일 내용 출력`
- `vhk memory — 기억 관리 v2 (decisions/failures/successes)`
- `vhk recall — 기억 회상 (자연어 키워드 검색 — RFC 0049)`
- `vhk brief — 프로젝트 요약 보고서 생성`
- `vhk loop-brief — 루프 1틱 앵커 생성 (의도+goal1+교훈+STOP)`
- `vhk remind — 치명 규칙 재주입 (RULES.md NON-NEGOTIABLE/Forbidden 압축)`
- `vhk content — 콘텐츠 초안 프롬프트 (풀사이클 뒷단 — 콘텐츠/마케팅)`
- `vhk launch — 런칭 게시물 프롬프트 (풀사이클 뒷단 — 런칭)`
- `vhk ops — 운영 회고 프롬프트 (풀사이클 뒷단 — 운영)`
- `vhk sell — 판매 카피 프롬프트 (풀사이클 뒷단 — 판매)`
- `vhk work — AI 작업 시작/이어하기 (+ handoff)`
- `vhk goal — Goal 단계별 미션 관리`
- `vhk blocker — 블로커 기록 (3건 누적 시 HARD_STOP)`
- `vhk learn — 교훈 기록 → memory v2 단일 SoT`
- `vhk win — 성공 기록 → memory successes (reinforce 입력)`
- `vhk autonomy-log — 자율 루프 런 시작/종결 기록 (완주율 계측, #373)`
- `vhk watch — 무인 세션 정지 감시 — idle 초과 시 텔레그램·콘솔 알림`
- `vhk resume — .vhk/HARD_STOP 해제 (--confirm 필요)`
- `vhk pattern — 반복 패턴 감지·목록 (avoid/reinforce)`
- `vhk evolve — 패턴 → 7일 룰 후보 표시·사람 승인·되돌리기`
- `vhk loop — 자가진화 조율 1틱 — 다음 한 수 (읽기 전용)`
- `vhk seo — SEO·수익 대시보드 (init: 사이트 등록 + 자격증명 보관)`
- `vhk config — vhk 사용자 설정 (set-rules-file: 사용자 규칙 YAML, 재시작 불필요)`

## 최근 활동 (git log — goals/blockers/memory 미사용 시 폴백)

```
62848f7 fix(search): bound latency and seed graph index
66123e6 fix(seed): memory/ 밖 소스의 상대경로 계산 오류 수정
fab7385 fix(seed): 벡터 색인 누락 3종 복구 + PS 콘솔 UTF-8
5f01e12 chore(위생): 감사 문서 커밋 + 로컬 MCP 등록 무시
aa48ae6 feat: 의미평가 실패에 어느 근거 항목이 떨어졌는지 기록 (#74)
```

---

_생성: 2026. 8. 22. 오후 10:12:27_
_vhk-context-git: 62848f728c5b10f57282ce3bb94e30a8991b2ac0_
