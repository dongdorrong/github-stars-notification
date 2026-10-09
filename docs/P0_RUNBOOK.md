# P0 Release 전달 운영 런북

> 범위: GitHub Release 수집·SQLite event/outbox·Slack 전송 안전성(#4~#6). GHSA, project registry, LLM, Critical/High/Digest routing 및 public Knowledge visibility(#7~#12)는 후속 작업이다.

## 상태와 실행 모드

| 항목 | 현재 계약 |
| --- | --- |
| 운영 상태 | `.cache/events.sqlite3` (`events`, `notification_outbox`, `state_metadata`, schema version 1) |
| 기존 상태 | `.cache/releases.json`은 read-only 마이그레이션 기준선. 삭제·덮어쓰기 금지 |
| 로컬 산출물 | `.cache/release-feed.json`, `repos.txt`, `.cache/stars-inventory.json` |
| `preview` | CLI 기본값. 기존 DB를 메모리 복사본으로 읽거나 임시 메모리 DB를 사용. 운영 DB/outbox/legacy cache/last notification 변경 및 Slack 전송 없음 |
| `commit` | event/outbox 및 migration metadata 갱신. CLI에서 `--send-slack`을 함께 지정한 경우에만 실제 Slack 전송 가능 |
| workflow | schedule은 commit + Slack. `workflow_dispatch` 기본은 preview; 수동 commit은 기본 브랜치에서만 허용 |

Release identity는 `github:release:<GitHub Release ID>`다. Collector는 저장소마다 모든 page를 조회하고 ID로 중복을 제거한다. 이미 저장된 ID를 만났다는 이유로 pagination을 멈추지 않는다. 저장소별 수집 오류는 그 저장소의 부분 결과를 버리고 다른 저장소와 분리해 보고한다. draft는 알림하지 않고, prerelease metadata는 보존하며 현재 Release 알림 정책에 포함한다.

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

수집의 종료 정책은 결정적이다. 최소 한 저장소가 성공하고 전체 저장소 중 실패 비율이 **50% 이하**이며 모든 실패가 격리된 HTTP 404/429/5xx이면 exit 0과 `collection_degraded: true`를 보고한다. 실패가 없으면 `collection_degraded: false`다. 전체 실패, 50% 초과, HTTP 401/403 또는 미분류 오류는 exit 1이다. 성공 저장소에서 수집한 event는 다른 저장소가 실패해도 state에 남지만, 치명적 수집 오류 때는 Slack 전송을 억제한다. DB/schema, config, legacy migration, Slack 전송 오류도 exit 1이다. Feed와 Step Summary에는 수집·오류 건수 및 HTTP 상태별 안전 범주만 남기고 응답 본문·헤더·토큰·요청 URL은 노출하지 않는다. Degraded run은 정상 완료로 보여도 실패 저장소의 다음 실행 전체 스캔을 확인한다.

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
2. legacy cache가 있으면 repo/tag/published를 **경계값**으로만 사용한다. 파일은 매 실행 read-only로 검증하지만 DB의 migration/저장소 초기화 marker 때문에 기준선 적용은 한 번만 한다. 과거 cache에 Release ID가 없으므로 과거 event를 정확히 복원하거나 동일성 증명할 수 없다. 기준선 이전 Release는 bootstrap 알림에서 제외하고, 이후 Release는 ID 기반 event가 된다.
3. legacy cache가 없거나 저장소가 새로 추가된 경우 기본 `first_run_notify: false`는 첫 수집 inventory를 baseline으로 기록하고 대량 Slack 전송을 막는다. 명시적으로 `true`로 바꾸는 것은 대량 전송 위험을 수용하는 운영 결정이다.
4. **legacy 파일이 있는** 최초 cutover commit 실행은 Slack을 보내지 않는다. legacy 파일이 없는 첫 실행에서 `first_run_notify: true`를 명시했다면 첫 inventory도 알림 후보가 될 수 있다. 기본값 `false`는 이를 막는다. malformed legacy JSON/entry 또는 malformed DB/schema는 자동 빈 상태로 교체하지 않고 오류로 종료한다.

마이그레이션 전에는 기존 cache와 DB를 별도 보존하고 preview 결과·Release 건수·pending 수를 비교한다. feature branch에서 **live commit mode 또는 실제 Slack 전송을 실행하지 않는다**. 기본 브랜치 배포·실제 알림 활성화는 사람이 rollout 시점을 정하고 명시적으로 검토해야 한다.

## GitHub Actions와 상태 보존

- schedule은 기존 UTC `23:00`, `05:00`, `08:00`에 실행한다. workflow-level `concurrency`는 실행 중 restore→수집→전송→save 구간을 직렬화하고 `cancel-in-progress: false`를 사용한다. GitHub의 [concurrency 문서](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency)에 따르면 기본 pending queue는 새 run이 이전 pending run을 대체할 수 있고 처리 순서도 보장되지 않는다. 따라서 collector는 매번 전체 page를 스캔한다.
- preview는 state cache를 restore해 읽을 수 있지만 저장하지 않는다. Slack secret도 해당 step에 주입하지 않는다.
- commit은 legacy cache를 별도 read-only namespace에서 restore하고, 새 DB를 별도 namespace에서 restore한다. 유효한 DB라면 Slack 실패로 Python step이 실패하더라도 cache save step을 시도한다.
- workflow는 잠재적인 private starred repository 정보 때문에 inventory/feed artifact를 업로드하지 않는다. 로컬 feed 역시 public export로 간주하지 않는다.

Actions cache는 영구 DB가 아니라 best-effort 보존 수단이다. GitHub의 [cache 문서](https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching)는 retention/eviction과 restore-key의 최근 cache 선택을 명시한다. 이 구현에서는 eviction, stale restore, 저장 실패, Slack 2xx 직후 DB acknowledgement 또는 cache save 실패가 있으면 중복 전송 또는 상태 소실이 가능하다. 따라서 **exactly-once를 보장하지 않는다**. DB와 전달 이력을 장기 보존해야 한다면 별도 durable store가 필요하다.

## 장애 및 rollback

| 증상 | 조치 |
| --- | --- |
| Slack 429/5xx/timeout | 해당 chunk는 delivered가 아니다. DB save 여부를 확인하고 다음 schedule의 재시도를 관찰한다. `Retry-After` 기간 전에는 429을 즉시 재전송하지 않는다. |
| GitHub API 일부 실패 | 격리된 404/429/5xx가 50% 이하이고 성공 저장소가 있으면 degraded-success다. 오류 저장소의 부분 page는 확정하지 않는다. HTTP 401/403, 미분류 오류, 과반 실패, 전체 실패는 workflow 실패이며 Slack을 보내지 않는다. 원인을 점검하고 다음 실행의 전체 스캔을 확인한다. |
| DB/cache malformed | 전송을 멈추고 손상 파일과 마지막 정상 백업을 보존한다. 새 빈 DB로 덮어쓰거나 legacy JSON을 수정해 재시도하지 않는다. |
| DB cache miss/eviction | 먼저 이전 cache/전달 이력을 조사한다. 빈 DB로 강제 재시작하면 과거 `DELIVERED` ID를 잃어 중복 또는 누락이 생길 수 있다. |
| Slack 성공 후 DB/cache 저장 실패 | 실제 Slack 수신과 DB 상태를 대조한다. 자동 재전송은 중복 가능성이 있으며, 수동 수정은 백업·감사 기록 후 수행한다. |

Rollback은 새 commit-mode 전송을 **중지**하고 event DB, legacy cache, Actions cache 키 및 Slack 전달 이력을 보존하는 것부터 시작한다. 이전 workflow를 stale `.cache/releases.json`만으로 곧바로 재가동하지 않는다. 그 cache는 P0 이후 갱신되지 않아 이미 보낸 Release를 다시 보낼 수 있다. 사람이 전달 이력을 대조해 기준선을 정하고, 필요하면 preview/shadow 실행으로 확인한 뒤 전송 경로를 재개한다. rollback 중에도 legacy 파일은 삭제·덮어쓰지 않는다.

## 남은 위험과 후속 범위

- GitHub Actions cache의 best-effort 내구성, 완료 acknowledgement와 cache save 사이의 crash window, 대기 workflow 취소/순서 변경은 P0만으로 제거되지 않는다.
- Release text는 untrusted input이다. P0 workflow는 artifact 업로드를 하지 않지만 로컬 feed와 Knowledge exporter에 private/public visibility 강제 필터가 없다. 신뢰할 수 있는 public-only 입력 확인 없이 public export하지 않는다(#12).
- GHSA(#8), project registry(#7), LLM(#9), 확장 Slack routing(#10), maintainer announcements(#11), visibility/Knowledge/CI hardening(#12)은 구현 범위 밖이다.
