# yohan-mcp — AGENTS.md (에이전트 작동 규약)

> ⚡ 이 파일은 RULES.md에서 자동 생성됨 (vhk sync). 직접 수정 금지.

## Loop Protocol
- 루프: `context → goal next → 작업 → goal check → goal done`
- 작업 시작 시 `.vhk/HARD_STOP` 확인 — 있으면 모든 자동화 즉시 중단.
- active goal 만 작업. `docs/state`(next-task/blockers)는 append-only.
- 교훈·결정·실패·성공은 `vhk memory`(memory v2 4버킷, 단일 출처).
- 게이트(tsc / test:run / build) 통과해야만 `vhk goal done`.

## Ecosystem (cross-repo)

> Contract SoT: yohan-brain `memory/core/ecosystem-contract.yaml` (obey when status=active).

- **Tier:** yohan-brain `memory/core/inheritance-registry.yaml`
- **Roster:** yohan-brain `memory/core/agent-roster.yaml` (CLI·모델·effort; obey when active)
- **Cursor:** `.cursor/rules/ecosystem.mdc` (vhk inject-bootstrap)
- **금지:** AGENTS.md 손수 편집 → `RULES.md` + `vhk sync`

## 기술 스택
- Python 3.11+
- MCP SDK · pytest
- brain `memory/` = SoT (로컬 memory/ 는 런타임 캐시만)

## 코딩 규칙
- type hints · pytest 필수
- `core/paths.py` — brain 경로 단일 소스
- repo 로컬 memory 를 SoT 로 쓰지 않음 (ecosystem-contract forbidden)
- 시크릿·`.env` 커밋 금지

## 기록 규칙
- 아키텍처 결정 → yohan-brain `memory/decisions/` 또는 `docs/adr/`
- 세션 로그 → brain `memory/logs/sessions/` (cross-repo 작업 시)

## 기타 규칙
> RULES.md 의 비표준 H2 섹션 — 표준 매핑 외이지만 보존 위해 전파(직접 수정은 RULES.md 에서).

### 교훈 (Dev Log 역전파, 2026-07-01 PR #22·#23·#24)
- 공유 벡터스토어/DB를 "비어있다"고 가정하고 삭제·초기화하지 않는다 — 삭제 전 read-only count로 실존·소유 여부를 먼저 확인한다 (다른 프로젝트의 실데이터가 같은 스토어에 수천 건 존재할 수 있음).
- PowerShell here-string(`@'...'@`)을 Bash 툴(POSIX sh)에서 사용하지 않는다 — 커밋 subject 등에 `@` 문자가 그대로 누출된다.
- `load_dotenv()`는 기본 `override=False` — 서브프로세스 spawn 시 비어있는 환경변수(예: `QDRANT_URL`)가 `.env` 값을 가려 의도치 않은 접속 대상(예: `:memory:`)으로 빠질 수 있다. 값이 실제로 반영됐는지 확인 후 실행한다.
- 머지 전 적대적 코드리뷰 게이트는 생략하지 않는다 — 파괴적 footgun(예: 데모 시드가 실제 컬렉션을 드롭·오차원 생성)과 테스트 위양성(top_k=0 동어반복, id stem 충돌, env 누출)을 다수 걸러낸 실적이 있다.

<!-- YOHAN-ROSTER-CARD:BEGIN (managed by yohan-brain ops/propagation ??SoT瑜?怨좎퀜?? 吏곸젒?섏젙 湲덉?) -->
## 상시 지휘자 — 라우팅 카드 (yohan ecosystem)

> SoT: yohan-brain `memory/core/agent-roster.yaml` `conductor_always_on` (v0.4+, status=active면 obey).
> 이 레포 자체 규칙(RULES/CLAUDE LIVE)이 있으면 그게 우선(precedence).

- 모든 태스크: 해법 구상 **전에** 크기 판정 → `라우팅: S|M|L — 계획 1줄 (근거: 파일수/신규설계/리스크)` 선언 후 진행. 키워드("풀개발") 불필요, 항상.
- **S**(≤2파일·신규설계 없음·≤15분): 지휘자 단독. 서브에이전트·orca 금지(오버헤드).
- **M**(3~6파일·부분 신규): 서브에이전트 티어링 — 탐색 haiku → 계획 opus(승인) → 구현 sonnet → 적대검증 opus/fable 루프.
- **L**(≥7파일·신규 모듈·다레포·릴리즈급): /goal orca 풀파이프라인 — Scout→Plan승인★→worktree fanout→타벤더 적대검증→머지게이트★. "풀개발"=L 강제.
- 하드 트리거(분류 생략): 스키마 마이그레이션·인증/결제/보안·크로스레포·릴리즈 = 무조건 **L** · 오타·문서/주석만 = **S**.
- 애매하면 작은 쪽 시작 → 검증 실패(테스트/tsc/critic) 시 **재선언 후 승급**(몰래 계속 금지).
- 동시 작업 = worktree만. 같은 레포·같은 브랜치 2에이전트 금지.
- Antigravity(agy) = 보조·초안 전용(메인 지휘 금지) — 산출물은 상위 티어 검증 필수.
- 배포·시크릿·npm publish·main 직push = 사람 게이트(불변).
<!-- YOHAN-ROSTER-CARD:END -->
