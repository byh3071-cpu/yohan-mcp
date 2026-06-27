# yohan-mcp — AGENTS.md (에이전트 작동 규약)

> ⚡ 이 파일은 RULES.md에서 자동 생성됨 (vhk sync). 직접 수정 금지.
> 빠른 시작(토큰 절감): `docs/context/agent-compact.md` 를 먼저 읽으세요.

## Loop Protocol
- 루프: `context → goal next → 작업 → goal check → goal done`
- 작업 시작 시 `.vhk/HARD_STOP` 확인 — 있으면 모든 자동화 즉시 중단.
- active goal 만 작업. `docs/state`(next-task/blockers)는 append-only.
- 교훈·결정·실패·성공은 `vhk memory`(memory v2 4버킷, 단일 출처).
- 게이트(tsc / test:run / build) 통과해야만 `vhk goal done`.

## Ecosystem (cross-repo)

> Contract SoT: yohan-brain `memory/core/ecosystem-contract.yaml` (obey when status=active).

- **Tier:** yohan-brain `memory/core/inheritance-registry.yaml`
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
