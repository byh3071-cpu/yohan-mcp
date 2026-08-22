# 무인 자율 결함루프 — 야간 보고 (2026-07-01)

> overnight-autoloop 워크플로 · 발굴 12 / PR 6 / 머지 0(설계) · PR 6건 실재·MERGEABLE 전수검증 완료

## 한눈에 (두괄식)

- **밤사이 결함 12건 발굴 → 6건 자동수정·PR 생성, 1건 park(미시도), 5건 미상세 미시도.** 머지는 0건(설계대로 밤엔 안 함) — 아래 권고 보고 **아침에 사람이 결정**.
- PR 6건 **전부 open·MERGEABLE 상태로 확인**(yohan-mcp #13/#14/#15, control-tower #10/#11/#12). 각 PR은 1회 시도로 통과, 적대적 리뷰 approve + 셀프검증(테스트/타입체크/린트) 통과.
- **단, 리뷰·테스트 결과는 자동루프(오케스트레이터 자체 리뷰+셀프 pytest/tsc/eslint) 기준**이며 GitHub 정식 리뷰는 미제출(`reviewDecision` 공란). CI가 있다면 머지 전 그린 확인 권장.

## PR별 머지 권고

| PR | 레포 | 심각도 | 권고 | 한줄 근거 |
|----|------|--------|------|-----------|
| [#13](https://github.com/byh3071-cpu/yohan-mcp/pull/13) RRF 단일백엔드 멀티청크 인플레이션 | yohan-mcp | high | **머지 권고** | 핵심 랭킹 결함, 회귀 0 |
| [#10](https://github.com/byh3071-cpu/yohan-control-tower/pull/10) 노션 표 셀 본문 직렬화 누락 | control-tower | med | **머지 권고** | 데이터 손실 결함, 근본수정 |
| [#11](https://github.com/byh3071-cpu/yohan-control-tower/pull/11) 라벨-only 빈청크 차단 | control-tower | med | **머지 권고** | 임베딩 품질 결함, 회귀 0 |
| [#15](https://github.com/byh3071-cpu/yohan-mcp/pull/15) _resolve_summary 무로그 except | yohan-mcp | low | **머지 권고** | 1라인 진단로깅, 동작보존 |
| [#14](https://github.com/byh3071-cpu/yohan-mcp/pull/14) studio PR push실패 published=False | yohan-mcp | med | **조건부 권고** | 가드 1줄로 정확수정, 단 잔존한계 1건 |
| [#12](https://github.com/byh3071-cpu/yohan-control-tower/pull/12) SOURCES.expected 드리프트 교정 | control-tower | med | **조건부 권고** | UI 힌트 상수 교체, 단 '라이브 실측' 신선도 확인 권장 |

### 즉시 머지 권고 (4건)

**[#13](https://github.com/byh3071-cpu/yohan-mcp/pull/13) — RRF 단일백엔드 멀티청크 인플레이션 (high) ★최우선**
- 근거: `core/router.py` `_rrf_fuse`에 백엔드별 `counted_keys` set 추가 → 같은 `(type::id)` 엔티티는 백엔드당 최선(첫) rank 1회만 가산. `counted_keys`가 백엔드 루프 안에서 매번 새로 선언돼 **교차 백엔드 융합은 정상 유지**(기존 융합 테스트 전부 통과). data/provenance는 최상위 청크에서 채워 무결성 보존.
- 검증: 신규 회귀테스트 `test_rrf_single_backend_multichunk_no_inflation` 통과(page1 점수=1/61 1회 기여로 인플레이션 0). router 8 passed, 전체 167 passed(6.23s). 변경 router 10줄+테스트 24줄로 범위이탈 없음.

**[#10](https://github.com/byh3071-cpu/yohan-control-tower/pull/10) — 노션 표 셀 본문 직렬화 누락 (med)**
- 근거: `richTextItemsOf`의 `.rich_text` 키 검사를 통과 못하던 cells(2차원)를 신규 `tableRowCellsOf` 가드+`table_row` 분기로 펼쳐 `' | '` 결합. `table_row` 한정 조기분기라 타 블록 경로 무변경=회귀 없음. `lib/notion.ts` 1파일 +25줄.
- 검증: typecheck/lint 통과.

**[#11](https://github.com/byh3071-cpu/yohan-control-tower/pull/11) — 라벨-only 빈청크 차단 (med)**
- 근거: 본문 없는 헤딩이 무의미 청크로 임베딩되던 결함 차단. 헤딩+본문/서두/빈문서 경로 동작 보존, constitution 생성기는 헤딩-only body에서 fallback으로 본문 전체 보존(오히려 개선). `if(text)` 가드 제거도 content 비공백 보장으로 안전.
- 검증: tsc EXIT 0, eslint EXIT 0. (레포에 테스트 스위트 없음 → 지정 검증=typecheck+lint)

**[#15](https://github.com/byh3071-cpu/yohan-mcp/pull/15) — _resolve_summary 무로그 except (low)**
- 근거: 직전 커밋 ad0e44f가 커버한 5사이트에 빠진 6번째 사이트. 조용한 `except Exception: pass`를 `logger.warning` 진단로깅으로 교체(1라인). logger 모듈레벨(L43) 정의로 NameError 없음, except가 여전히 잡고 return None → 동작보존(회귀 0).
- 검증: 관련테스트 42 passed, 전체 166 passed(7.21s).

### 조건부 머지 권고 (2건)

**[#14](https://github.com/byh3071-cpu/yohan-mcp/pull/14) — studio PR모드 push실패 published=False (med)**
- 근거: push 실패해도 `published=True` + 멱등 저널 기록으로 재시도가 영구 차단되던 결함을 가드 1줄로 수정(실패 시 `published=False` + 저널 미오염). 신규 회귀테스트가 실제 git repo로 push-only 실패 경로 재현.
- **유보 포인트**: 리뷰가 짚은 **기존 `_publish_pr` 한계 관찰 1건**(낮은 심각도, 블로커 아님)이 잔존. 이번 PR 범위 밖이므로 머지는 가능하나 후속 이슈로 남길지 판단 필요.
- 검증: 전체 167 passed(5.20s), studio 단위 9 passed.

**[#12](https://github.com/byh3071-cpu/yohan-control-tower/pull/12) — SOURCES.expected 드리프트 교정 (med)**
- 근거: 대시보드 '예상 건수'가 실제 노션 행수와 4~8배 어긋나던 것을 `lib/sources.ts`의 11개 expected 상수 교정으로 수정. expected는 `IngestButton.tsx`의 "{expected}건 예상" 표시 전용 순수 UI 힌트 → 로직/타입/시그니처 변경 0, 함수적 회귀 없음. resource=196은 직전 머지 #9의 "196p→377청크"와 일치, qdrant 4컬렉션 청크총량과도 교차 일관.
- **유보 포인트**: 값이 **밤사이 시점 '라이브 노션 실측'**에 의존하는 정적 상수다. 노션 행수가 자주 바뀌면 곧 재드리프트 가능 → 머지는 OK이나 **수동 상수 대신 동적 계측으로 가는 후속 검토** 권장. typecheck/lint 통과.

## Park (미시도) — 1건

**[control-tower] 부분실패 집계 단위 불일치 — failed(레코드) vs chunks/upserted(청크) 혼용 (low, fixable)**
- 사유: 데이터 자체엔 영향 없는 **보고 정확성** 결함이라 high/med 수정 우선순위에 밀려 이번 루프에선 시도 안 함(park).
- 내용: `lib/ingest.ts:124-128` `failed += 1`(레코드 그룹 단위) vs `:141` `chunks: subs.length`(실패 포함 전체 청크)·`upserted`(성공 청크만). 예) chunks=281/upserted=250/failed=8이면 청크 31 손실인데 failed=8(레코드)로 보고돼 손실 규모를 수치로 알 수 없음.
- 수정 가능성: `recordsFailed`(레코드)와 `chunksFailed=chunks-upserted`(청크)를 분리해 `IngestSummary` 필드/계산 경계를 명확히 하면 됨 → 다음 루프 후보로 권고.

## 발굴했으나 미시도 (집계)

- **전체 발굴 12건** = PR 6 + 상세 park 1 + **미상세 미시도 5건**.
- 미상세 5건은 이번 산출물에 항목 detail이 넘어오지 않음 → **여기서 임의 복원/추정하지 않음(과장 방지)**. 루프 로그/발굴 원장에서 5건 목록을 별도 확인해야 정확.

## 후속 권고

1. **머지 순서**: high #13 먼저 → med 본문/표 #10·#11 → low #15 → 조건부 #14·#12. 같은 레포 내 충돌 없으니 독립 머지 가능(전부 MERGEABLE 확인).
2. **CI 그린 확인**: `reviewDecision` 공란 = GitHub 정식 리뷰/체크 미반영. CI 워크플로가 있으면 머지 전 통과 확인.
3. **#12 후속**: expected 정적 상수 → 동적 행수 계측 전환 검토(재드리프트 방지).
4. **다음 루프 우선순위**: park된 부분실패 집계 단위 결함 + 미상세 5건 트리아지.

---

# 독립 적대 리뷰 (2차 검증) — 최종 머지권고

## 결론: 6건 전부 머지 가능 — 5건 즉시 머지(merge), 1건(#14) 조건부(conditional), hold 0건

내 독립 적대 리뷰 결과 **6개 PR 모두 주장된 결함을 정확히 고쳤고 회귀 없음(matchesClaim·fixCorrect·noRegression 전부 true)**. 자동루프 자체리뷰(=PR 진단)와 독립리뷰가 **결함 진단·수정 정확성에서는 전건 일치**. 어긋난 지점은 단 2곳이며 둘 다 코드 결함이 아니라 **PR 본문 영향과장(#12)**·**선재 한계 잔존(#14)** 수준이다.

---

### PR별 종합 (독립리뷰 vs 자동루프 자체리뷰 일치여부)

| PR | 레포 | 심각도 | 결함 | 독립↔자동루프 일치 | 권고 | 핵심 근거 |
|----|------|--------|------|----------------------|------|-----------|
| **#13** | yohan-mcp | **high** | RRF 융합이 단일 백엔드 멀티청크 중복을 합산→랭킹 인플레이션 | ✅ 일치 (+ 설계주석 1) | **merge** | 백엔드별 counted_keys로 중복가산 차단, 교차백엔드 융합 유지. 전체 167 + 신규 테스트 통과 |
| **#14** | yohan-mcp | med | push 실패 시 거짓 published=True + 멱등 저널 오염 | ⚠️ **부분 어긋남**: 수정은 정확하나 독립리뷰가 자체리뷰엔 없는 **선재 한계 잔존**을 추가 적발 | **conditional** | 가드는 저널 미오염 보장 O. 단 orphan 커밋 탓에 origin 복구 후 재시도해도 'nothing to commit'→발행 영구 불가(이 PR이 만든 결함 아님, 선재) |
| **#15** | yohan-mcp | low | `_resolve_summary` 무로그 except 침묵 | ✅ 완전 일치 | **merge** | except:pass→logger.warning, 동작보존·포맷인자 일치·부작용 0 |
| **#10** | control-tower | med | table_row 셀 누락 직렬화 결함 | ✅ 일치 (무해한 docstring 불일치 1) | **merge** | tableRowCellsOf 가드+분기, table 부모 자식재귀 회수. typecheck·lint 통과 |
| **#11** | control-tower | med | 본문없는 라벨-only 빈청크 | ✅ 일치 | **merge** | content 비공백 보장으로 차단. ⚠️ **레포에 테스트 스위트 0** → 수동추적·typecheck·lint로만 검증 |
| **#12** | control-tower | med | expected 상수 11개 교정 | ⚠️ **어긋남**: 자체리뷰(PR본문)는 "인제스트 진행/완료 판정 기준선이 깨진다"고 주장하나 독립리뷰 결과 **완료판정 로직 자체가 없음→영향 과장**(코드 결함 아님) | **merge** | expected는 '{n}건 예상' 표시 전용, 제어흐름 무관. +11/-11 상수만, typecheck·lint 통과 |

**어긋난 지점 요약 (과장 없이):**
- **#12 — 영향 과장**: 자동루프가 PR 본문에 적은 "완료 판정 기준선 붕괴"는 실제로 완료판정 로직이 없어 성립 안 함. 순수 UI 힌트 수정. 머지엔 무관하나 자동루프 자체평가가 자기 변경의 파급을 부풀린 사례.
- **#14 — 미완결 회복**: 자동루프는 "고침"으로 종결했으나, 독립리뷰는 push 실패가 남긴 orphan 커밋 탓에 **재시도 회복이 완전치 않다**는 잔존 한계를 추가 발견. 이 PR이 만든 결함은 아니므로 hold가 아닌 conditional.

---

### 권장 머지 순서 (고심각도·독립적용 우선)
PR 간 명시된 의존성 없음. 2개 레포 분리 진행.

1. **#13** (yohan-mcp, high) — 최고 심각도, 독립 적용, 랭킹 정확성 직결 → **최우선**
2. **#10** (control-tower, med) — 셀 누락 직렬화, 임베딩 정확성
3. **#11** (control-tower, med) — 빈청크 차단 (단 테스트 부재 인지하고 머지)
4. **#12** (control-tower, med) — UI 상수, 위험 최저
5. **#15** (yohan-mcp, low) — 순수 로깅, 무위험
6. **#14** (yohan-mcp, conditional) — **마지막**. 머지하되 후속 이슈로 "orphan 커밋 재시도 회복" 선재 한계를 등록할 것

---

### 머지 전 주의 (필독)

- **self-merge 차단 가능성**: 세 PR(#13·#14·#15) 모두 **author=byh3071-cpu(=머지 주체 본인)**. 브랜치 보호/분류기가 자기 작성 PR의 self-merge를 막을 수 있음. 막히면 (a) 다른 리뷰어 승인 경로 또는 (b) 보호 규칙 일시 우회 권한 확인 필요. 막힘 자체는 정상 가드이니 무력화하지 말 것.
- **CI 그린의 실제 범위**: 열린 yohan-mcp PR(#13·#14·#15)의 GitHub 체크는 **CodeRabbit pass 단일뿐** — **테스트 스위트를 굴리는 CI 게이트는 GitHub에 없음**. 리뷰에 인용된 "167 테스트 통과"는 **로컬 실행 결과**. 즉 "CI 그린"="CodeRabbit + 로컬 테스트"이지 자동화된 회귀 게이트가 아님. #11(control-tower)은 **레포에 테스트 자체가 없어** typecheck·lint로만 검증됨. 머지 후 회귀 자동검출 안전망이 얇다는 점 인지.
- mergeStateStatus: #13·#14·#15 전부 **CLEAN/MERGEABLE** 확인됨. 충돌 없음.


---

# PR #14 심층 적대검증 — 최종

## 결론: #14 지금 머지 안전 (순개선) + orphan 한계는 별건 후속

**머지 판정: merge_now_plus_followup — 지금 머지하되 orphan 재시도 한계는 후속 이슈로 분리.**

### 핵심질문 답
1. **#14는 현재상태보다 순개선인가(=머지 안전)?** → 예. 확인됨.
   - master(44c449d)의 `adapters/studio_adapter.py` publish 경로는 `pr = _publish_pr(...)` 직후 **무조건** `self._journal.record(...)` 실행 후 `published:True` 반환. 즉 push 실패(`pushed=False`)여도 "발행됨"으로 저널에 기록 → 이후 동일 content가 `already_published` no-op로 **영구차단**되던 선재 결함 존재.
   - #14의 가드는 `_publish_pr` 반환의 `pushed=False`를 감지해 저널 기록을 건너뛰고 `published:False + errors`로 반환 → 저널 오염을 정확히 차단. 성공경로(pushed=True)는 무변경 → **회귀 없음**. studio 테스트 9 passed(신규 회귀테스트 포함) 확인.

2. **orphan 선재한계는 이 PR 블로커인가 별건 후속인가?** → **별건 후속(블로커 아님).**
   - 이유: (a) PR이 도입한 결함이 아니라 **선재 버그**(push 실패 시 로컬에 남는 orphan 커밋), (b) **동일-content 재시도에 한정** — content가 바뀌면 hash 변경으로 commit 성공→복구 가능, (c) 성공경로 무해. #14는 이 한계에 대해 중립적(악화시키지 않음)이며 저널 오염이라는 더 심각한 문제를 제거하는 순증.

### 두 리뷰 일치/불일치
**두 리뷰(심층검증·적대반증) 완전 일치 — 어긋남 없음.**
- guardCorrect=true, residualReal=true, residualSeverity=med, "가드는 순증·머지 안전, orphan 재시도 한계는 후속 명시 권고" 모두 동일 결론.
- 적대반증이 추가로 짚은 low 항목(신규 테스트가 res2의 기전·pushed 재도달을 검증 안 해 한계를 가림)도 심층검증의 "부수 관찰"과 정합. 상충 아니라 보강 관계.

### 잔존 한계 (med, 후속 처리)
push 1회 실패로 orphan 커밋이 남으면 → origin 복구 후 동일-content 재시도가 `_publish_pr`의 `git commit`에서 "nothing to commit"(exit1)→CalledProcessError→포괄 except 폴백으로 죽어 push에 영영 도달 못 함. 저널은 깨끗하나 기능적으로는 수동 git 정리 전까지 영구 미발행. 재현으로 origin refs empty 확인됨.

### 권고 액션
1. **#14 머지 진행** (순개선, 안전).
2. 첨부한 후속 이슈 등록 — `_publish_pr` 재진입 시 기존 HEAD 재push 분기 또는 `--allow-empty`, dry_run 표기 정합화, 재시도 pushed 재도달 검증 테스트 보강.

## 후속이슈 초안
**fix(studio): PR 발행 push 1회 실패 후 남는 orphan 커밋 → 동일-content 재시도가 'nothing to commit'으로 영구 미발행**

## 배경
PR #14(가드: `_publish_pr` 반환의 `pushed=False` 감지 시 저널 기록 생략 + `published:False` 반환)는 "push 실패인데 published=True로 기록되어 already_published no-op로 영구차단"되던 선재 결함을 정확히 해소했다. 저널 오염은 실제로 사라졌다.

그러나 저널을 깨끗하게 두는 것만으로는 "재시도 가능"이 절반만 달성된다. `_publish_pr`(adapters/studio_adapter.py)은 다음 순서로 동작한다:
1. `checkout -b publish/<slug>` (존재 시 checkout)
2. `_write_file` → `git add` → `git commit -m "publish: <slug>"`
3. `git push -u origin <branch>` (실패 시 예외 삼켜 `pushed=False`)

1차 시도에서 push만 실패하면 로컬 브랜치 `publish/<slug>`에 커밋이 그대로 남는다(orphan 커밋). 저널은 비어 있으므로 재시도는 already 차단을 피하지만, 2차 시도에서 동일-content(=동일 content_hash)라면 기존 브랜치 checkout → 동일내용 write → `git add` → `git commit`에서 **"nothing to commit"(exit 1) → CalledProcessError**가 발생하고, 이를 `publish()`의 포괄 except가 삼켜 `published:False, dry_run:True`로 폴백한다. push 라인에는 도달조차 못 한다.

## 재현
1. 실제 git repo(init+커밋), origin 없음 상태에서 `StudioAdapter(mode="pr").publish(approved=True)` 호출
   - 결과 R1: `published=False, dry_run=True, pr.pushed=False`, 저널 파일 미생성(가드 정상). 단 로컬 브랜치 `publish/<slug>`에 커밋 잔류.
2. bare repo를 origin으로 추가(=원격 장애 복구) 후 **동일 summary**로 재호출
   - 결과 R2: `published=False, dry_run=True, detail="실발행 실패 → 드라이런 폴백: CalledProcessError ... git commit ... exit status 1"`, `pr` 키 없음, `ls-remote origin` = empty
   - → origin이 살아있어도 commit 단계에서 죽어 push에 영영 도달 못 함 = 수동 git 정리(reset/amend/기존 HEAD push) 없이는 영구 미발행

## 부수 관찰
- `dry_run:True` 표기가 부정확: push 실패 응답은 dry_run으로 표기되지만 실제로는 로컬 브랜치 `publish/<slug>` + 커밋이 남은 상태(부작용 있음). dry_run을 "디스크/깃 무변경"으로 해석하는 소비자는 남겨진 orphan 브랜치를 인지 못 한다.
- 신규 회귀테스트 `test_pr_mode_push_fail_not_published_and_journal_clean`는 res2의 `published/already_published`만 검사해 "재시도 가능"으로 단정하나, 실제 res2는 nothing-to-commit 포괄예외 폴백이다. 테스트가 재시도 기전·pushed 재도달을 검증하지 않아 이 한계를 통과시킨다(잘못된 확신).

## 제안 수정
`_publish_pr` 재진입 경로를 견고화(택1 이상):
- **(A) 기존 HEAD 재push 분기**: 브랜치가 이미 존재하고 HEAD가 base_branch 대비 앞서 있으면(=발행 커밋 존재) commit 단계를 건너뛰고 곧장 `git push` 시도. 가장 자연스러운 재시도 시맨틱.
- (B) `git commit --allow-empty` 또는 commit 실패를 "nothing to commit"에 한해 무시하고 push로 진행.
- 응답 정합성: push 실패 시 `dry_run:True` 대신 `dry_run:False` + `side_effect:"local_branch_committed"` 같은 표기로 로컬 부작용을 명시(소비자 오해 방지).
- 테스트 보강: 재시도 시 res2가 실제 push 재도달(pushed=True 또는 origin refs 존재)에 도달하는지 검증하는 회귀테스트 추가(현재는 폴백을 성공으로 오판).

## 우선순위
med — PR #14 머지의 블로커 아님(선재 버그이고, content가 바뀌면 hash 변경→commit 성공으로 복구되며, 성공경로 무해). 다만 "일시적 원격 장애 후 같은 글 재발행"이라는 흔한 시나리오가 정확히 막히므로 무시 불가. #14 머지 직후 별건 후속으로 처리 권고.
