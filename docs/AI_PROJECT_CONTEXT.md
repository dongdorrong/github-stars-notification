# AI Project Context — GitHub Stars Release Notification

> 다른 세션·프로젝트가 이 저장소의 **현재 P0 Release 경로**를 이해하기 위한 handoff 문서다. 목표 설계와 구현 완료 범위를 혼동하지 않는다.

## 1. 목적과 구현 범위

GitHub에서 star한 저장소의 Release를 주기적으로 수집하고, 결정적 정책에 따라 Slack에 알린다. P0(#4~#6)는 GitHub Release ID를 event identity로 사용하고 SQLite event/outbox를 통해 pending 누적·실패 재시도·preview 안전성을 제공한다. Kubernetes project registry, GHSA, 로컬 LLM 분석, Critical/High/Digest routing, maintainer announcements, 공개 Knowledge visibility 강화는 #7~#12의 후속 범위다.

## 2. 핵심 파일

| 경로 | 역할 |
| --- | --- |
| `.github/workflows/notify-starred-releases.yml` | schedule/dispatch mode, inventory 수집, cache restore/save, Slack secret 경계, concurrency |
| `.github/scripts/check_release.py` | 호환 CLI, config, feed/output, pipeline 실행 |
| `starwatch/release_collector.py` | live/fixture Release 목록 수집과 ID 정규화 |
| `starwatch/event_store.py` | SQLite schema, event/outbox 상태, transaction, lease·ack |
| `starwatch/pipeline.py` | legacy 기준선, 수집·저장·선택·전달 orchestration |
| `starwatch/policy.py`, `starwatch/notifier.py`, `starwatch/slack_payload.py` | pending 선택, Slack 응답, 순수 payload 생성 |
| `config.yaml` | 관심 프로젝트와 알림 정책. `owner / repo` 표기는 `owner/repo`로 정규화 |
| `docs/P0_RUNBOOK.md` | 운영·마이그레이션·복구·잔여 위험 |
| `tests/` | token-free fixture 및 상태 전이 회귀 테스트 |

## 3. 현재 실행 흐름

1. workflow가 `gh api /user/starred --paginate`로 starred repository 목록과 inventory를 로컬 파일에 만든다.
2. Release collector가 각 repository의 모든 page를 끝까지 읽고 GitHub Release ID(`github:release:<id>`)로 중복을 제거한다. 저장소별 API 오류는 다른 저장소와 분리하고 그 저장소의 부분 page 결과는 확정하지 않는다. 격리된 HTTP 404/429/5xx가 전체의 50% 이하이고 최소 한 저장소가 성공했다면 `collection_degraded: true`와 exit 0이다. 401/403, 미분류 오류, 과반 실패, 전체 실패는 exit 1이며 Slack을 억제한다. 성공 저장소의 event는 보존한다.
3. pipeline이 `.cache/events.sqlite3`의 `events`와 `notification_outbox`를 갱신한다. `.cache/releases.json`은 매번 read-only로 검증하되 ID가 없는 과거 기준선은 DB marker에 따라 최초 저장소 초기화에만 적용하며 원본을 보존한다.
4. `min_release_count`는 실행별 신규 건수가 아니라 미전달 pending 누적 수에 적용된다. 특별 프로젝트가 pending이면 현재 정책상 즉시 후보가 된다.
5. commit + `--send-slack`이면 Python notifier가 Slack을 호출한다. 2xx 확인 후 해당 chunk의 event만 `DELIVERED`로 갱신한다. 429/5xx/timeout은 실패 상태와 재시도 metadata를 남긴다.
6. `.cache/release-feed.json`을 로컬에 만든다. Workflow는 inventory/feed를 artifact로 업로드하지 않는다.

Feed schema v1의 배열은 분리된 계약이다.

| 필드 | 의미 |
| --- | --- |
| `new_releases[]`, `new_release_count` | 이번 실행에서 처음 발견한 event. 기존 `releases[]`/`release_count`의 고정 alias이며 Slack 상태에 따라 의미가 달라지지 않는다. Knowledge exporter는 이 discovery alias만 읽는다. |
| `pending_releases[]`, `pending_release_count` | 전송 전 선택 가능한 pending. 지연 재시도 중인 event는 제외한다. |
| `notification_batch[]`, `notification_batch_count` | `slack_chunks[].event_ids`와 순서까지 일치하는 생성 알림 batch. LLM의 현재 알림 분석 입력이다. |
| `pending_count` | 지연 재시도를 포함한 **전송 후** 미전달 수. `pending_before_delivery_count`는 같은 기준의 전송 전 수다. |

Feed와 Step Summary는 `scanned_repos`, `collection_success_count`, `collection_error_count`, `collection_degraded`, `collector_errors_by_type`에 대응하는 수집 통계와 안전한 HTTP 범주만 남긴다. 오류 응답 본문·헤더·토큰·요청 URL은 남기지 않는다. Release body/title은 untrusted input이며 로컬 feed의 public/private 필터링은 아직 보장하지 않는다.

Draft는 알림 대상에서 제외하며, prerelease는 metadata를 보존하고 현재 Release 정책에 포함한다. Live Release에는 숫자 ID가 필수다. 기존 ID 없는 fixture만 deterministic fixture 전용 fallback ID를 사용한다.

## 4. 모드와 GitHub Actions

| 실행 | 동작 |
| --- | --- |
| CLI 기본 `--mode preview` | 기존 DB를 메모리 복사본으로 읽고 수집·feed preview. 운영 DB/outbox/legacy cache/last notification 불변, Slack 0회 |
| CLI `--mode commit` | DB/event/outbox/migration metadata 갱신. `--send-slack` 지정 시 정책 후보를 실제 전송 |
| schedule | UTC `23:00`, `05:00`, `08:00`; commit + Slack |
| `workflow_dispatch` | 기본 preview; commit은 기본 브랜치에서만 허용 |

Workflow는 `contents: read`, 고정 concurrency group, `cancel-in-progress: false`, job timeout을 사용한다. Preview는 DB cache를 restore해 읽을 수 있지만 새 state cache를 저장하지 않으며 Slack secret을 주입받지 않는다. Commit은 legacy cache를 read-only로 restore하고 별도 DB cache namespace에 유효한 SQLite 파일을 저장한다. Slack 실패로 step이 실패해도 유효한 DB라면 save를 시도한다.

CLI의 `--sleep-seconds`/`--no-sleep`은 이전 호출과의 호환을 위한 deprecated no-op이며 새 collector의 repository pacing을 조정하지 않는다.

Actions cache는 **영구 상태 저장소가 아니다**. Eviction/stale restore/save 실패나 Slack 성공 직후 DB ack·cache save 전에 종료되면 중복 또는 상태 소실이 생길 수 있다. Concurrency는 실행 중인 run을 직렬화하지만 대기 run의 실행 순서·모두 실행됨을 보장하지 않는다. 따라서 이 구현은 exactly-once가 아니다.

## 5. 첫 실행과 migration

- `notification.first_run_notify` 기본값은 `false`다. cache miss나 처음 보는 repository의 기존 Release를 baseline으로 기록하고 대량 Slack 전송을 막는다.
- 기존 `.cache/releases.json`의 repo/tag/published는 과거 event ID가 아니라 migration 경계값이다. Migration marker는 DB에 한 번만 기록하며 legacy 파일이 있는 첫 cutover 실행은 Slack을 보내지 않는다. Legacy 파일이 없고 `first_run_notify: true`를 명시한 경우 첫 inventory 알림은 가능하다.
- malformed legacy cache나 SQLite DB/schema는 빈 상태로 자동 대체하지 않고 실패한다. 원본 cache는 삭제·덮어쓰지 않는다.
- 기본 브랜치 운영 rollout 및 rollback은 [P0 런북](P0_RUNBOOK.md)의 백업·대조 절차를 따른다. 오래된 legacy cache만으로 이전 workflow를 바로 재가동하면 중복 알림 위험이 있다.

## 6. 연동과 보안 경계

- `GH_PAT`와 `SLACK_WEBHOOK_URL`은 workflow Secrets 또는 실행 환경에서만 주입한다. 파일·로그·artifact·커밋에 값을 남기지 않는다.
- Python/SQLite가 신규·중복·전달 여부의 source of truth다. GitHub MCP는 선택적 read-only 수집면이다. LLM은 요약·분류·권고 초안만 만들며 상태 변경, Slack 전송, 정책 override를 하지 않는다.
- `.cache/release-feed.json`은 로컬 신뢰 경계에 남긴다. `scripts/export_knowledge_jsonl.py`는 현재 private/public 판별 없이 visibility를 표시할 수 있으므로 public-only 입력을 확인하지 않은 feed를 공개 Knowledge Store로 보내지 않는다(#12 후속).
- GitHub Release title/body/URL은 외부 저장소 작성자가 제어할 수 있는 데이터다. Shell code에 직접 expression으로 삽입하거나 LLM 지시로 취급하지 않는다.

## 7. 검증

저장소 루트에서 다음 명령을 실행한다. Fixture smoke command와 운영 절차는 [P0 런북](P0_RUNBOOK.md)에 있다.

```bash
python3 -m py_compile .github/scripts/check_release.py
python3 -m compileall -q .github/scripts starwatch
python3 -m unittest discover -s tests -v
git diff --check
```

실제 Slack webhook이나 운영 commit-mode workflow는 회귀 검증에 사용하지 않는다. Fake transport를 주입한 fixture 테스트로 성공·429·500·부분 실패·재시도를 확인한다.
