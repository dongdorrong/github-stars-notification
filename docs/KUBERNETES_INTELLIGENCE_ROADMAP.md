# Kubernetes Intelligence Roadmap

Epic #3은 PR 병합만으로 닫지 않습니다.

| 범위 | 구현 및 검증 책임 |
| --- | --- |
| P0 #4–#6 | PR #14 병합. SQLite pending/ack, bounded Release/reconciliation, preview/cutover 계약 유지 |
| P1 #7 | versioned registry, deterministic classification, aliases/legacy compatibility |
| P1 #8 | bounded GHSA, mapping, revisions, security floor, source isolation |
| P1 #9 | separate schema-valid AI/fallback/cache, prompt boundary |
| P1 #10 | audited CRITICAL/HIGH/DIGEST/SUPPRESSED, time/count routing, partial ack |
| P2 #11 | opt-in trusted Discussions/Issues/RSS with SSRF protection |
| P2 #12 | visibility/JSONL export, reproducible Python/deps, separate no-secret CI |

P1/P2 기능 브랜치의 완료 판단은 acceptance fixtures, migration regression, 독립 검토, exact-HEAD PR CI 및 readonly preview가 모두 green인 경우입니다. 작업 중 테스트나 과거 SHA의 preview를 최종 증거로 대체하지 않습니다.

## 운영 승인 단계 — 이 PR에서 실행하지 않음

1. main preview: write/Slack 없이 실제 설정 확인.
2. shadow commit: 새 signal과 분석 state 보존, 새 Slack 금지, 기존 Release 유지.
3. canary: 명시 프로젝트 allowlist와 별도 검증 destination, ack/retry 검토.
4. full: 운영자 승인 후 설정 활성화, scheduled cycle 및 no-flood 확인.
5. Epic closure: 공식 signal→policy→analysis→Slack ack의 운영 수용 완료 후 #3 checklist 갱신.

각 단계 rollback 및 v2 backup 조건은 [P1/P2 rollout runbook](P1_P2_ROLLOUT_RUNBOOK.md)을 따른다. 자동 patch/upgrade/cluster action/third-party comment는 범위 밖이다.
