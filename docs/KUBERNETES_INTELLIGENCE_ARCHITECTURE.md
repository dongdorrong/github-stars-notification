# Kubernetes Intelligence — 구현 아키텍처

P0 PR #14 위에 P1/P2를 추가합니다. 운영 rollout 완료를 뜻하지 않습니다. 기본은 shadow입니다.

```text
starred inventory + explicit project registry
  → bounded Release / Global GHSA + withdrawn / optional repo GHSA
  → opt-in Discussions / labelled Issues / official HTTPS feeds
  → normalized authoritative event + provenance + visibility
  → SQLite v2 event revisions / outbox / cursors
  → deterministic priority floor
  → optional schema-validated AI or deterministic fallback (separate table)
  → audited route + rollout/visibility suppression
  → Slack chunks with exact event IDs → 2xx ack / retry
  → read-only public-default Knowledge JSONL
```

## 소유권

| 모듈 | 책임 |
| --- | --- |
| registry.py | schema/alias/legacy compatibility/classification |
| release_collector.py | P0 recent scan + overlap reconciliation + runtime |
| signal_collectors.py | read-only HTTP/page contract, safe error/deadline |
| advisories.py | GHSA normalization/mapping/time-window continuation |
| announcements.py | explicit source trust, cursor, RSS SSRF/XML 경계 |
| event_store.py | schema migration, revision/analysis/route/attempt 저장, outbox |
| analysis.py | 엄격한 AI JSON, envelope, bounded adapter/fallback/cache |
| routing.py | priority floor, suppression, digest window, Slack formatting |
| intelligence.py | 새 source의 page-atomic persistence, analysis/routing/rollout |
| pipeline.py | P0 cutover + mixed-event selection + acknowledgement |
| check_release.py | config/CLI, feed, safe outputs/metrics |
| export_knowledge_jsonl.py | 네트워크·migration 없는 readonly export |

## 상태 계약

SQLite v1→v2는 transaction 내 additive migration입니다. events/outbox/legacy metadata와 기존 delivery 상태를 보존합니다. 새 revision/analysis/routing/attempt 테이블과 suppression reason을 추가합니다. Preview는 readonly backup을 메모리로 옮긴 뒤 migration하며 원본 bytes를 바꾸지 않습니다. 손상 state는 초기화하지 않습니다.

Release identity는 `github:release:<id>`, GHSA는 `github:ghsa:<GHSA-ID>`입니다. 동일 GHSA revision도 identity를 변경하지 않습니다. Raw와 AI는 별도 저장하며 formatting/비실질 GHSA 변경의 revision 감사와 알림 재큐잉을 구분합니다. 전송 당시 hash/route/attempt를 기록합니다.

P0 legacy cutover는 Release-only ID 집합으로 판단하여 다른 signal이 먼저 존재해도 억제가 우회되지 않습니다. 원본 `.cache/releases.json`은 읽기 전용입니다. 기존 draft/pending/retry/partial-ack 계약을 보존합니다.

## 수집 보장과 예산

Release 최근 일반/특별 3/5 page, bootstrap1, known-only1; reconciliation2/4 page 및 overlap, 제한된 deterministic shard, 저장된 continuation. 전체 Release 예산900초(상한1200), 저장소60초. 매번 전체 history를 읽지 않습니다.

새 signal은 source별 page 상한과 추가 시간 예산을 갖되 invocation 전체1200초를 넘는 수집을 시작하지 않습니다. 전체 page 정규화가 끝난 경우에만 events와 proposed cursor를 함께 commit합니다. Global GHSA는 고정 modified window + Link continuation + overlap, withdrawn은 독립cursor입니다. Repo advisory/Discussion은 문서화된 since가 없으므로 updated 정렬·client boundary를 사용합니다. API 상세는 각 runbook에 있습니다.

신규 source 오류는 기존 Release G004 건강 판정에 합산하지 않습니다. Release fatal/DB/config 실패는 Slack을 차단합니다. 별도 source errors/deferred는 안전한 집계로 보고합니다.

## 분석 및 정책 경계

AI는 raw identity/trust/outbox를 변경할 수 없습니다. schema/hash/prompt/provider/model이 cache key입니다. 모델 입력은 untrusted envelope이며 도구/명령 실행을 제공하지 않습니다. Preview는 enabled 설정과 무관하게 fallback-only입니다.

분석은 개수/남은 시간으로 제한하며 endpoint 요청을 SQLite write transaction 밖에서 수행합니다. 예산 미만의 미분석 이벤트도 deterministic floor는 평가됩니다. 향후 실행은 cache를 재사용하며 deferred 분석을 이어갑니다.

CRITICAL/HIGH는 즉시, DIGEST는 수량5·최대대기24시간·KST17시 중 하나로 선택합니다. 실패 digest retry는 다음 window까지 기다리지 않습니다. Policy floor와 실제 route/suppression은 분리해 감사 기록을 남깁니다. Shadow는 새 신호를 영속 SUPPRESSED로 기록하며 full 전환이 기존 backlog를 자동 부활시키지 않습니다. 기존 Release는 shadow/canary에서 P0 선택을 유지합니다.

## 공개 범위

public/private/internal/unknown을 보존합니다. unknown은 public export/destination에서 차단됩니다. 현재 inventory visibility는 Release pending 평가에 우선합니다. Global GHSA 공개 source와 Repository Advisory의 접근 범위를 혼동하지 않습니다.

Actions는 원본 repo 이름 대신 keyed reference/ordinal과 집계만 출력합니다. inventory/feed/DB artifact 업로드는 기본 비활성입니다. Knowledge export는 readonly SQLite와 명시적인 visibility 검사, 구조적 값 redaction을 사용합니다.

## 잔여 운영 제약

- exactly-once 아님: Slack ack와 DB/cache durability 사이 crash window.
- Actions cache는 best-effort; eviction/stale restore 가능.
- 역사 발견은 반복 성공 실행에 따른 eventual reconciliation이며 최대 지연 보장 없음.
- optional API 권한/LLM 실품질/운영 canary는 별도 검증.
- v2 DB에 P0 code-only rollback 금지. [Rollout 런북](P1_P2_ROLLOUT_RUNBOOK.md) 참조.

## Collector interface compatibility

Collectors use source-specific page contracts, then normalize into the common event/provenance/store/routing boundary. GHSA, Discussions, Issues and RSS implement `Collector.fetch_page` / `CollectedPage`. The P0 Release compatibility adapter deliberately retains `ReleaseSource.fetch_page`, `RepositoryUpdate` and its existing bounded/reconciliation metadata. A single interchangeable plugin registration API is not claimed; mixed-source failure isolation, budgets and preview immutability are integration-tested.
