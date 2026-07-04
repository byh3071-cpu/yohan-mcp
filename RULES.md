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

## 교훈 (Dev Log 역전파, 2026-07-01 PR #22·#23·#24)

- 공유 벡터스토어/DB를 "비어있다"고 가정하고 삭제·초기화하지 않는다 — 삭제 전 read-only count로 실존·소유 여부를 먼저 확인한다 (다른 프로젝트의 실데이터가 같은 스토어에 수천 건 존재할 수 있음).
- PowerShell here-string(`@'...'@`)을 Bash 툴(POSIX sh)에서 사용하지 않는다 — 커밋 subject 등에 `@` 문자가 그대로 누출된다.
- `load_dotenv()`는 기본 `override=False` — 서브프로세스 spawn 시 비어있는 환경변수(예: `QDRANT_URL`)가 `.env` 값을 가려 의도치 않은 접속 대상(예: `:memory:`)으로 빠질 수 있다. 값이 실제로 반영됐는지 확인 후 실행한다.
- 머지 전 적대적 코드리뷰 게이트는 생략하지 않는다 — 파괴적 footgun(예: 데모 시드가 실제 컬렉션을 드롭·오차원 생성)과 테스트 위양성(top_k=0 동어반복, id stem 충돌, env 누출)을 다수 걸러낸 실적이 있다.
