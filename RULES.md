# yohan-mcp — 프로젝트 규칙 단일 소스 (Single Source of Truth)

> ⚡ 규칙 변경은 **여기서만** — `vhk sync` 로 AGENTS.md · .cursorrules 전파.

## 서문

- 한 줄 설명: Yohan ecosystem MCP nervous system (brain memory/ SoT 읽기)
- 레포: https://github.com/byh3071-cpu/yohan-mcp
- tier: S (inheritance-registry)

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
