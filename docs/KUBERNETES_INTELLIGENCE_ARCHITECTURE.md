# Kubernetes Ecosystem Intelligence Watcher — Architecture

> Status: P0 Release 경로 구현, P1/P2 설계안
> Updated: 2026-10-09  
> Epic: #3

## 1. 목적

이 프로젝트를 단순한 GitHub starred repository 최신 Release 알림기에서 다음 목적을 가진 개인용 Technology Intelligence Watcher로 확장한다.

> 사용자가 실무에서 사용하는 Kubernetes/Cloud Native 생태계 프로젝트의 공식 Release, Security Advisory, Maintainer Announcement를 수집하고, 결정적 정책과 선택적 AI 분석을 거쳐 필요한 정보만 Slack으로 전달한다.

핵심 사용자는 Kubernetes, EKS/AKS, Argo CD, Karpenter, Istio, Prometheus/Grafana, CNI/CSI, etcd 등 운영 생태계를 추적하는 DevOps/Platform/SRE 엔지니어다.

## 2. 설계 원칙

1. **원본 이벤트가 우선이다.** GitHub Release, GHSA, Discussion, Issue 등의 원문과 provenance를 먼저 보존한다.
2. **상태와 전달 판단은 결정적 코드가 소유한다.** LLM은 신규/중복 판정, event 상태 변경, Slack 전송 여부를 결정하지 않는다.
3. **알림은 소실되지 않아야 한다.** 임계값 미만 이벤트도 pending 상태로 누적하고 Slack 성공 전에는 delivered로 처리하지 않는다.
4. **공식 신호를 우선한다.** Advisory와 Release가 최상위 신뢰도를 가지며 일반 contributor의 Issue/PR은 기본 수집 대상이 아니다.
5. **AI는 보조 계층이다.** 한국어 요약, 운영 영향, 분류, 확인 권고를 생성하지만 deterministic severity와 policy를 덮어쓰지 않는다.
6. **공개 범위를 명시한다.** private/internal repository 정보는 public artifact와 Knowledge export에서 기본 제외한다.
7. **운영 경로와 실험 경로를 분리한다.** GitHub Actions 운영 경로는 결정적이고 재현 가능해야 하며, 로컬 LLM/Codex 실험 실패가 collector를 중단시키지 않는다.

## 3. 범위

### 구현 단계 구분

P0(#4~#6)는 GitHub Release ID 기반 수집, SQLite event/outbox, pending 누적, Slack 응답 확인 후 acknowledgement, preview/commit 분리를 구현한다. 아래 registry, GHSA, announcement, AI 분석, Critical/High/Digest routing, visibility/Knowledge 강화는 #7~#12의 **목표 설계**이며 P0 운영 기능이 아니다. 구현된 정확한 CLI/상태 경계는 [P0 런북](P0_RUNBOOK.md)을 따른다.

### 포함

- Starred repository inventory
- Kubernetes/Cloud Native ecosystem 분류
- 전체 unseen GitHub Release incremental 수집
- GitHub Security Advisory/GHSA 수집
- 선택적 maintainer announcement 수집
- durable event/outbox state
- deterministic priority policy
- OpenAI-compatible local LLM 분석
- Critical/High/Digest Slack routing
- read-only Knowledge JSONL export

### 제외

- 자동 dependency upgrade 또는 cluster patch
- GitHub Issue/PR에 자동 댓글 작성
- X/Twitter, Reddit 등 비공식 SNS 전수 수집
- LLM이 notification state 또는 GitHub/Slack write를 직접 수행하는 구조
- 1차 구현에서의 PostgreSQL 운영 서비스

## 4. 상위 아키텍처

```text
GitHub Starred Repositories
          │
          ▼
┌──────────────────────────────┐
│ Inventory Collector          │
│ repo metadata + visibility   │
└──────────────┬───────────────┘
               ▼
┌──────────────────────────────┐
│ Project Registry / Classifier│
│ tier, category, signals      │
└──────────────┬───────────────┘
               ▼
┌──────────────────────────────────────────────────────────┐
│ Official Signal Collectors                               │
│  - GitHub Releases                                       │
│  - GitHub Security Advisories / GHSA                     │
│  - Discussions / maintainer Issues / official RSS        │
└──────────────┬───────────────────────────────────────────┘
               ▼
┌──────────────────────────────┐
│ Normalizer                   │
│ stable IDs + provenance      │
└──────────────┬───────────────┘
               ▼
┌──────────────────────────────┐
│ Event Store + Outbox         │
│ SQLite, atomic state         │
└──────────────┬───────────────┘
               ├─────────────────────────────────────┐
               ▼                                     ▼
┌──────────────────────────────┐       ┌────────────────────────────┐
│ Deterministic Policy Engine  │       │ AI Advisory Analyzer       │
│ priority + routing           │       │ summary/impact/categories  │
└──────────────┬───────────────┘       └─────────────┬──────────────┘
               └──────────────────────┬───────────────┘
                                      ▼
                         ┌────────────────────────────┐
                         │ Slack Notifier             │
                         │ Critical / High / Digest   │
                         └─────────────┬──────────────┘
                                       ▼
                         ┌────────────────────────────┐
                         │ Delivery Ack / Retry       │
                         └────────────────────────────┘

Event Store ── read-only export ──> fordongdorrong Knowledge Store
```

## 5. 컴포넌트

### 5.1 Inventory Collector

현재 `gh api /user/starred --paginate` 경로를 유지하되 다음 metadata를 포함한다.

```text
full_name
node_id / repository_id
description
html_url
language
topics
archived
disabled
fork
private
visibility
pushed_at
updated_at
stargazers_count
open_issues_count
```

`private`와 `visibility`는 artifact/export 경계 판단에 필수다.

출력:

```text
.cache/stars-inventory.json
.cache/stars-inventory.jsonl
repos.txt
```

### 5.2 Project Registry / Ecosystem Classifier

명시적 project registry가 자동 분류보다 우선한다.

예시:

```yaml
projects:
  kubernetes/kubernetes:
    tier: critical
    categories: [kubernetes, api, security]
    signals:
      release: true
      advisory: true
      announcement: true
      prerelease: false

  kubernetes-sigs/karpenter:
    tier: critical
    categories: [kubernetes, autoscaling, aws]
    signals:
      release: true
      advisory: true
      breaking_change: true
      announcement: true
```

자동 분류 입력:

- GitHub owner: `kubernetes`, `kubernetes-sigs`, CNCF ecosystem owners
- topics: `kubernetes`, `cloud-native`, `cncf`, `operator`, `cni`, `csi`, `gitops`
- repository name/description keywords
- known project registry overrides

자동 분류 결과:

```text
CONFIRMED_KUBERNETES
NOT_KUBERNETES
AMBIGUOUS
```

`AMBIGUOUS`만 선택적으로 LLM 보조 분류를 허용한다. 보조 결과는 registry override를 변경하지 않는다.

### 5.3 Release Collector

`get_latest_release()` 대신 Release 목록을 incremental하게 수집한다.

식별 규칙:

```text
source_id = github:release:<github_release_id>
event_id  = github:release:<github_release_id>
```

수집 필드:

```text
release_id
repository
tag_name
release_name
body
html_url
published_at
created_at
updated_at
draft
prerelease
author
```

P0 collector는 page 1에서 멈추거나 최신 Release 하나만 읽지 않는다. 각 repository의 목록을 마지막 page까지 조회한 뒤 저장된 Release ID 및 legacy 기준선과 비교한다. API 정렬·페이지 이동에 안전한 완료 cursor가 증명되지 않은 상태에서 이미 본 Release 하나를 만나도 조기 중단하지 않는다. 수집 순서가 바뀌어도 동일 Release ID는 동일 event다.

### 5.4 Security Advisory Collector

대상:

- Repository Security Advisory
- 대상 project/package와 연결된 Global Security Advisory

식별 규칙:

```text
source_id = github:ghsa:<GHSA-ID>
event_id  = github:ghsa:<GHSA-ID>
```

수집 필드:

```text
ghsa_id
cve_id
repository/package ecosystem
severity
cvss vector/score
cwe
summary
description
affected_versions
patched_versions
published_at
updated_at
withdrawn_at
references
```

Advisory가 수정되면 같은 event ID를 유지하면서 원본 revision/content hash를 갱신한다.

Critical/High advisory의 최소 우선순위는 deterministic policy가 부여한다. AI는 이를 하향 조정할 수 없다.

### 5.5 Maintainer Announcement Collector

P2에서 opt-in 방식으로 도입한다.

대상 후보:

- GitHub Discussions announcement category
- maintainer/member가 작성한 labeled Issue
- 공식 프로젝트 Blog/RSS

초기 trust score:

| Source | Score |
| --- | ---: |
| Repository Security Advisory | 100 |
| Official GitHub Release | 100 |
| Official organization blog/RSS | 95 |
| Discussion announcement | 90 |
| Maintainer-authored labeled Issue | 85 |
| Maintainer-authored PR | 75 |
| Organization member Issue | 65 |
| General contributor Issue | 30 |

일반 contributor의 unlabeled Issue는 기본 Slack 후보가 아니다.

### 5.6 Normalized Event

Collector별 원본 차이를 다음 공통 envelope로 감싼다.

```json
{
  "schema_version": "k8s-intelligence-event/v1",
  "event_id": "github:release:123456",
  "event_type": "github_release",
  "source": "github",
  "source_id": "github:release:123456",
  "repository": "kubernetes-sigs/karpenter",
  "project_tier": "critical",
  "categories": ["kubernetes", "autoscaling", "aws"],
  "source_trust": 100,
  "title": "v1.2.3",
  "body": "...",
  "url": "...",
  "published_at": "2026-10-09T00:00:00Z",
  "updated_at": "2026-10-09T00:00:00Z",
  "visibility": "public",
  "content_hash": "sha256:...",
  "provenance": {
    "collector": "github-releases",
    "api_resource_id": 123456
  },
  "metadata": {}
}
```

원본 body는 Slack text와 분리한다. Slack formatting을 위해 원본을 변경하지 않는다.

### 5.7 Event Store / Outbox

P0 구현은 `.cache/events.sqlite3`의 SQLite `events`, `notification_outbox`, `state_metadata`를 사용한다. 아래 SQL과 `ai_analyses`는 목표 아키텍처의 예시이며 실제 P0 schema는 `starwatch/event_store.py`를 기준으로 한다. AI 분석 테이블은 #9 범위다.

권장 테이블:

```sql
CREATE TABLE events (
    event_id            TEXT PRIMARY KEY,
    event_type          TEXT NOT NULL,
    repository          TEXT,
    source_id           TEXT NOT NULL,
    source_trust        INTEGER NOT NULL,
    visibility          TEXT NOT NULL,
    published_at        TEXT NOT NULL,
    first_seen_at       TEXT NOT NULL,
    last_seen_at        TEXT NOT NULL,
    content_hash        TEXT NOT NULL,
    payload_json        TEXT NOT NULL
);

CREATE TABLE notification_outbox (
    event_id            TEXT PRIMARY KEY,
    priority            TEXT NOT NULL,
    notification_state  TEXT NOT NULL,
    notify_attempts     INTEGER NOT NULL DEFAULT 0,
    first_queued_at     TEXT NOT NULL,
    next_attempt_at     TEXT,
    notified_at         TEXT,
    last_error          TEXT,
    FOREIGN KEY(event_id) REFERENCES events(event_id)
);

CREATE TABLE ai_analyses (
    event_id            TEXT NOT NULL,
    content_hash        TEXT NOT NULL,
    model               TEXT NOT NULL,
    prompt_version      TEXT NOT NULL,
    schema_version      TEXT NOT NULL,
    analysis_json       TEXT NOT NULL,
    analyzed_at         TEXT NOT NULL,
    PRIMARY KEY(event_id, content_hash, model, prompt_version)
);
```

목표 notification state:

```text
DISCOVERED
PENDING_NOTIFICATION
DELIVERY_IN_PROGRESS
DELIVERED
DELIVERY_FAILED
SUPPRESSED
```

상태 전이:

```text
DISCOVERED
   ├─ policy suppress ───────────────> SUPPRESSED
   └─ policy queue ──────────────────> PENDING_NOTIFICATION
                                         │
                                         ▼
                                   DELIVERY_IN_PROGRESS
                                      │           │
                               HTTP 2xx│           │timeout/429/5xx
                                      ▼           ▼
                                  DELIVERED   DELIVERY_FAILED
                                                   │
                                                   └─ retry ─> PENDING_NOTIFICATION
```

### 5.8 Deterministic Priority Engine

우선순위는 다음 신호를 조합한다.

- event type
- advisory severity/CVSS
- project tier
- source trust
- deterministic keywords
- draft/prerelease policy
- AI advisory result

AI는 deterministic minimum priority를 낮출 수 없다.

초기 규칙 예시:

```text
Critical/High GHSA + critical/high project        CRITICAL
RCE/auth bypass/privilege escalation              CRITICAL
Breaking API/CRD schema/default config change     HIGH
Deprecated API removal/migration required         HIGH
Kubernetes version support change                 HIGH or MEDIUM
Maintenance/security patch without known impact   MEDIUM
General feature/performance/docs                  DIGEST
Ignored prerelease/docs-only                      SUPPRESSED
```

### 5.9 AI Advisory Analyzer

지원 인터페이스:

```text
OpenAI-compatible /chat/completions
```

기본 운영 목적은 로컬 LLM이다. Codex는 구현·테스트와 중요한 이벤트의 deep analysis에 사용하고, 매 이벤트 정기 분류의 기본 실행기는 아니다.

출력 계약:

```json
{
  "schema_version": "k8s-intelligence-analysis/v1",
  "event_id": "github:release:123456",
  "summary_ko": "...",
  "impact": "none|low|medium|high|critical",
  "categories": ["breaking-change", "upgrade"],
  "operator_attention": true,
  "reason": "...",
  "recommended_actions": ["..."],
  "confidence": 0.92,
  "model": "local/model-name",
  "prompt_version": "v1",
  "truncated_input": false
}
```

강제 경계:

- 원본 body는 untrusted data로 구분한다.
- system instruction과 JSON schema는 애플리케이션이 소유한다.
- invalid JSON은 저장하지 않고 fallback analyzer로 전환한다.
- AI endpoint 장애는 event 수집과 outbox를 실패시키지 않는다.
- 동일 content hash/model/prompt version은 재분석하지 않는다.

### 5.10 Slack Notifier

P1 #10 목표 routing:

- `CRITICAL`: 즉시 개별 메시지
- `HIGH`: 즉시 또는 짧은 묶음
- `DIGEST`: 시간 창/수량 기준 묶음
- `SUPPRESSED`: 전송하지 않음

전달 규칙:

1. outbox에서 due event를 선택한다.
2. Slack payload를 구성한다.
3. 외부 title/body의 `<`, `>`, `&`와 mention syntax를 escape한다.
4. Slack HTTP 응답을 확인한다.
5. 2xx에서만 `DELIVERED`로 변경한다.
6. 429는 `Retry-After`를 사용한다.
7. timeout/5xx는 `DELIVERY_FAILED`로 기록한다.
8. 재시도 시 동일 event가 별도 row로 중복 생성되지 않는다.

P0는 기존 Release digest 형식을 유지하면서 이 중 **Slack 성공 후 acknowledgement와 실패 재시도**만 구현한다. CRITICAL/HIGH 개별 routing, AI 기반 우선순위, 완전한 lease 운영은 #10 이후 범위다.

### 5.11 Knowledge Export

Knowledge export는 read-only다.

포함:

- normalized raw event
- provenance
- release/advisory body
- project classification
- notification priority/reason
- 별도 AI analysis record 또는 명시적 metadata

금지:

- GitHub live API 호출
- Slack 전송
- outbox 상태 변경
- token/webhook/private metadata 무조건 노출

Private/internal event의 public export 차단은 #12 목표다. P0 workflow는 잠재적으로 민감한 inventory/feed artifact 업로드를 하지 않는다. 로컬 feed/inventory에는 이 보장을 적용했다고 가정하지 말고 신뢰할 수 있는 소비자에게만 제공한다.

## 6. 실행 모델

### 6.1 단기: GitHub Actions

```text
schedule/workflow_dispatch
  -> inventory
  -> collectors
  -> event DB restore
  -> normalize/upsert
  -> policy
  -> optional AI (P1)
  -> notifier
  -> DB cache save (commit only), local feed
```

필수 workflow 보호:

```yaml
permissions:
  contents: read

concurrency:
  group: github-stars-intelligence-state
  cancel-in-progress: false
```

수동 실행 mode:

```text
preview: 외부 조회와 report만 수행, 운영 state 변경 없음
commit: state/outbox 변경 및 정책에 따른 Slack 전송
```

### 6.2 중기: Mac mini Pull Worker

향후 로컬 LLM 서버를 사용하는 경우 GitHub Actions에서 가정 내 서버로 inbound webhook을 열지 않는다.

```text
Mac mini systemd timer/cron
  -> GitHub API 또는 Action artifact pull
  -> local event store
  -> local LLM
  -> Slack notifier
```

Pull 방식은 인바운드 포트 노출을 피하고 로컬 모델과 state를 함께 관리하기 쉽다.

## 7. 실패 의미론

| 실패 | 기대 동작 |
| --- | --- |
| 한 repository Release 404 | 해당 repository만 skip, 전체 run 계속 |
| GitHub rate limit | cursor/state 보존, retry 가능한 오류로 종료 또는 partial report |
| GHSA collector 실패 | Release collector 결과 보존 |
| LLM timeout/invalid JSON | deterministic fallback, event/outbox 보존 |
| Slack 429 | Retry-After 저장, delivered 처리 금지 |
| Slack 5xx/timeout | DELIVERY_FAILED, 다음 실행 재시도 |
| Artifact upload 실패 | run summary에 명시, delivery state와 분리 |
| Cache miss | bootstrap preview/safe mode, 대량 즉시 통지 금지 |
| malformed state DB | fail closed, 새 DB로 덮어쓰지 않음 |

## 8. 보안 및 개인정보 경계

- `GH_PAT`, `SLACK_WEBHOOK_URL`, LLM API key를 파일·artifact·로그에 저장하지 않는다.
- GitHub 원본 title/body는 untrusted input이다.
- Shell inline expression에 외부 입력을 직접 삽입하지 않는다.
- P0 workflow는 inventory/feed artifact를 업로드하지 않는다. private/internal repository의 public export filtering은 #12에서 구현한다.
- event body와 AI prompt/log의 secret-like 값 redaction은 후속 hardening 범위다. P0에서는 외부 Release text를 신뢰하지 않고 feed/artifact 접근 범위를 제한한다.
- GitHub MCP 사용 시 read-only와 최소 toolset을 유지한다.
- LLM output으로 tool/action을 자동 실행하지 않는다.

## 9. 관측성

매 실행 Step Summary 또는 report에 다음을 남긴다.

```text
inventory_count
classified_kubernetes_count
ambiguous_project_count
release_events_collected
advisory_events_collected
pending_outbox_count
critical_count
high_count
digest_count
delivered_count
failed_delivery_count
suppressed_count
collector_errors_by_type
AI success/fallback/failure count
```

로그에는 token, webhook, private event body를 남기지 않는다.

## 10. 테스트 전략

### Unit

- stable event ID
- release pagination/cursor
- duplicate upsert
- event state transitions
- threshold accumulation
- preview no-side-effect
- priority policy
- Slack escaping/splitting
- AI schema/fallback
- visibility filtering

### Fixture integration

- 4개의 pending release + 다음 실행 1개 추가
- Slack 500 후 다음 실행 retry
- 같은 Release 재수집
- 한 repository의 page 2에 unseen release
- Critical GHSA + local LLM down
- private repository inventory/export
- malformed cache/DB

### Live smoke

명시적 수동 실행에서만 수행한다.

- GitHub read-only collector
- `preview` mode
- Slack 실제 전송은 별도 opt-in

## 11. 마이그레이션

1. 기존 `.cache/releases.json`을 read-only로 읽어 repo/tag/published 기준선을 만든다. 과거 cache에는 Release ID가 없으므로 과거 ID를 복원했다고 주장하지 않는다.
2. 기존 값은 `migrated_from_legacy_cache=true` metadata로 기록한다.
3. 기존 legacy cache 파일을 사용하는 첫 cutover run은 Slack을 보내지 않는다. Cache miss에서는 `first_run_notify: false`가 안전 기본값이고, 명시적 `true`는 bootstrap 전송을 허용한다.
4. event DB가 정상 검증된 뒤 legacy cache write를 중단한다.
5. 최소 한 주기 동안 compatibility report로 old/new detection 결과를 비교한다.
6. 차이가 설명 가능해도 rollback 기간에는 원본 legacy cache를 삭제하지 않는다. 제거는 별도 운영 결정이다.

## 12. Issue mapping

| Priority | Issue | 내용 |
| --- | --- | --- |
| Epic | #3 | 전체 Intelligence Watcher |
| P0 | #4 | durable event/outbox와 no-loss delivery |
| P0 | #5 | 모든 unseen Release incremental 수집 |
| P0 | #6 | preview/concurrency/state safety |
| P1 | #7 | Kubernetes 분류와 project registry |
| P1 | #8 | GHSA/Security Advisory collector |
| P1 | #9 | schema-validated local LLM 분석 |
| P1 | #10 | Critical/High/Digest Slack routing |
| P2 | #11 | Maintainer announcement 수집 |
| P2 | #12 | visibility, Knowledge export, CI hardening |

## 13. 미결정 사항

사용자가 소유해야 하는 결정:

1. 일반 Release digest의 시간 창: 실행당/일 단위/누적 수량
2. prerelease의 기본 수집 여부
3. project tier 초기 목록
4. High 이벤트의 즉시 전송 여부
5. private repository의 로컬 처리 및 export 정책
6. 로컬 LLM 기본 모델과 최대 입력 크기
7. GitHub Actions 상태 저장을 장기 유지할지 Mac mini worker로 이전할지
