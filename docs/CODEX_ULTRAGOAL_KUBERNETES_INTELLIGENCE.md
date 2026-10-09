# Codex UltraGoal — Reliable Kubernetes Intelligence P0

아래 프롬프트를 Codex/OMX 메인 세션에 그대로 전달한다.

```text
# ULTRAGOAL: Reliable Kubernetes Intelligence P0

Repository:
- /home/dongdorrong/github/private/github-stars-notification
- GitHub: dongdorrong/github-stars-notification

Primary issues:
- #4 Introduce durable event/outbox state and no-loss delivery semantics
- #5 Collect every unseen GitHub release incrementally
- #6 Make preview runs and concurrent workflows state-safe

Parent epic:
- #3 Kubernetes ecosystem official intelligence watcher

Reference documents:
- AGENTS.md
- README.md
- docs/AI_PROJECT_CONTEXT.md
- docs/KUBERNETES_INTELLIGENCE_ARCHITECTURE.md
- docs/KUBERNETES_INTELLIGENCE_ROADMAP.md
- docs/adr/0001-deterministic-event-state-and-ai-boundary.md
- docs/SECURITY_LAYERING_NOTES.md

## Mission

현재 GitHub starred repository의 latest Release를 `.cache/releases.json`과 비교하고 Slack으로 전송하는 구조를, 이벤트를 잃지 않는 durable event/outbox 기반 P0 vertical slice로 전환하라.

이번 UltraGoal은 #4, #5, #6을 구현·검증·문서화하고 원격 branch와 Pull Request까지 만드는 것이 완료 조건이다.

단순 설계나 일부 코드 작성으로 종료하지 마라. repository 조사, characterization tests, implementation, migration compatibility, workflow 변경, 문서 동기화, fixture 검증, commit, push, PR까지 현재 세션에서 수행하라.

## Absolute success criteria

다음이 모두 충족돼야 COMPLETE다.

1. Release event가 임계값 미만이어도 pending 상태로 소실 없이 누적된다.
2. Slack 성공 전에는 event가 delivered 처리되지 않는다.
3. Slack 실패/429/5xx/timeout 이후 같은 event가 다음 실행에서 재시도 대상이 된다.
4. Slack 성공 이후에는 같은 event가 다시 알림 후보가 되지 않는다.
5. 한 repository에서 실행 사이 여러 Release가 나오면 모든 unseen Release를 수집한다.
6. stable identity는 GitHub Release ID를 기준으로 하며 discovery 순서나 tag 문자열에 의존하지 않는다.
7. pagination 이후 page의 unseen Release도 수집한다.
8. `workflow_dispatch` preview는 event DB, outbox, legacy cache, last notification을 변경하지 않는다.
9. schedule/commit 실행만 state를 변경한다.
10. workflow concurrency와 최소 permissions가 적용된다.
11. cache miss/first run이 기본적으로 대량 Slack 알림을 발생시키지 않는다.
12. 기존 token-free fixture 테스트 경로가 유지되거나 명확한 호환 wrapper를 제공한다.
13. 모든 unit/fixture tests와 compile checks가 통과한다.
14. README, AI context, runbook/architecture 설명이 실제 구현과 일치한다.
15. live Slack 전송, 실제 GitHub write, secret 출력 없이 검증된다.
16. 변경사항이 feature branch에 commit/push되고 PR이 생성된다.

## Mandatory safety constraints

- main branch에 직접 commit하지 마라.
- 기존 사용자 변경을 reset, clean, checkout overwrite, stash drop 등으로 파괴하지 마라.
- 시작 workspace가 dirty하면 전용 worktree를 만들어 작업하라.
- GH_PAT, SLACK_WEBHOOK_URL, token, webhook 값을 출력·로그·파일·commit에 남기지 마라.
- 실제 Slack webhook을 호출하지 마라.
- 실제 notification state를 소비하는 live commit-mode workflow를 실행하지 마라.
- GitHub Release title/body는 untrusted input으로 취급하라.
- LLM을 이번 P0 state/notification 판단에 도입하지 마라.
- #7~#12 구현을 무리하게 끌어오지 마라.
- 기존 `.cache/releases.json`을 migration source로 읽을 수 있으나 삭제하거나 원본을 덮어쓰지 마라.
- malformed DB/cache 발견 시 fail closed하고 자동 초기화로 과거 state를 지우지 마라.
- 외부 API 없이 핵심 수용 기준을 재현할 fixture tests를 먼저 만들어라.

## Initial procedure

1. repository 및 작업 상태를 확인한다.

```bash
cd /home/dongdorrong/github/private/github-stars-notification
git status --short --branch
git remote -v
git fetch --prune
```

2. 다음 파일과 이슈를 먼저 읽는다.

```bash
sed -n '1,260p' AGENTS.md
sed -n '1,320p' README.md
sed -n '1,360p' docs/AI_PROJECT_CONTEXT.md
sed -n '1,420p' docs/KUBERNETES_INTELLIGENCE_ARCHITECTURE.md
sed -n '1,360p' docs/KUBERNETES_INTELLIGENCE_ROADMAP.md
sed -n '1,320p' docs/adr/0001-deterministic-event-state-and-ai-boundary.md
sed -n '1,360p' .github/scripts/check_release.py
sed -n '1,280p' .github/workflows/notify-starred-releases.yml
sed -n '1,320p' tests/test_check_release.py

gh issue view 4 --repo dongdorrong/github-stars-notification
gh issue view 5 --repo dongdorrong/github-stars-notification
gh issue view 6 --repo dongdorrong/github-stars-notification
```

3. 현재 구현의 실제 behavior를 코드와 test로 확인한다. 문서만 신뢰하지 마라.

4. 안전한 작업 branch를 만든다.

권장 branch:

```text
feat/reliable-kubernetes-intelligence-p0
```

workspace가 clean이면:

```bash
git switch -c feat/reliable-kubernetes-intelligence-p0 origin/main
```

workspace가 dirty하면 기존 변경을 건드리지 말고 별도 worktree를 만든다.

## Required design

### A. Module boundaries

현재 `.github/scripts/check_release.py`의 public CLI compatibility는 최대한 유지하되, P0 상태/수집 로직을 테스트 가능한 모듈로 분리하라.

권장 구조이며 기존 repository에 맞게 합리적으로 조정할 수 있다.

```text
.github/scripts/check_release.py       # CLI/orchestration compatibility
starwatch/
  __init__.py
  models.py                            # normalized ReleaseEvent
  release_collector.py                 # live + fixture collectors
  event_store.py                       # SQLite schema/migration/query
  policy.py                            # pending selection/threshold
  slack_payload.py                     # pure formatting only
tests/
  test_event_store.py
  test_release_collector.py
  test_notification_policy.py
  test_preview_mode.py
```

새 package 위치는 달라도 되지만 다음 책임은 분리돼야 한다.

- collection
- event normalization
- persistent state/outbox
- notification selection
- Slack payload formatting
- CLI/workflow orchestration

### B. Event identity

GitHub Release event:

```text
event_id = github:release:<release_id>
source_id = github:release:<release_id>
```

필수 normalized fields:

```text
event_id
event_type=github_release
source_id
repository
release_id
tag_name
release_name
body
html_url
published_at
created_at
updated_at
draft
prerelease
is_special/project priority context
content_hash
raw metadata/provenance
```

Tag와 published time만으로 event ID를 만들지 마라.

Fixture가 아직 Release ID를 제공하지 않는 경우 fixture schema를 확장한다. legacy fixture 호환을 유지해야 한다면 deterministic fixture-only fallback ID를 명시적으로 제공하되 live collector는 GitHub Release ID를 필수로 사용하라.

### C. SQLite state

Python standard library `sqlite3`를 우선 사용하여 불필요한 runtime dependency를 늘리지 마라.

최소 schema:

```sql
CREATE TABLE events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    repository TEXT NOT NULL,
    source_id TEXT NOT NULL,
    published_at TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE TABLE notification_outbox (
    event_id TEXT PRIMARY KEY,
    priority TEXT NOT NULL,
    notification_state TEXT NOT NULL,
    notify_attempts INTEGER NOT NULL DEFAULT 0,
    first_queued_at TEXT NOT NULL,
    next_attempt_at TEXT,
    notified_at TEXT,
    last_error TEXT,
    FOREIGN KEY(event_id) REFERENCES events(event_id)
);

CREATE TABLE state_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

필요하면 migration version을 추가하라.

State enum 최소값:

```text
DISCOVERED
PENDING_NOTIFICATION
DELIVERY_IN_PROGRESS
DELIVERED
DELIVERY_FAILED
SUPPRESSED
```

Atomic transaction을 사용하라.

### D. Pending accumulation semantics

현재 `min_release_count=5` 의미를 실제 누적 pending count로 구현하라.

예시:

```text
Run 1: 4 release events -> store/outbox pending, no Slack payload
Run 2: 1 release event  -> pending total 5, one digest payload
Delivery success        -> 5 events delivered
Delivery failure        -> 5 events remain retryable
```

`special_project_always_notify=true`는 pending special event가 하나라도 있으면 즉시 candidate가 되는 기존 의도를 유지하되, delivery success 전에는 상태를 소비하지 마라.

### E. Delivery acknowledgement interface

현재 workflow의 별도 notify job이 실제 Slack response를 state에 반영하기 어렵다면 구조를 안전하게 바꿔라.

허용되는 방향:

1. Python notifier가 Slack을 전송하고 성공 후 DB를 commit
2. 별도 notify job이 acknowledgement artifact/output을 만들고 후속 finalize job이 state를 갱신

단기 P0에서는 1번이 더 단순할 가능성이 높다. 다만 테스트에서는 fake transport를 주입하여 실제 Slack을 호출하지 마라.

필수 transport contract 예시:

```python
class SlackTransport(Protocol):
    def send(self, payload: dict[str, str]) -> SlackResult: ...
```

`SlackResult`에는 status code, retry-after, error가 포함돼야 한다.

Payload 생성 함수는 pure function으로 유지하라.

### F. Incremental Release collection

Live collector는 repository별 Release 목록을 pagination하고 unseen event를 모두 반환해야 한다.

요구사항:

- release ID 포함
- page 2 이상 지원
- draft/prerelease metadata 보존
- repository별 오류 격리
- 404=no releases 또는 inaccessible 상태를 명확히 구분
- rate limit/5xx가 partial state를 잘못 delivered 처리하지 않음

`PyGithub`를 유지할지 `gh api`/REST로 바꿀지는 현재 코드와 테스트 가능성을 보고 결정하라. 무엇을 선택하든 collector interface와 fixture implementation을 분리하라.

### G. Legacy cache migration

기존 `.cache/releases.json`은 cursor bootstrap 자료다.

요구사항:

- 원본 파일 보존
- one-time migration marker
- migration 자체로 Slack을 보내지 않음
- old cache에 Release ID가 없으므로 기존 repo/tag/published를 migration baseline으로만 사용
- migration 이후 새 live Release는 ID 기반 event로 저장
- malformed legacy cache는 명시적 오류를 내고 새 빈 state로 조용히 대체하지 않음

완벽한 과거 event 복원이 아니라 안전한 cutover가 목표다.

### H. Preview and commit mode

CLI에 명시적 mode를 둔다.

예시:

```text
--mode preview
--mode commit
```

Preview:

- live/fixture collect 가능
- report/feed/payload preview 가능
- DB/cache/outbox/last notification 변경 금지
- Slack 전송 금지

Commit:

- event/outbox upsert
- policy selection
- 명시적 notifier enable 조건에서만 Slack
- delivery result state 반영

기존 arguments와의 호환성을 유지하되 ambiguous한 `send_slack=false`만으로 preview 의미를 표현하지 마라.

### I. Workflow hardening

`.github/workflows/notify-starred-releases.yml`에 최소 다음을 반영하라.

```yaml
permissions:
  contents: read

concurrency:
  group: github-stars-intelligence-state
  cancel-in-progress: false
```

- schedule은 commit mode
- workflow_dispatch는 mode input을 제공하고 기본 `preview`
- preview는 Slack 미전송 및 state artifact/cache 저장 금지 또는 preview 전용 경로
- commit mode만 state cache를 저장
- job timeout 설정
- 외부 입력을 inline shell expression으로 삽입하지 않는 기존 보안 규칙 유지
- schedule 시각은 변경하지 않음

### J. First run / cache miss

Safe default:

- first run/cache miss는 inventory와 baseline state를 만들되 대량 Slack을 기본 전송하지 않음
- explicit bootstrap notification 옵션이 있을 때만 현재 전체 Release를 통지
- config default와 README를 실제 behavior에 맞춤

## Mandatory tests

구현 전에 기존 behavior characterization test를 추가하고, 이후 다음 테스트를 모두 작성하라.

### Event store

1. 동일 event ID 반복 upsert -> events 1개
2. first_seen_at 유지, last_seen_at 갱신
3. raw payload/content hash update
4. malformed DB/schema version -> fail closed
5. transaction rollback

### Pending/outbox

6. 4개 pending + 다음 실행 1개 -> 5개 선택
7. threshold 미달 pending 유지
8. special event 즉시 선택
9. delivery failure -> retryable
10. delivery success -> delivered
11. delivered event 재선택 금지
12. partial payload success/failure 상태 보존

### Release collector

13. 같은 repository의 3개 unseen releases 전부 반환
14. pagination page 2 unseen release 수집
15. duplicate release ID dedupe
16. draft/prerelease metadata
17. repository 404/5xx isolation
18. fixture/live adapter contract parity

### Preview

19. preview 전후 SQLite file hash 또는 logical state 동일
20. preview에서 감지한 event가 다음 commit run에 다시 수집/처리됨
21. preview Slack transport 호출 0회
22. commit + fake Slack success 상태 전이
23. commit + fake Slack 429/500 상태 전이

### Workflow/config

24. mode default가 preview인지 검증 가능한 parser/config test
25. first-run default no-notify
26. existing config normalization compatibility
27. Slack text splitting/escaping regression

## Validation commands

최소 다음을 실행하라.

```bash
python3 -m py_compile .github/scripts/check_release.py
python3 -m compileall .github/scripts starwatch 2>/dev/null || true
python3 -m unittest discover -s tests -v
git diff --check
```

새 package 이름이 `starwatch`가 아니면 실제 경로로 compileall하라. `|| true`로 실제 compile 실패를 숨기지 마라. 위 예시의 경로 미존재 처리만 조정하라.

Fixture smoke를 추가로 실행하라.

```bash
python3 .github/scripts/check_release.py \
  --repos-file <fixture repos> \
  --fixture-releases <fixture releases> \
  --state-db <temporary sqlite> \
  --mode preview \
  --github-output <temporary output> \
  --no-sleep
```

그리고 commit mode는 fake/no-network Slack transport로 검증하라.

## Documentation updates

실제 구현에 맞게 다음을 갱신하라.

- README.md
- AGENTS.md
- docs/AI_PROJECT_CONTEXT.md
- docs/KUBERNETES_INTELLIGENCE_ARCHITECTURE.md
- docs/KUBERNETES_INTELLIGENCE_ROADMAP.md
- 필요 시 신규 migration/runbook

문서에서 다음을 명확히 하라.

- state DB path
- preview vs commit
- first-run behavior
- migration
- retry/delivery semantics
- fixture commands
- rollback
- known residual risks

## Commit strategy

가능하면 다음 순서의 논리적 commit으로 분리하라.

1. `test: characterize release cache and notification behavior`
2. `feat: add durable event store and notification outbox`
3. `feat: collect all unseen releases incrementally`
4. `feat: make preview and workflow state-safe`
5. `docs: update reliable event delivery runbook`

각 commit 전에 관련 테스트를 실행하라. 중간 commit이 반드시 전체 green일 필요는 없지만 최종 branch는 green이어야 한다.

## GitHub issue and PR handling

- #4, #5, #6의 body와 acceptance criteria를 기준으로 구현한다.
- 완전히 충족한 이슈만 PR body에서 `Closes #N`으로 연결한다.
- 부분 구현은 `Refs #N`으로 남기고 완료했다고 표시하지 마라.
- parent #3은 닫지 마라.
- PR body에 다음을 포함한다.

```text
Summary
Before / After behavior
Issue mapping
State schema and migration
Preview/commit semantics
Tests and evidence
Live side effects performed: none
Rollback
Residual risks
```

Push 전 확인:

```bash
git status --short --branch
git log --oneline --decorate -n 10
git diff origin/main...HEAD --check
```

그 다음 branch를 push하고 PR을 생성하라.

## Stop conditions

다음 이유로 중단하지 마라.

- 작업량이 많아 보임
- 기존 코드가 monolithic함
- 상태 migration이 복잡함
- 테스트가 추가로 필요함

합리적인 설계 결정을 내리고 계속 진행하라. 사용자에게 질문하지 않아도 repository, issues, architecture 문서로 결정 가능한 사항은 안전한 기본값을 선택하고 PR에 기록하라.

다음 경우에만 block으로 보고 최종 보고에 명시하라.

- GitHub 인증이 없어 push/PR 자체가 불가능함
- repository에 해결할 수 없는 permission denial이 있음
- upstream/main이 작업 중 강제로 변경되어 안전한 rebase가 불가능함

Block이 있어도 구현·테스트·local commits까지 가능한 범위는 모두 완료하라.

## Final report format

```text
ULTRAGOAL STATUS: COMPLETE | BLOCKED

Branch:
Commits:
PR:
Issues completed/referenced:

Architecture implemented:
- ...

Behavior before:
- ...

Behavior after:
- ...

Tests:
- command: result

Migration:
- ...

Live side effects:
- Slack sent: no
- Production state modified: no

Residual risks:
- ...

Follow-up issues:
- #7 ...
- #8 ...
```

UltraGoal은 코드만 작성한 상태가 아니라 tests green + docs synced + remote PR created 상태에서 완료다.
```
