---
vhk_format: 1
type: meta
project: yohan-mcp
version: v0.1
---

# Common Gates

1. `python -m pytest -q`
2. `python -m compileall -q core adapters tests`

## Forbidden Actions (전역)

- 공유 Qdrant 저장소 재시딩 또는 쓰기
- 실행 중인 MCP 프로세스 중단·재시작
- master 브랜치 수정, push, merge

## Goal 파일 스키마 (필독 — VHK-021)

`vhk goal list/next/check/done` 는 `goals/*.md`(이 `_meta.md` 제외) 중 아래
frontmatter 를 만족하는 파일만 goal 로 인식한다. **하나라도 어긋나면 조용히 무시**되며
`vhk goal list` 가 경고로 알려준다.

| 필드 | 필수 | 값 |
| --- | --- | --- |
| `type` | ✅ | `goal` (문자열 그대로) |
| `id` | ✅ | **숫자만** (`1`, `2` … — `G1` 같은 문자열 ❌) |
| `status` | ✅ | `NOT_STARTED` | `IN_PROGRESS` | `DONE` | `BLOCKED` |
| `priority` | 권장 | `P0` | `P1` | `P2` |
| `title` | 권장 | 한 줄 제목 |
| `depends_on` | 선택 | 먼저 끝나야 할 Goal ID를 쉼표로 구분 (예: `1,2`) |

파일명 규칙: `goals/<id>-<name>.md` (예: `goals/1-login.md`).

## Goal 본문 Phase/Task (RFC 0065)

Goal을 더 작은 작업으로 나눌 때는 코드 펜스 밖에서 아래 정확한 문법만 사용한다.
Phase/Task 투영은 선택 사항이며, Phase가 없는 legacy Goal도 호환한다.

```markdown
### Phase 10
- [ ] **Task 100** 작업 설명 / 증거: sample-evidence
- [ ] **Task 105** `(na)` 제외 이유

### Phase 30
- [ ] **Task 220** 다음 작업 / evidence: sample-report
```

- Phase와 Goal 전체의 Task 번호는 각각 양수·고유·오름차순이어야 하며 결번을 허용한다.
- malformed Phase와 Phase 밖 Task는 구조 오류다.
- Phase가 없는 legacy Goal도 `valid: true`다. `activeGoal`에는 `phases: []`와 `tasks: []`를 담고,
  `warnings`에 warning 코드 `NO_PHASES`를 포함하고 exit 0으로 끝난다.
- `[ ]`은 `pending`, `[x]`/`[X]`는 `completed`다. Task 라벨 직후 정확히
  backticked `(na)`를 쓴 `[ ]` Task만 `notApplicable`이며, 완료 checkbox와 함께 쓰면 구조 오류다.
- `/ 증거:`와 `/ evidence:`는 같은 선택적 hint다. 파일 존재나 검증 성공, Task 완료를 뜻하지 않는다.
- `completed`와 `notApplicable`은 `terminal`이다. 첫 Phase의 pending Task는 `ready`,
  이후 Phase의 pending Task는 직전 Phase Task 전부가 terminal일 때 `ready`, 아니면 `waiting`이다.
  따라서 readiness는 `ready`, `waiting`, `terminal` 중 하나다.
- 첫 Phase의 `dependsOn`은 비어 있다. 다음 Phase의 각 Task는 직전 Phase의 모든 `goal:N/task:N` string ID를
  가진다. 같은 Phase끼리는 서로 의존하지 않지만 자동 병렬 실행을 보장하지 않는다.
- `vhk context --json`은 이 구조를 읽기 전용으로 투영하며 성공·실패 모두 파일을 쓰지 않는다.
  구조·flag·공개 경계 오류는 원문·절대경로·stack 없이 `valid: false`, `activeGoal: null`,
  안정적인 `errors` JSON과 exit 1로 끝난다. `--compact --json`도 안전한 오류다.

- 공개 경계가 차단하는 입력은 시크릿·토큰·키, 홈 절대경로, 개인 이메일·실명·저장소명, 실제 외부 객체 ID다.
  차단 시 입력 원문·경로·stack을 오류에 다시 노출하지 않으며,
  예시는 `sample-*`, `<HOME>`, 명백히 가짜 ID만 사용한다.
- `--compact --json` 충돌은 `valid: false`와 exit 1로 끝나는 flag 구조 오류다.

### 새 goal 템플릿 (복붙)

```markdown
---
vhk_format: 1
type: goal
id: 1
title: 로그인 기능
status: NOT_STARTED
priority: P0
# 선택: depends_on: 1,2
---

# Goal 1: 로그인 기능

## 배경 / 동작 / Completion Check ...

### Phase 10
- [ ] **Task 100** 작업 설명 / 증거: sample-evidence
```

게이트 스크립트는 `vhk goal sync` 로 `scripts/check-goal-<id>.mjs` 를 백필한다.
