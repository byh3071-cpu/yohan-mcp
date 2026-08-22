---
vhk_format: 1
type: goal
id: 1
title: Graph-Aware Context Resolver Phase 1
status: DONE
priority: P0
completed: 2026-08-22
---

# Goal 1: Graph-Aware Context Resolver Phase 1

## 배경

사용자가 요한 생태계나 프로젝트 이름을 말하면 전체 저장소와 문서를 무차별로 읽지 않고,
질의에서 엔티티를 식별해 관련 그래프와 근거 청크만 제한된 예산 안에서 반환해야 한다.
현재 검색은 벡터 결과와 정적 링크를 결합하지만 프로젝트 별칭 자동 해석, 실제 1-hop 확장,
근거 부족 시에만 수행하는 보충 검색 계약이 없다.

## 범위

- 프로젝트·별칭·생태계 엔티티 자동 식별
- 식별된 엔티티의 1-hop 관계 확장
- 관계가 가리키는 근거 문서에 우선순위를 둔 제한 검색
- 결과 수·문자 예산 적용과 중복 제거
- 첫 검색의 근거가 부족할 때만 한 번의 보충 검색
- 요한 생태계·MOVA·yohan-mcp 골든 질의 회귀 테스트

## 범위 밖

- 공유 Qdrant 저장소 재시딩·쓰기
- 실행 중인 MCP 프로세스 중단·재시작
- 전체 지식 그래프 재설계 또는 다중-hop 추론
- yohan-brain·MOVA·control-tower 저장소 수정
- master 수정, push, merge, 배포

## 완료 조건

1. 프로젝트 이름이나 별칭이 포함된 질의에서 결정론적으로 엔티티 후보를 식별한다.
2. 식별된 엔티티의 직접 관계만 확장하며 확장 개수에 상한이 있다.
3. 관련 근거 청크가 우선되고 결과 개수와 총 문자 수가 설정된 예산을 넘지 않는다.
4. 충분한 근거가 있으면 보충 검색을 호출하지 않고, 부족할 때만 최대 한 번 호출한다.
5. 전체 문서 스캔 없이 주입된 검색·그래프 포트만으로 단위 테스트할 수 있다.
6. 골든 질의 3종과 기존 전체 테스트가 공유 Qdrant 접근 없이 통과한다.

### Phase 10
- [x] **Task 100** 현재 검색·트리플 계약을 고정하는 실패 테스트 작성 / evidence: tests/test_context_resolver.py

### Phase 20
- [x] **Task 200** 엔티티 해석·1-hop 확장·예산 적용 코어 구현 / evidence: core/context_resolver.py

### Phase 30
- [x] **Task 300** get_context 연결과 보충 검색 조건 구현 / evidence: core/tools.py
- [x] **Task 310** 생태계·MOVA·yohan-mcp 골든 질의 검증 / evidence: tests/test_context_resolver.py

### Phase 40
- [x] **Task 400** 전체 pytest·compileall·Goal 게이트 통과 / evidence: goal-check-1
