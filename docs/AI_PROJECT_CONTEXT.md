# AI Project Context — GitHub Stars Kubernetes Intelligence

> 구현 handoff: P0 Release 경로는 기존 운영 기준이다. P1/P2(#7~#12) 구현과 최종 HEAD 원격 preview·PR CI 증거는 [PR #15](https://github.com/dongdorrong/github-stars-notification/pull/15)에서 추적한다. main shadow/canary/full 운영은 아직 **미검증**이며 실행 승인도 없다. 설계와 운영 완료를 혼동하지 않는다.

## 목적·권위 경계

Starred repository의 공식 Release, 공개 GitHub Security Advisory와 명시적 maintainer announcement를 수집한다. Python/SQLite가 GitHub Release ID·GHSA ID·Discussion/Issue/RSS 안정 ID, 중복, cursor, outbox, retry, routing, Slack ack를 소유한다. 원본 사실과 revision은 AI 요약보다 우선한다. AI는 schema 검증을 통과한 한국어 요약·분류·권고만 만들며 이벤트 ID, source trust, 보안 floor, suppression, outbox, GitHub/Slack 쓰기 권한이 없다. 실제 LLM이 없어도 결정적 fallback으로 계속된다.

## 핵심 경로

| 경로 | 역할 |
| --- | --- |
| `.github/workflows/notify-starred-releases.yml` | schedule/preview/commit, starred inventory, secret/cache/Slack save 경계, concurrency |
| `.github/scripts/check_release.py`, `starwatch/pipeline.py` | 호환 CLI/feed, legacy 기준선, P0 수집·outbox·전달 orchestration |
| `starwatch/release_collector.py` | 최근 bounded Release scan, 점진 reconciliation, ID 중복 제거 |
| `starwatch/event_store.py` | SQLite v1→v2 migration, raw/revision/AI/routing/outbox/delivery audit |
| `starwatch/registry.py`, `config/projects.yaml` | 명시적 registry, aliases, classifier, legacy special 호환 |
| `starwatch/advisories.py`, `starwatch/announcements.py`, `starwatch/signal_collectors.py` | bounded GHSA 및 opt-in Discussion/Issue/RSS, read-only GitHub adapter |
| `starwatch/analysis.py`, `starwatch/routing.py`, `starwatch/intelligence.py` | schema 검증 AI/fallback, 결정적 priority floor, shadow/canary/full |
| `starwatch/security.py`, `scripts/export_knowledge_jsonl.py` | 구조적 redaction, visibility-gated read-only Knowledge JSONL |
| `config.yaml` | collector, notification, intelligence, analysis, routing, artifact 안전 기본값 |

## P0 호환 동작

- Release ID는 `github:release:<GitHub ID>`다. 일반/특별 프로젝트 최근 scan은 최대 3/5 page, bootstrap 1 page, global/per-repo budget 900/60초다. 별도 reconciliation은 일반 8회·특수 2회 성공 방문을 기준으로 shard를 선택하고 page overlap을 두어 후속 실행에서 backdated 이벤트를 점진적으로 찾는다. 한 실행에서 전체 이력 수집을 보장하지 않는다.
- 격리된 404/429/5xx가 시작 저장소의 50% 이하이고 최소 1개 완료면 `collection_degraded=true`, exit 0이다. 완료 0, 401/403, 미분류 오류, 과반 실패는 exit 1이며 Slack을 억제한다. 오류 본문·헤더·토큰·private URL은 공개 출력에 넣지 않는다.
- `min_release_count: 5`는 **미전달 pending 누적 수**에 적용된다. 4+1은 5개가 같은 후보가 된다. 기존 special project는 즉시 후보. Slack 2xx 확인 후 해당 chunk만 DELIVERED, 429 Retry-After·5xx/timeout·부분 성공은 retry를 유지한다. Exactly-once 보장은 없다.
- Feed `github-stars-release-feed/v1`: `releases[]`는 항상 `new_releases[]`의 discovery alias다. `pending_releases[]`는 전송 전 eligible pending, `notification_batch[]`는 `slack_chunks[].event_ids`의 정확한 순서 합집합이다. 현재 알림 분석은 `notification_batch[]`를 사용한다. `pending_count`는 지연 재시도를 포함한 전송 후 수다.
- `.cache/releases.json`은 삭제·수정하지 않는 read-only legacy boundary다. 첫 cutover 기본 `suppress_existing`은 관측 backlog를 저장하되 SUPPRESSED로 두고 Slack 전송을 막는다. 이후 새 Release만 정상 pending으로 들어간다. Malformed cache/DB는 빈 state로 교체하지 않는다.
- Schedule은 UTC 23:00/05:00/08:00. 수동 dispatch 기본 preview는 원본 DB를 메모리 복사본으로 읽고 운영 DB/outbox/cache/last notification을 바꾸거나 Slack을 호출하지 않는다. Commit은 기본 브랜치만 허용한다. `--sleep-seconds`/`--no-sleep`은 deprecated no-op이다.

## P1/P2 추가 동작과 상태

- `config/projects.yaml`의 명시 결정·ignore가 휴리스틱보다 우선한다. 기존 `special_projects`와 release floor가 충돌하면 fail closed한다. Classification report는 전체/visibility/tier/category/ambiguous 건수만 공개한다.
- Global GHSA는 primary public source다. Repository GHSA는 기본 off/권한 의존. `modified` window와 cursor pagination, overlap, source별 최대 3 page/120초 예산을 사용한다. 처음 관측한 cohort는 bootstrap 억제; GHSA 수정은 동일 ID의 revision으로 남긴다. Registry에 결정적으로 매핑되지 않은 권고는 Slack 후보가 아니다.
- Discussion/Issue/RSS는 registry에서 개별 opt-in·allowlist 없이는 수집하지 않는다. Maintainer trust는 원천/label/category/association에서 결정하며 AI가 높이지 못한다. RSS는 HTTPS allowlist, DNS/redirect/응답 크기/XML 경계를 적용한다.
- SQLite schema v2는 `event_revisions`, `ai_analyses`, `routing_decisions`, `delivery_attempts`, outbox `suppression_reason`을 추가한다. Preview는 RAM에서만 v1→v2 migration한다. Commit은 기존 v1을 transaction으로 업그레이드하고 event/ack 이력을 유지한다. **P0 코드는 v2 DB를 직접 열 수 없다**. 백업 없는 단순 코드 rollback은 안전하지 않다.
- `analysis.enabled: false` 기본. JSON schema `k8s-intelligence-analysis/v1`에 맞는 응답만 별도 저장하고 event/hash/schema/prompt/provider/model로 cache한다. Fallback도 별도 provider로 저장한다. AI는 deterministic CRITICAL/HIGH를 하향할 수 없다. `routing`은 CRITICAL/HIGH 즉시, DIGEST 5개/24시간/KST 17시, SUPPRESSED 사유 보존을 결정한다.
- `intelligence.mode: shadow` 기본은 **새 signal GHSA/announcement Slack을 억제**하고 Release P0 전달을 유지한다. Canary는 명시 프로젝트만, full은 승인된 정책 전체다. 이번 작업에서 canary/full 운영이나 실제 LLM/Slack 전송은 하지 않는다.
- Inventory/feed artifact upload 기본 off. 공개 Knowledge JSONL은 명시 public만, private/internal은 명시적 private destination+opt-in, unknown은 제외한다. DB export에서 `repository_visibility:<repo>` 최신 metadata는 이전 public event/revision/AI payload보다 우선한다. [Knowledge export](KNOWLEDGE_EXPORT.md)를 따른다.

## 운영·보안·검증

`GH_PAT`, Slack webhook, LLM URL/key 값은 Secret/환경에서만 주입한다. GitHub release/advisory/Issue/Discussion/RSS 본문은 untrusted이며 Actions inline shell source나 model system policy로 승격하지 않는다. Preview는 실 LLM 호출과 Slack transport 없이 fixture 또는 공식 read-only 수집만 한다. GitHub Actions cache eviction, Slack ack 후 cache save 실패, source rate limit/정렬 변화는 at-least-once 및 발견 지연 위험으로 남는다.

```bash
python3 -m py_compile .github/scripts/check_release.py
python3 -m compileall -q .github/scripts starwatch scripts
python3 -m unittest discover -s tests -v
git diff --check
```

세부 절차: [P0 런북](P0_RUNBOOK.md), [P1/P2 롤아웃](P1_P2_ROLLOUT_RUNBOOK.md), [Registry](PROJECT_REGISTRY.md), [Security Advisory](SECURITY_ADVISORY_RUNBOOK.md), [Announcements](MAINTAINER_ANNOUNCEMENT_SOURCES.md), [Local LLM](LOCAL_LLM_RUNBOOK.md), [Knowledge export](KNOWLEDGE_EXPORT.md). 현재 구현·최종 preview 수치와 PR CI 상태는 최종 HEAD에서 별도 확인해야 한다.
