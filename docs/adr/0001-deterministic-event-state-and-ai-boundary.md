# ADR-0001: Deterministic Event State and AI Advisory Boundary

- Status: Accepted for implementation
- Date: 2026-10-09
- Epic: #3
- Related: #4, #5, #6, #9, #10

## Context

현재 프로젝트는 starred repository의 latest Release를 조회하고 `.cache/releases.json`과 비교한 뒤 Slack payload를 만든다. 이 구조는 단순하지만 다음 문제가 있다.

1. 알림 임계값 미만의 Release도 cache에 즉시 기록되어 다음 실행에 누적되지 않는다.
2. Slack 전송은 별도 job에서 수행되므로 cache가 먼저 갱신된 뒤 Slack이 실패하면 해당 이벤트가 재시도되지 않는다.
3. `workflow_dispatch` preview 성격의 실행도 운영 cache를 변경할 수 있다.
4. `get_latest_release()`는 실행 사이에 발생한 중간 Release를 놓친다.
5. 향후 LLM이 중요도와 요약을 생성할 때 상태 판단과 자연어 판단의 책임이 섞일 위험이 있다.

이 프로젝트는 개인 자동화이지만 보안·Breaking Change 정보의 유실 여부가 중요하다. 따라서 notification pipeline은 최소한 at-least-once delivery에 가까운 상태 모델과 명확한 AI 경계를 가져야 한다.

## Decision

### 1. Normalized event를 source of truth로 사용한다

Collector는 GitHub API의 각 원본을 stable ID를 가진 normalized event로 변환한다.

```text
GitHub Release: github:release:<release_id>
GitHub Advisory: github:ghsa:<GHSA-ID>
Discussion: github:discussion:<discussion_id>
Issue: github:issue:<repository_id>:<issue_number>
Feed item: feed:<source_id>:<entry_id-or-content-hash>
```

표시 순서나 tag 문자열만으로 event identity를 만들지 않는다.

### 2. 원본 event와 notification outbox를 분리한다

- `events`: 원본/normalized event와 provenance
- `notification_outbox`: 전달 우선순위와 전달 상태
- `ai_analyses`: AI 분석 결과

하나의 JSON object에 원본, AI 결과, 전달 상태를 혼합하지 않는다.

### 3. Slack 성공 전에는 delivered로 처리하지 않는다

상태 전이:

```text
DISCOVERED
  -> PENDING_NOTIFICATION
  -> DELIVERY_IN_PROGRESS
  -> DELIVERED | DELIVERY_FAILED
```

Slack 2xx 응답을 확인한 경우만 `DELIVERED`로 변경한다. Timeout, 429, 5xx는 `DELIVERY_FAILED`와 재시도 metadata를 남긴다.

### 4. Preview 실행은 운영 state를 변경하지 않는다

수동 preview는 다음 side effect를 금지한다.

- event/outbox DB 변경
- legacy cache 변경
- last notification 변경
- Slack 전송

Preview는 report/Step Summary/artifact만 생성한다.

### 5. AI는 advisory layer다

AI가 할 수 있는 일:

- 한국어 요약
- 운영 영향 초안
- 카테고리 분류
- 사람이 확인할 항목 추천
- 애매한 ecosystem 분류의 보조 의견

AI가 할 수 없는 일:

- 신규/중복 event 판정
- stable ID 생성 규칙 변경
- event/outbox 상태 변경
- Slack 직접 호출
- notification policy override
- deterministic security severity 하향
- GitHub/cluster/tool write 실행

### 6. Deterministic policy가 최소 우선순위를 보장한다

예를 들어 Critical GHSA나 RCE/auth bypass/privilege escalation 신호는 AI 실패 또는 낮은 confidence와 무관하게 최소 `CRITICAL`을 유지한다.

AI는 운영 중요도를 상향 제안할 수 있지만 하향은 policy가 허용하는 범위 내에서만 참고한다.

### 7. 1차 state store는 SQLite다

이유:

- 단일 사용자·단일 workflow 규모에 적합
- atomic transaction
- schema migration
- fixture/local test 편의성
- GitHub Actions cache/artifact로 보존 가능
- 향후 Mac mini pull worker로 이동하기 쉬움

PostgreSQL은 동시 worker 또는 웹 UI가 실제로 필요할 때 검토한다.

## Consequences

### Positive

- 임계값 미만 event가 소실되지 않는다.
- Slack 실패를 재시도할 수 있다.
- 수동 preview가 운영 상태를 오염시키지 않는다.
- Release/GHSA/Announcement를 같은 event pipeline으로 처리할 수 있다.
- LLM 장애가 수집과 전달 상태를 망가뜨리지 않는다.
- Knowledge export에서 raw event와 AI summary를 구분할 수 있다.
- 테스트 가능한 상태 머신이 생긴다.

### Negative

- 기존 단일 스크립트보다 구조와 migration이 복잡해진다.
- SQLite DB lifecycle과 GitHub Actions cache 보존을 관리해야 한다.
- Slack 전송 결과를 state에 반영하기 위해 workflow/job 구조를 조정해야 한다.
- legacy `.cache/releases.json`과 일정 기간 호환이 필요하다.
- 정확한 retry/lease semantics를 구현해야 한다.

### Risks

- GitHub Actions cache는 영구 데이터베이스가 아니므로 eviction 가능성이 있다.
- 병렬 실행이 DB를 경쟁적으로 변경할 수 있으므로 workflow concurrency가 필요하다.
- malformed DB에서 자동으로 새 DB를 덮어쓰면 과거 state를 잃을 수 있다.
- Slack 전송 후 DB commit 전에 process가 종료되면 중복 전송 가능성이 있다.

마지막 위험은 at-least-once delivery의 허용 결과다. 중요한 이벤트 유실보다 드문 중복 전달을 우선 허용하고, message/event ID 표시와 상태 lease로 중복 가능성을 줄인다.

## Alternatives Considered

### A. 기존 JSON cache 유지

장점:

- 구현이 단순함

거절 이유:

- atomic multi-state transition이 어려움
- pending/delivery retry/query가 복잡함
- migration/versioning이 취약함
- preview와 운영 state 분리가 불명확함

### B. Slack 전송 전에 cache를 저장하지 않음

장점:

- 현재 구조 변경이 작음

거절 이유:

- threshold 미만 pending 누적 모델을 해결하지 못함
- 여러 event와 여러 Slack payload의 부분 성공을 표현하기 어려움
- Release/GHSA 등 복수 event type 확장에 취약함

### C. LLM이 중요 이벤트를 직접 결정

장점:

- 규칙 관리가 줄어 보임

거절 이유:

- 비결정적이고 회귀 검증이 어려움
- prompt injection과 모델 변경에 따라 전달 여부가 변할 수 있음
- 보안 severity를 안정적으로 보장하지 못함
- API 장애가 notification pipeline을 중단시킴

### D. 즉시 PostgreSQL 도입

장점:

- 동시성·조회·서비스 확장에 유리함

거절 이유:

- 현재 개인 GitHub Actions 자동화 규모에 과도함
- 운영 비용과 secret/network 관리가 추가됨
- SQLite로 필요한 상태 의미론을 충분히 검증할 수 있음

## Implementation Constraints

- 외부 API를 사용하지 않는 fixture tests가 먼저 있어야 한다.
- migration은 기존 cache 원본을 삭제하지 않는다.
- preview mode가 기본적으로 side-effect free임을 테스트한다.
- workflow에 `concurrency`와 명시적 최소 permissions를 적용한다.
- event body/title은 untrusted input으로 처리한다.
- secret/private metadata를 로그와 public artifact에 남기지 않는다.

## Validation

ADR이 구현됐다고 판단하는 최소 조건:

1. #4 수용 기준 통과
2. #5 다중 Release fixture 통과
3. #6 preview digest 불변 테스트 통과
4. Slack 실패 후 재시도 테스트 통과
5. LLM endpoint down 상태에서 deterministic pipeline 통과
6. raw event, outbox, AI analysis가 물리적으로 분리됨

## Follow-up

- #7 Project Registry
- #8 Security Advisory Collector
- #9 Local LLM Analysis
- #10 Slack Routing/Acknowledgement
- #11 Maintainer Announcements
- #12 Visibility/Knowledge/CI hardening
