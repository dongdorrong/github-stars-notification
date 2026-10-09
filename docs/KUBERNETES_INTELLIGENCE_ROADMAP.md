# Kubernetes Ecosystem Intelligence Watcher — Roadmap

> Epic: #3  
> Architecture: [KUBERNETES_INTELLIGENCE_ARCHITECTURE.md](KUBERNETES_INTELLIGENCE_ARCHITECTURE.md)

P0(#4~#6)의 구현·운영 경계는 [P0 런북](P0_RUNBOOK.md)을 기준으로 한다. P1/P2(#7~#12)는 아래의 향후 계획이며 P0에서 구현하지 않는다.

## 1. 목표

기존 latest Release 기반 알림기를 다음 단계로 안전하게 확장한다.

```text
Release notification script
  -> reliable official-event collector
  -> Kubernetes ecosystem intelligence watcher
  -> local AI assisted operator digest
```

작업 우선순위는 기능 수보다 **이벤트 소실 방지와 상태 일관성**을 먼저 해결하도록 정한다.

## 2. 단계 요약

| Phase | 목표 | Issues | 완료 판단 |
| --- | --- | --- | --- |
| P0 | 상태·수집·실행 안정성 | #4, #5, #6 | Release를 놓치거나 Slack 실패로 소실하지 않음 |
| P1-A | 대상 프로젝트와 보안 신호 | #7, #8 | Kubernetes project와 GHSA를 공식 원천에서 추적 |
| P1-B | AI와 Slack routing | #9, #10 | AI 보조 분석과 Critical/High/Digest 전달 |
| P2 | 신호 확장과 운영 강화 | #11, #12 | Maintainer 공지, visibility, Knowledge, CI 강화 |

## 3. P0 — Reliable Event Delivery

### P0-1. Event Store / Outbox — #4

목표:

- raw event와 delivery state 분리
- pending event 누적
- Slack 성공 후 delivery 완료
- 실패 재시도

필수 결과물:

```text
state/event_store.py 또는 동등한 모듈
SQLite schema/migration
normalized event model
outbox state model
fixture tests
legacy cache migration
```

Gate:

- 4개 이벤트가 threshold 아래에서 대기하고 다음 실행 1개와 함께 처리됨
- Slack 실패 이벤트가 다음 실행에서 다시 후보가 됨
- Slack 성공 이벤트는 중복 전송되지 않음

### P0-2. Incremental Release Collector — #5

목표:

- `latest_release` 단건 수집 제거
- cursor 이후 모든 Release 수집
- GitHub Release ID 기반 stable identity

Gate:

- 실행 사이 3개 Release가 발생하면 3개 모두 event store에 존재
- pagination page 2 이후에도 unseen event를 찾음. 저장된 event를 만났다는 이유만으로 조기 중단하지 않음
- draft/prerelease metadata 보존

### P0-3. Preview / Concurrency / Bootstrap Safety — #6

목표:

- preview는 운영 state를 변경하지 않음
- schedule과 manual run이 동시에 state를 변경하지 않음
- cache miss가 대량 Slack 전송을 만들지 않음

Gate:

- preview 전후 state digest 동일
- workflow concurrency 적용
- safe first-run default
- explicit workflow permissions

### P0 권장 작업 순서

```text
1. characterization tests
2. event contract
3. SQLite store/outbox
4. legacy compatibility
5. incremental release collector
6. notification selection from pending outbox
7. preview/commit CLI semantics
8. workflow concurrency and permissions
9. README/runbook update
10. end-to-end fixture verification
```

### P0 완료 후 유지해야 할 호환성

- 기존 `config.yaml`의 `special_projects` 정책은 P0에서도 유지한다. project registry migration은 #7에서 다룬다.
- 기존 fixture CLI 사용자는 token 없이 계속 테스트할 수 있어야 한다.
- 기존 release feed 소비자는 호환 필드를 확인하고 새 pending/outbox 필드를 선택적으로 처리한다. 공개 feed로 배포하는 것은 #12 visibility 정책이 정해진 뒤 검토한다.
- schedule 시각은 의도적 변경이 아니면 유지한다.

## 4. P1-A — Kubernetes Projects and Security

### P1-A1. Project Registry / Classifier — #7

목표:

- 전체 starred repository 중 Kubernetes/Cloud Native 대상을 deterministic하게 식별
- project tier와 관심 signal 설정

Gate:

- 명시 registry override가 자동 분류보다 우선
- `AMBIGUOUS`만 AI 분류 후보
- private/visibility metadata 보존

### P1-A2. GHSA Collector — #8

목표:

- Repository/Global Security Advisory를 normalized event로 수집
- severity, CVSS, affected/patched version 보존

Gate:

- 동일 GHSA 중복 없음
- updated advisory 반영
- Critical/High는 AI 장애에도 deterministic priority 유지

## 5. P1-B — AI Advisory and Slack Routing

### P1-B1. Local LLM Analysis — #9

목표:

- 한국어 요약
- 운영 영향
- 카테고리
- 확인 권고

강제 경계:

- event state 변경 금지
- notification policy override 금지
- Slack 직접 전송 금지
- deterministic severity 하향 금지

Gate:

- schema-valid output만 저장
- endpoint down/invalid JSON에서 fallback
- 동일 content hash 분석 cache

### P1-B2. Critical / High / Digest — #10

목표:

- 보안/Breaking Change는 빠르게 전달
- 일반 Release는 digest로 누적
- Slack delivery acknowledgement

Gate:

- Critical 즉시 후보
- Digest 소실 없음
- 429/5xx 재시도
- mrkdwn mention 방지

## 6. P2 — Signal Expansion and Hardening

### P2-1. Maintainer Announcement — #11

대상:

- Discussions announcement
- maintainer/member labeled Issue
- official Blog/RSS

원칙:

- opt-in
- trust score 결정적 계산
- 일반 Issue/PR 전수 수집 금지

### P2-2. Visibility / Knowledge / CI — #12

목표:

- public artifact에 private metadata 기본 제외
- Release body/GHSA provenance를 Knowledge export에 포함
- dependency/CI 재현성 강화

Gate:

- private leak fixture 통과
- export side effect 없음
- PR CI에서 unit/security check 실행

## 7. Work Breakdown Structure

### Plan

- [x] 목표 아키텍처 정의
- [x] 상태·AI 경계 ADR 작성
- [x] Epic 및 하위 이슈 생성
- [x] P0 prerelease metadata 보존 및 Release 알림 포함 정책 결정
- [ ] P1 digest window 결정
- [ ] project tier 초기 목록 검토

### Build

- [x] P0 event/outbox 구현
- [x] incremental release collector 구현
- [x] preview/commit workflow 구현
- [ ] project registry/classifier 구현
- [ ] advisory collector 구현
- [ ] AI adapter/schema 구현
- [ ] Slack router/ack 구현

### Test

- [x] legacy behavior characterization
- [x] state transition tests
- [x] pagination tests
- [x] threshold accumulation tests
- [x] preview no-write tests
- [x] Slack retry tests
- [ ] private visibility tests
- [ ] AI schema/fallback tests

### Review

- [ ] raw event와 AI output이 분리됐는지 확인
- [ ] notifier가 state source of truth를 우회하지 않는지 확인
- [ ] first-run과 cache miss가 fail-safe인지 확인
- [ ] secret/private data가 artifact/log에 없는지 확인

### Document

- [x] architecture
- [x] roadmap
- [x] ADR
- [x] Codex UltraGoal
- [x] P0 migration/runbook
- [ ] event/feed schema reference
- [ ] local LLM setup/runbook

### Deploy

- [ ] fixture-only PR validation
- [ ] manual preview run
- [ ] schedule shadow comparison
- [ ] commit mode canary
- [ ] legacy cache retirement

## 8. AI-native 역할 분담

### Delegate

AI/Codex가 자동 처리 가능한 작업:

- 코드 구조 조사
- characterization tests 작성
- SQLite schema/migration 구현
- collector adapter 구현
- JSON schema와 fixture 생성
- workflow 수정
- 문서 동기화
- static analysis 및 regression test

### Review

사람이 결과를 확인해야 하는 작업:

- event state migration 결과
- Slack 메시지 내용과 우선순위
- project classification 오탐
- GHSA와 repository 연결 정확도
- AI 요약의 운영 영향 왜곡
- private metadata artifact 포함 여부

### Own

사용자가 직접 결정해야 하는 정책:

- Critical/High/Digest의 전달 시점
- prerelease 기본 수집 여부
- project tier와 관심 범위
- private repository 처리 방식
- 로컬 LLM 모델·하드웨어·비용
- 자동화가 실제 운영 환경에 조치까지 연결될지 여부

## 9. 단기 Codex 범위

단일 UltraGoal에서 우선 처리할 범위:

```text
IN SCOPE
- #4 event/outbox state
- #5 incremental releases
- #6 preview/concurrency safety
- 관련 tests/docs/workflow

OUT OF SCOPE
- #7 project registry/classifier
- #8 live GHSA collector
- #9 production local LLM
- #10 full Slack redesign
- #11 announcement collectors
- #12 전체 CI/supply-chain 개편
```

P0는 상태 모델 변경 폭이 크므로 한 번에 P1까지 얹지 않는다. Codex는 P0를 완결된 vertical slice로 종료해야 한다.

## 10. 릴리스 전략

### Stage 1 — Fixture only

- 외부 API/Slack 없이 전체 상태 전이 검증
- 기존 캐시 fixture migration

### Stage 2 — Preview

- 실제 Starred Repository와 Release API 조회
- 운영 state/Slack write 없음
- old/new detector 결과 비교

### Stage 3 — Shadow Commit

- 신규 DB에 state 저장
- Slack은 기존 경로 유지하거나 비활성
- 누락/중복 비교

### Stage 4 — Canary Delivery

- 관심 프로젝트 일부만 새 outbox/notifier 사용
- 실패 retry 확인

### Stage 5 — Full Cutover

- 새 event/outbox가 source of truth
- legacy releases cache write 종료
- rollback 문서 확인

## 11. Rollback

P0 PR은 다음 rollback을 가능하게 해야 한다.

- legacy `check_release.py` 동작을 tag/commit으로 즉시 복구
- SQLite DB는 별도 `.cache/events.sqlite3` 파일로 유지
- migration은 원본 `.cache/releases.json`을 삭제하지 않음
- 수동 preview와 schedule commit mode는 명시적 flag/workflow input으로 구분
- Slack notifier cutover 전에 old/new 결과 비교 report 보존

**운영 rollback 주의:** legacy `.cache/releases.json`은 P0 이후 갱신되지 않으므로 이전 workflow를 그대로 재가동하면 오래된 cache 기준으로 중복 Slack 전송이 생길 수 있다. 먼저 새 전송을 멈추고 DB/캐시 및 전달 이력을 백업·대조한 뒤 사람이 전환 여부를 결정한다. Actions cache는 영구 DB가 아니며 stale restore/eviction/save 실패 또는 Slack 성공 직후 DB 반영 실패로 exactly-once를 보장하지 못한다.

## 12. 완료 보고 형식

각 이슈 완료 시 다음 증적을 남긴다.

```text
Issue:
Commit/PR:
Changed files:
Behavior before:
Behavior after:
Tests:
Live side effects performed:
Migration/rollback:
Residual risks:
```
