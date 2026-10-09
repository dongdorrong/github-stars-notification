> P1/P2 업데이트: 현재 DB는 schema v2다. 기존 P0 event/outbox·cutover·bounded collection 의미론은 유지하지만 새 테이블을 추가한다. v1 backup→v2 migration, v2 cache namespace 및 rollback은 [schema reference](EVENT_SCHEMA_REFERENCE.md)와 [rollout runbook](P1_P2_ROLLOUT_RUNBOOK.md)을 우선한다. Python은 3.12, 새 signal 기본은 shadow, public Knowledge export는 visibility fail-closed다.

# P0 Release 전달 운영 런북

> 범위: GitHub Release 수집·SQLite event/outbox·Slack 전송 안전성(#4~#6). GHSA, project registry, LLM, Critical/High/Digest routing 및 public Knowledge visibility(#7~#12)는 후속 작업이다.

## 상태와 실행 모드

| 항목 | 현재 계약 |
| --- | --- |
| 운영 상태 | `.cache/events.sqlite3` (P0 tables 및 revision/analysis/routing/attempt tables, schema version 2) |
| 기존 상태 | `.cache/releases.json`은 read-only 마이그레이션 기준선. 삭제·덮어쓰기 금지 |
| 로컬 산출물 | `.cache/release-feed.json`, `repos.txt`, `.cache/stars-inventory.json` |
| `preview` | CLI 기본값. 기존 DB를 메모리 복사본으로 읽거나 임시 메모리 DB를 사용. 운영 DB/outbox/legacy cache/last notification 변경 및 Slack 전송 없음 |
| `commit` | event/outbox 및 migration metadata 갱신. CLI에서 `--send-slack`을 함께 지정한 경우에만 실제 Slack 전송 가능 |
| workflow | schedule은 commit + Slack. `workflow_dispatch` 기본은 preview; 수동 commit은 기본 브랜치에서만 허용 |

Release identity는 `github:release:<GitHub Release ID>`다. Collector는 최근 page를 제한된 비용 안에서 조회하고 ID로 중복을 제거한다. 과거/backdated Release는 저장된 reconciliation 진행 상태를 통해 후속 실행에서 재확인한다. **한 실행이 전체 이력을 확인하지는 않는다.** 저장소별 수집 오류는 다른 저장소와 분리해 보고한다. 실패한 page의 부분 결과는 버리지만 이전에 완료한 page의 event는 보존한다. draft는 알림하지 않고, prerelease metadata는 보존하며 현재 Release 알림 정책에 포함한다.

## Bounded scan과 과거 reconciliation

| 설정 (`collector`) | 기본값 | 의미 |
| --- | ---: | --- |
| `per_page` | 100 | GitHub Release 목록 요청의 page 크기 |
| `max_incremental_pages_per_repo` / `max_incremental_pages_special_project` | 3 / 5 | 매 실행 recent scan의 일반/관심 저장소별 page 상한 |
| `bootstrap_pages` | 1 | 처음 보는 저장소의 recent 기준선 page 상한. 기본 `first_run_notify: false`가 대량 Slack 전송을 막음 |
| `known_only_pages_to_stop` | 1 | 기존 ID만 있는 완전한 recent page를 만났을 때 recent 경로를 멈추는 기준. 과거 이력 완료를 뜻하지 않음 |
| `global_budget_seconds` / `per_repo_budget_seconds` | 900 / 60 | 전체 수집/저장소별 시간 예산(초). 전체 설정 상한은 1,200초. Live page fetch/정규화에 이른 deadline을 적용하고 fixture는 주입된 clock으로 검사 |
| `reconciliation_pages_per_repo` / `reconciliation_pages_special_project` | 2 / 4 | 선택된 저장소에서 이번 실행에 재확인할 과거 page 상한 |
| `max_reconciliation_repositories_per_run` | 10 | 한 실행의 deep reconciliation 대상 저장소 수 상한 |
| `reconciliation_shards` / `special_reconciliation_shards` | 8 / 2 | 저장소별 안정적 SHA-256 shard와 해당 저장소의 성공 방문 횟수로 정하는 점검 주기. 관심 저장소가 더 자주 선택됨 |

최근 경로는 page 1부터 진행한다. 새로 발견한 Release가 있으면 다음 page도 page 상한까지 확인하고, 기존 ID만 있는 완전한 page를 만나면 멈춘다. Reconciliation은 초기화된 저장소만 대상으로 같은 실행의 남은 예산 안에서 진행한다. `state_metadata`의 `collector_next_repo_index`, `reconcile_visit:<repo>`, `reconciliation_cursor:<repo>`가 실행 간 순서와 진행을 보존한다. 저장소별 성공 방문 횟수와 안정적 SHA-256 shard로 deep scan 대상을 결정한다. 대상 저장소의 deep scan이 실패하거나 대상 수 상한/예산으로 미뤄지면 방문 횟수를 전진시키지 않아 그 기회가 다음 실행에 남는다. 완료한 page 다음에 재개할 때 마지막 성공 page부터 **1 page overlap**을 두고, 목록 끝에 도달하면 page 1로 돌아간다. 실패한 page는 cursor를 전진시키지 않는다. 전체/저장소별 예산이 끝나 시작하지 못했거나 끝내지 못한 저장소는 `repositories_deferred`, 대상 수 상한 또는 deep page 상한으로 남은 작업은 별도 `reconciliation_deferred`로 보고한다. 전자는 이번 실행의 bounded 경로를 완료하지 못했고 후자는 최근 경로를 완료했을 수 있다. Preview는 DB의 메모리 복사본만 바꾸므로 이 진행 상태를 저장하지 않는다.

이 방식은 page 2 이후와 과거에 삽입된 Release를 **후속 성공 실행에서 점진적으로** 찾는다. 한 번의 run이나 고정된 시간 내에 모든 unseen Release를 발견한다고 보장하지 않는다. GitHub API 오류, cache eviction, 계속 증가하는 이력, 잦은 예산 소진은 발견을 더 지연시킬 수 있다. Live PyGithub 요청은 15초 socket timeout/retry 0이고, POSIX main thread의 page fetch·정규화에는 전체/저장소 deadline 중 이른 시각까지의 `SIGALRM` hard wall을 둔다. 기한이 지나 중단된 page는 부분 결과를 버리고 deferred로 남긴다. 지원하지 않는 thread/platform이나 이미 설치된 alarm을 임의로 덮어쓰지 않고 fail closed한다. Fixture는 signal 대신 주입된 clock으로 page 경계에서 검사한다.

Live adapter는 고정된 `PyGithub==2.2.0`의 Release 목록 응답 `_rawData`를 읽어 **page당 목록 요청 1회**를 사용한다. `raw_data` 속성은 항목별 상세 GET을 추가할 수 있어 사용하지 않는다. `_rawData`는 SDK 내부 필드이므로 버전 변경 시 API 호출 수와 page 결과를 회귀 테스트하고, 필드가 없으면 제한 없는 상세 조회로 fallback하지 않고 실패한다. 마지막 page가 정확히 꽉 찼다면 종료 확인을 위한 다음 빈 page 요청이 한 번 필요할 수 있다.

`repositories_started`는 실제로 시도한 저장소 수이고 시작도 못한 deferred는 포함하지 않는다. `repositories_completed`는 **이번 실행의 bounded 경로를 끝냈다**는 뜻이지 과거 전체 페이지를 확인했다는 뜻이 아니다. Release continuation은 기존 metadata를 보존한다. DB는 P1/P2 additive v2 migration을 사용하며 legacy JSON은 바뀌지 않는다. Legacy cache가 없는 새 저장소에서 기본 `first_run_notify: false`이면 첫 page에서 관찰한 최대 숫자 Release ID를 bootstrap 억제 경계로 사용한다. 이후 더 큰 ID의 backdated Release는 발행 시각과 무관하게 후보로 남는다. 숫자 ID가 없는 fixture에서만 `created_at` 시점으로 fallback한다. 명시적 `first_run_notify: true`일 때는 이 억제 경계를 설정하지 않는다. 최대 ID가 과거 이력을 완벽히 구분한다는 GitHub 정렬 보장은 없으므로 두 기준 모두 휴리스틱이다. Cutover 전후 Release와 Slack 수신 이력은 별도로 대조한다.

`min_release_count: 5`는 **이번 실행의 신규 수**가 아니라 아직 전달되지 않은 pending 수다. 4개가 남은 뒤 다음 실행에 1개가 추가되면 5개가 같은 알림 후보가 된다. 관심 프로젝트가 pending이면 `special_project_always_notify: true` 정책으로 즉시 후보가 된다. Slack 2xx가 확인된 chunk의 event만 `DELIVERED`로 바뀐다. 429는 양의 `Retry-After` 초를 반영하고 남은 chunk 전송을 멈춘다. 미전송 chunk도 시도 횟수를 늘리지 않고 `DELIVERY_FAILED`로 표시해 임계값 미만이어도 기한 이후 재시도한다. 5xx/timeout은 실패 상태로 남아 다음 실행에서 재시도된다. 여러 chunk 중 일부만 성공하면 성공 chunk만 완료 처리한다. 실행 중 종료된 lease는 만료 후 재시도된다.

## Feed와 수집 오류 계약

Feed schema `github-stars-release-feed/v1`은 기존 필드를 유지하며 다음 구분을 추가한다.

| 필드 | 계약 |
| --- | --- |
| `new_releases[]`, `new_release_count` | **이번 실행에서 처음 발견한** Release. 기존 `releases[]`, `release_count`는 이 둘의 정확한 alias다. Slack 전송 여부에 따라 의미가 달라지지 않는다. |
| `pending_releases[]`, `pending_release_count` | 전송 전 지금 선택 가능한 pending Release. 임계값 미만이어도 포함하지만 지연 재시도 중인 event는 제외한다. |
| `notification_batch[]`, `notification_batch_count` | 생성한 Slack chunk의 전체 batch. 모든 `slack_chunks[].event_ids`를 순서대로 이어 붙인 값은 `notification_batch[].event_id`와 정확히 일치한다. 부분 전송 실패 후에도 생성 당시 mapping을 유지한다. |
| `pending_before_delivery_count`, `pending_count` | 지연 재시도를 포함한 전송 전·후 미전달 전체 건수. `pending_count`는 후상태이며 `pending_release_count`와 다를 수 있다. |

현재 알림을 분석하는 로컬 LLM은 `notification_batch[]`를 사용한다. Knowledge exporter는 호환 `releases[]`만 소비하므로 누적 알림 batch가 아니라 이번 실행의 신규 수집분만 내보낸다. Private/public 강제 필터는 아직 없으므로 public-only 입력 확인이 필요하다.

수집의 종료 정책은 결정적이다. 최소 한 저장소가 완료되고 **시작한 저장소 중** 실패 비율이 **50% 이하**이며 모든 실패가 격리된 HTTP 404/429/5xx이면 exit 0이다. 오류 또는 deferred가 있으면 `collection_degraded: true`, 둘 다 없으면 `false`다. 완료한 저장소가 없거나, 시작한 저장소 중 과반 실패, HTTP 401/403 또는 미분류 오류가 있으면 exit 1이다. 성공 저장소에서 수집한 event는 다른 저장소가 실패해도 state에 남지만, 치명적 수집 오류 때는 Slack 전송을 억제한다. `notify: true`나 `notification_batch[]`는 **생성된 후보**를 뜻할 뿐 실제 전송 성공 증거가 아니다. `delivery_succeeded`와 실행 종료 코드를 함께 본다. DB/schema, config, legacy migration, Slack 전송 오류도 exit 1이다. Feed와 Step Summary에는 수집·오류·deferred 건수 및 HTTP 상태별 안전 범주만 남기고 응답 본문·헤더·토큰·요청 URL은 노출하지 않는다. Degraded 또는 deferred run은 완료된 이력 조사로 오해하지 말고 다음 실행의 재개 여부를 확인한다.

관측할 수치는 `repositories_total`, `repositories_started`, `repositories_completed`, `repositories_deferred`, `reconciliation_deferred`, `pages_fetched`, `releases_observed`, `new_release_count`, `elapsed_seconds`, `collection_budget_exhausted`, `collection_success_count`, `collection_error_count`, `collection_degraded`, `collector_errors_by_type`다. `scanned_repos`는 기존 소비자 호환 값으로 유지된다. 진행 로그는 저장소 이름 대신 실행별 ordinal과 짧은 keyed reference를 사용한다. 로컬 feed에는 신뢰 경계 안에서 사용하는 Release facts가 남으므로 외부에 게시하지 않는다.

## 안전한 로컬 검증

저장소 루트에서 실행한다. 다음 fixture는 token이나 Slack webhook을 요구하지 않으며 preview만 수행한다.

```bash
tmp_dir="$(mktemp -d)"
printf 'example/project\n' > "$tmp_dir/repos.txt"
printf '%s\n' '{"example/project":[{"id":123456,"tag_name":"v1.0.0","name":"v1.0.0","published_at":"2026-10-09T00:00:00Z","html_url":"https://github.com/example/project/releases/tag/v1.0.0"}]}' > "$tmp_dir/releases.json"
python3 .github/scripts/check_release.py \
  --repos-file "$tmp_dir/repos.txt" \
  --fixture-releases "$tmp_dir/releases.json" \
  --state-db "$tmp_dir/events.sqlite3" \
  --cache-path "$tmp_dir/legacy.json" \
  --feed-path "$tmp_dir/feed.json" \
  --github-output "$tmp_dir/output.txt" \
  --mode preview
test ! -e "$tmp_dir/events.sqlite3"
```

`--sleep-seconds`와 `--no-sleep`은 이전 CLI 호출의 호환만 위한 deprecated no-op이다. 현재 collector의 저장소별 pacing에는 영향을 주지 않으므로 새 실행 절차에 사용하지 않는다.

기존 fixture JSON의 단일 Release object도 계속 읽는다. ID가 없는 **fixture에 한해서만** repo/tag/published 기반 deterministic fallback ID를 생성한다. live GitHub Release에는 ID가 필수다. 전달 성공·실패의 commit 동작은 실제 webhook 대신 fake transport를 주입하는 `tests/`의 회귀 테스트로 검증한다. `--mode preview --send-slack` 조합은 오류다.

```bash
python3 -m py_compile .github/scripts/check_release.py
python3 -m compileall -q .github/scripts starwatch
python3 -m unittest discover -s tests -v
git diff --check
```

## 마이그레이션과 첫 실행

1. 새 DB가 없으면 `commit`이 schema를 만들고 `state_metadata`에 one-time legacy migration marker 및 저장소별 초기화 marker를 기록한다. `preview`는 메모리에서만 같은 판단을 시뮬레이션한다.
2. legacy cache가 있으면 repo/tag/published를 **경계값**으로만 사용한다. 파일은 매 실행 read-only로 검증한다. 최초 migration은 수집 성공 여부와 무관하게 **모든 legacy 저장소**의 published 날짜를 DB의 `legacy_cutover_published_at:<repo>`에 먼저 기록한다. 실패·deferred 저장소도 이후 첫 수집에서 이 날짜를 사용할 수 있으며, legacy 파일이 없어져도 경계가 남는다. 기존 P0 DB가 전역 migration marker만 가진 채 아직 초기화되지 않은 저장소는 legacy 파일이 남아 있을 때만 이 날짜를 보충할 수 있다. 파일과 날짜 metadata가 둘 다 없으면 원래 경계를 복원할 수 없으므로 과거 알림 이력과 대조해야 한다. 이 날짜는 numeric bootstrap ID 경계보다 **우선** 적용한다. 과거 cache에 Release ID가 없으므로 과거 event를 정확히 복원하거나 동일성 증명할 수 없다. 아래 cutover 정책 적용 후에는 이 날짜 경계를 기존 G005 방식으로 유지한다.
3. legacy cache가 없거나 저장소가 새로 추가된 경우 기본 `first_run_notify: false`는 bounded 첫 page를 baseline으로 기록하고 대량 Slack 전송을 막는다. 숫자 ID는 최대 관찰 ID, fixture의 synthetic ID는 `created_at`를 억제 경계로 저장한다. 명시적으로 `true`로 바꾸면 이 억제 경계를 설정하지 않으며, 대량 전송 위험을 수용하는 운영 결정이다.
4. **legacy 파일이 있는** 최초 cutover commit 실행은 Slack을 보내지 않는다. 기본 `notification.cutover_pending_policy: suppress_existing`은 그 실행의 bounded scan에서 발견한 모든 기존 Release를 event store에 저장하되 outbox는 `SUPPRESSED`로 만든다. Legacy 날짜보다 새롭거나 `first_run_notify: true`인 경우도 동일하다. 따라서 기존 preview의 71개 알림 후보도 새 정책으로 시뮬레이션/최초 commit하면 pending 0이며, 이후 실행에서 새 Release가 추가되면 그 Release만 정상 pending 정책을 따른다. 명시적 `preserve_pending`은 이전 날짜 기준 migration 동작을 유지하는 호환 옵션이며 backlog가 다음 실행에 전송될 수 있다. legacy 파일이 없는 첫 실행에서 `first_run_notify: true`를 명시했다면 첫 inventory도 알림 후보가 될 수 있다. 기본값 `false`는 이를 막는다. malformed legacy JSON/entry 또는 malformed DB/schema는 자동 빈 상태로 교체하지 않고 오류로 종료한다.

Cutover 억제의 적용 조건은 legacy 파일 존재, `legacy_migrated` marker 부재, 기존 Release event row가 없는 DB다. 선택한 정책은 `legacy_cutover_pending_policy` metadata에 기록하고 실제 적용 여부/비-draft 관찰 cohort 건수는 feed·GitHub outputs·안전한 console의 `cutover_policy_applied`, `cutover_backlog_suppressed_count`로 확인한다. 이미 Release event가 있는 DB는 marker가 누락돼도 새로운 cutover cohort로 간주하지 않는다. Cutover 억제는 최초 전환 실행에 관찰한 cohort에만 적용하며, 이미 migration이 끝난 SQLite의 pending/retry/delivered를 일괄 삭제하거나 재분류하지 않는다. 페이지 상한 때문에 아직 읽지 않은 전체 과거 이력을 모두 suppress했다고 주장하지 않는다. 후속 reconciliation은 기존 G005 기준선 정책을 따른다. Preview는 SQLite 메모리 복사본만 변경하므로 실제 첫 commit의 cutover marker를 소비하지 않는다. Cutover 자체는 metadata 정책이며, 현재 v2 migration에서도 legacy 파일을 그대로 유지한다. 설정을 나중에 바꿔도 이미 `SUPPRESSED`로 저장한 baseline을 자동 pending으로 되살리지 않는다.

마이그레이션 전에는 기존 cache와 DB를 별도 보존하고 preview 결과·Release 건수·pending 수를 비교한다. feature branch에서 **live commit mode 또는 실제 Slack 전송을 실행하지 않는다**. 기본 브랜치 배포·실제 알림 활성화는 사람이 rollout 시점을 정하고 명시적으로 검토해야 한다.

## GitHub Actions와 상태 보존

- schedule은 기존 UTC `23:00`, `05:00`, `08:00`에 실행한다. workflow-level `concurrency`는 실행 중 restore→수집→전송→save 구간을 직렬화하고 `cancel-in-progress: false`를 사용한다. GitHub의 [concurrency 문서](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency)에 따르면 기본 pending queue는 새 run이 이전 pending run을 대체할 수 있고 처리 순서도 보장되지 않는다. 따라서 collector는 commit 실행 간 저장된 cursor와 overlap을 사용하고, 실행 순서가 보장된다고 가정하지 않는다.
- preview는 state cache를 restore해 읽을 수 있지만 저장하지 않는다. Slack secret도 해당 step에 주입하지 않는다.
- commit은 legacy cache를 별도 read-only namespace에서 restore하고, 새 DB를 별도 namespace에서 restore한다. 유효한 DB라면 Slack 실패로 Python step이 실패하더라도 cache save step을 시도한다.
- workflow는 잠재적인 private starred repository 정보 때문에 inventory/feed artifact를 업로드하지 않는다. 로컬 feed 역시 public export로 간주하지 않는다.

Actions cache는 영구 DB가 아니라 best-effort 보존 수단이다. GitHub의 [cache 문서](https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching)는 retention/eviction과 restore-key의 최근 cache 선택을 명시한다. 이 구현에서는 eviction, stale restore, 저장 실패, Slack 2xx 직후 DB acknowledgement 또는 cache save 실패가 있으면 중복 전송 또는 상태 소실이 가능하다. 따라서 **exactly-once를 보장하지 않는다**. DB와 전달 이력을 장기 보존해야 한다면 별도 durable store가 필요하다.

## 장애 및 rollback

| 증상 | 조치 |
| --- | --- |
| Slack 429/5xx/timeout | 해당 chunk는 delivered가 아니다. DB save 여부를 확인하고 다음 schedule의 재시도를 관찰한다. `Retry-After` 기간 전에는 429을 즉시 재전송하지 않는다. |
| GitHub API 일부 실패 | 시작한 저장소 중 격리된 404/429/5xx가 50% 이하이고 완료한 저장소가 있으면 degraded-success다. 실패 page의 부분 결과와 cursor 전진은 버리되 그 전에 완료한 page의 event는 보존한다. HTTP 401/403, 미분류 오류, 시작한 저장소 중 과반 실패, 완료 저장소 0건은 workflow 실패이며 Slack을 보내지 않는다. 다음 실행의 bounded scan/reconciliation 재개를 확인한다. |
| 수집 예산 소진/deferred 증가 | Feed/Step Summary의 `collection_budget_exhausted`, `repositories_started/completed/deferred`, `pages_fetched`, `elapsed_seconds`를 확인한다. 반복되면 설정한 page/시간 예산과 GitHub rate limit을 검토하되 cursor·DB를 지우지 않는다. |
| Live deadline 실행 불가 | POSIX main thread와 기존 alarm handler/timer 충돌 여부를 확인한다. 강제 우회하거나 제한 없는 요청으로 대체하지 않는다. Fixture preview로 state 안전성을 먼저 검증한다. |
| DB/cache malformed | 전송을 멈추고 손상 파일과 마지막 정상 백업을 보존한다. 새 빈 DB로 덮어쓰거나 legacy JSON을 수정해 재시도하지 않는다. |
| DB cache miss/eviction | 먼저 이전 cache/전달 이력을 조사한다. 빈 DB로 강제 재시작하면 과거 `DELIVERED` ID를 잃어 중복 또는 누락이 생길 수 있다. |
| Slack 성공 후 DB/cache 저장 실패 | 실제 Slack 수신과 DB 상태를 대조한다. 자동 재전송은 중복 가능성이 있으며, 수동 수정은 백업·감사 기록 후 수행한다. |

Rollback은 새 commit-mode 전송을 **중지**하고 event DB, legacy cache, Actions cache 키 및 Slack 전달 이력을 보존하는 것부터 시작한다. 이전 workflow를 stale `.cache/releases.json`만으로 곧바로 재가동하지 않는다. 그 cache는 P0 이후 갱신되지 않아 이미 보낸 Release를 다시 보낼 수 있다. 사람이 전달 이력을 대조해 기준선을 정하고, 필요하면 preview/shadow 실행으로 확인한 뒤 전송 경로를 재개한다. rollback 중에도 legacy 파일은 삭제·덮어쓰지 않는다.

Bounded collector만의 rollback과 달리 P1/P2 v2 DB에서 P0 코드로의 rollback은 v1 backup 복구가 필요하다. 자동 schema downgrade는 제공하지 않는다. `state_metadata`에 추가한 continuation 키를 삭제해 과거 페이지를 처음부터 강제 스캔하지 않는다. 이전 exhaustive collector로 되돌리면 GitHub API 비용과 Actions timeout 위험이 재발할 수 있으므로 상태 백업과 preview로 실제 호출 범위를 확인한 뒤 운영 전송 여부를 결정한다.

## 남은 위험과 후속 범위

- GitHub Actions cache의 best-effort 내구성, 완료 acknowledgement와 cache save 사이의 crash window, 대기 workflow 취소/순서 변경은 P0만으로 제거되지 않는다.
- Bounded scan은 API 비용을 낮추지만 Release 발견 지연을 허용한다. Reconciliation은 정상 commit 실행과 보존된 metadata가 계속된다는 전제에서 점진적으로 진행하며, cache eviction이나 반복 오류 시 완전성·최대 지연 시간은 보장되지 않는다.
- Page당 1회 요청 최적화는 고정된 PyGithub 2.2.0의 `_rawData` 내부 필드에 결합돼 있다. SDK 변경 시 token-free adapter/API-request-count 회귀 테스트와 안전한 preview 없이 버전을 올리지 않는다.
- Release text는 untrusted input이다. P0 workflow는 artifact 업로드를 하지 않지만 로컬 feed는 신뢰 경계 안에 둔다. Knowledge exporter는 public-only 기본이며 unknown/private/internal은 제외한다.
- P1/P2 signal·분석·routing의 운영 절차는 각 전용 runbook과 shadow/canary/full rollout을 따른다.
