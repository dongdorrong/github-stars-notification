# P1/P2 Intelligence 단계별 운영·롤백

> 이 문서는 **준비 절차**다. 이번 feature branch 작업에서 main commit, 운영 Slack, 실 LLM 호출, PR 병합을 실행하지 않는다. 원격 preview와 PR CI 증거는 최종 HEAD에서 실제 성공 확인 전까지 **미검증**이다. P0 Release 운영 복구는 [P0_RUNBOOK.md](P0_RUNBOOK.md)를 함께 따른다.

## 현재 구성과 권한

- `config.yaml`의 `intelligence.mode` 기본은 `shadow`, `analysis.enabled` 기본은 `false`, 새 announcement 신호는 registry에서 opt-in 전까지 꺼져 있다. `routing.destination_visibility`는 설정된 Slack 수신 대상의 `public|private` 범위를 뜻한다. 실제 webhook의 접근 제어는 운영자가 확인해야 한다.
- `.github/workflows/notify-starred-releases.yml`은 기존 UTC `23:00/05:00/08:00` schedule, 기본 브랜치 commit gate, concurrency, 30분 job timeout을 유지한다. feature branch `workflow_dispatch(mode=preview)`만 허용되는 안전 검증이다. Preview는 메모리 DB, Slack secret 미주입, Event DB cache save skip이다. 원격 로그에서 각 step이 실제로 skip됐는지 별도 확인한다.
- Release collector는 최근 일반/특수 최대 3/5 page, bootstrap 1 page와 점진 reconciliation, 전역 900초 예산을 유지한다. 새로운 signal collector는 source별 최대 3 page, 별도 120초 예산과 page-atomic cursor를 사용한다. 예산/실패 시 미처리 source를 다음 실행에 남기며 모든 과거 이벤트를 한 실행에서 찾는다고 주장하지 않는다.
- `GH_PAT`는 starred inventory와 접근 대상의 read 권한만 주고 Slack webhook은 commit step에만 주입한다. Global GHSA는 공개 읽기 source다. Repository GHSA는 기본 비활성이고 접근 거부 시 해당 source만 degrade한다. Discussion/Issue/RSS는 registry의 명시적 공개 opt-in에만 의존한다. API 권한 표는 [Security Advisory](SECURITY_ADVISORY_RUNBOOK.md), [Announcement](MAINTAINER_ANNOUNCEMENT_SOURCES.md) 문서와 아래 공식 링크를 확인한다.
- Python 3.12, SHA-pinned Actions, 해시 고정 requirements, 별도 PR CI를 유지한다. 정확한 CI job 이름·결과는 PR에서 확인한다.

## 실행 전 필수 백업·검증

1. 운영 `.cache/events.sqlite3`와 `.cache/releases.json`을 **별도 비공개 위치에 복사**하고 checksum, `PRAGMA user_version`, event/outbox 상태별 수를 기록한다. `.cache/releases.json`은 수정·삭제하지 않는다. GitHub Actions cache가 영구 백업이 아니라는 점을 전제로 한다.
2. 기존 v1 DB의 `quick_check`, FK, 필수 schema 검증이 실패하면 중단한다. 손상 DB를 새 빈 DB로 대체하지 않는다.
3. feature branch preview, fixture 전체 테스트, schema/registry/lock 검증, PR CI를 정확한 HEAD에서 완료하고 로그의 private 이름·비밀값 부재를 확인한다. Preview의 Release/GHSA/announcement pages, deferred/errors, classification, AI fallback/cache, route counts, visibility exclusion을 보고한다. 아직 수행 전에는 녹색으로 가정하지 않는다.
4. `config/projects.yaml`에서 명시 tier/signal/alias/package map 및 starred inventory의 visibility를 확인한다. `unknown`은 public export에서 제외된다.

## 단계 1 — main preview (향후 승인 필요)

`workflow_dispatch(mode=preview)`만 사용한다. 상태 저장, Slack, live LLM을 하지 않는다. 과거 DB를 읽더라도 RAM 복사본에서 v2 migration을 시뮬레이션할 뿐 원본 바이트를 변경하지 않는다. Preview 성공은 API 가용성과 정책 보고 증거이지 운영 전달 성공 증거가 아니다.

**중단/롤백:** preview 실패 시 설정·권한·source 단독 장애를 fixture로 재현하고 수정한다. 원본 DB를 복구할 작업은 없어야 한다. Event DB Save 또는 Slack step 실행이 관측되면 즉시 rollout을 중단한다.

## 단계 2 — shadow commit (향후 승인 필요)

기본 `intelligence.mode: shadow`로 첫 main schedule/commit을 수행한다. 첫 성공 commit에서 SQLite **schema v1→v2**를 검증 후 transaction으로 migration한다. v2는 `event_revisions`, `ai_analyses`, `routing_decisions`, `delivery_attempts`와 outbox `suppression_reason`을 추가하며 v1 event/ack/history를 보존한다. GHSA와 announcement는 공식 source 이벤트·revision·cursor·fallback·route 평가를 저장하되 `rollout_shadow`로 새 signal Slack을 억제한다. 기존 P0 Release 임계값·special project·ack 경로를 유지한다. 첫 GHSA/source cohort도 bootstrap 기준선으로 suppress한다.

관측: v1/v2 row count 비교, malformed state 거부, Release 4+1·retry 지속, source별 safe error/cursor, unmapped GHSA 억제, announcement trust, AI fallback/cache, route counts, unknown/private exclusion. `SUPPRESSED`는 실제 전달이 아니며 나중에 모드를 바꿔도 자동 대량 재전송이 일어나지 않는지 검토한다.

**중단/롤백:** `intelligence.mode: shadow`를 유지하고 source enable flag를 끄면 새 수집/전송 확대를 멈춘다. **P0 코드가 schema v2 DB를 열 수 없으므로** 단순 코드 revert는 안전한 롤백이 아니다. 정확한 migration 전 v1 백업을 복원하려면 그 이후 v2에서 생성된 event/outbox/ack을 먼저 감사·별도 보존하고 Slack 중복/누락을 수동 대조해야 한다. 백업 없이 v2를 v1로 다운그레이드하거나 빈 DB를 생성하지 않는다. 필요하면 v2를 읽는 수정 버전에서 shadow로 계속 운영한다.

## 단계 3 — canary (향후 승인 필요)

`intelligence.mode: canary`, 소수의 **명시적** `canary_projects`만 지정한다. 가능하면 운영 webhook과 분리된 전용 테스트 webhook 및 제한된 visibility destination을 사용한다. Critical/High/Digest formatter의 event ID, 공식 링크, 결정적 floor, 한국어 fallback/AI 요약, 2xx ack, 429 Retry-After, 5xx/timeout retry, 부분 chunk 성공을 사람이 검토한다. Private/unknown content를 public destination으로 보내지 않는다.

**중단/롤백:** mode를 `shadow`로 되돌리고 canary allowlist를 비운다. 이미 `DELIVERED`인 ack을 삭제하거나 되돌리지 않는다. 실패 retry row는 재전송 계획을 세우기 전까지 보존한다.

## 단계 4 — full (향후 승인 필요)

`intelligence.mode: full`은 승인된 project/signal/visibility만 활성화한다. CRITICAL/HIGH는 즉시 후보, DIGEST는 5개/최대 24시간/KST 17시 중 하나가 충족될 때 후보다. 실패 retry는 digest window를 기다리지 않는다. 최소 한 schedule cycle 동안 Release 상태 복원·저장, 신규/수정 GHSA, announcement cursor, ack/retry, collector budget, source errors, artifact/secret 경계를 점검한다. 실제 전송은 at-least-once이며 exactly-once 보장이 아니다.

**중단/롤백:** 먼저 `shadow` 또는 `canary`로 범위를 줄이고 source별 flag를 끈다. DB v2와 delivery audit을 보존한 채 실패 원인을 고친다. 운영 DB 복원은 위 v1 백업 주의와 Slack ack 대조 없이 진행하지 않는다.

## 단계 5 — Epic #3 종료 (향후 승인 필요)

PR 병합만으로 #3을 닫지 않는다. main shadow persistence, canary, 운영자 수용, 공식 signal→정책→분석→Slack 2xx ack가 안전하게 확인된 뒤 이슈 체크리스트와 운영 evidence를 갱신한다.

## API 계약과 원문 증거

Adapter는 GitHub REST `X-GitHub-Api-Version: 2022-11-28`을 보낸다. Global GHSA `GET /advisories`는 `modified` 필터와 pagination, Repository GHSA `GET /repos/{owner}/{repo}/security-advisories`는 별도 권한/능력에 의존한다. Issues는 `GET /repos/{owner}/{repo}/issues`의 `since`, `sort=updated`, Link pagination을 사용한다. Discussions는 읽기 전용 GraphQL query의 `UPDATED_AT` 순서와 `pageInfo` cursor를 쓴다. GitHub 403은 권한 또는 rate limit일 수 있어 안전 범주로 구분하고 raw body/URL/토큰을 기록하지 않는다. Slack incoming webhook은 HTTP 429의 `Retry-After`(초)를 존중한다. 현재 공식 문서: [Global GHSA](https://docs.github.com/en/rest/security-advisories/global-advisories?apiVersion=2022-11-28), [Repository GHSA](https://docs.github.com/en/rest/security-advisories/repository-advisories?apiVersion=2022-11-28), [REST Issues](https://docs.github.com/en/rest/issues/issues?apiVersion=2022-11-28), [GraphQL Discussions](https://docs.github.com/en/graphql/guides/using-the-graphql-api-for-discussions), [REST pagination](https://docs.github.com/en/rest/using-the-rest-api/using-pagination-in-the-rest-api), [Slack rate limits](https://docs.slack.dev/apis/web-api/rate-limits/).

## 현재 미검증 사항

- 이 feature branch의 **최종 HEAD** 원격 preview/PR CI 결론과 수치는 별도 evidence가 아직 필요하다. 실행되지 않은 stage 1~5를 완료로 표기하지 않는다.
- 선택적 Repository GHSA, opt-in Discussion/Issue/RSS의 live 권한·응답·운영 지연은 fixture만으로 보장할 수 없다. 공개 preview에서 해당 source가 꺼져 있으면 특히 live capability는 미검증이다.
- 실제 로컬 LLM endpoint 품질·가용성 및 운영 Slack ack는 안전 제어 때문에 이번 작업에서 실행하지 않는다. 허가된 stage 3에서만 검증한다.
- GitHub Actions cache eviction, source API 정렬/수정 지연, 아카이브 이동·rename, private destination 실제 접근 제어는 운영 잔여 위험이다.
