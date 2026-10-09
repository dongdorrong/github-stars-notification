# AGENTS.md — github-stars-notification repo local guidance

이 저장소는 GitHub에서 사용자가 star한 저장소들의 Release를 수집하고 Slack으로 알림을 보내는 GitHub Actions 기반 자동화 프로젝트다. 다른 OMX/Codex 세션은 먼저 `docs/AI_PROJECT_CONTEXT.md`와 Kubernetes Intelligence 설계 문서를 읽고 작업한다.

## 기본 언어와 톤

- 기본 응답 언어: 한국어.
- 이 repo는 운영 자동화 성격이 있으므로 추측보다 workflow/script/config 근거를 우선한다.
- Slack/GitHub 토큰과 webhook은 민감 정보다. 절대 파일에 저장하거나 커밋하지 않는다.

## 핵심 경로

| 목적 | 경로 |
| --- | --- |
| GitHub Actions workflow | `.github/workflows/notify-starred-releases.yml` |
| release 감지/정책/Feed 스크립트 | `.github/scripts/check_release.py` |
| Python dependency pin | `.github/scripts/requirements.txt` |
| 관심 프로젝트/알림 정책 설정 | `config.yaml` |
| 상세 handoff 문서 | `docs/AI_PROJECT_CONTEXT.md` |
| Kubernetes Intelligence 아키텍처 | `docs/KUBERNETES_INTELLIGENCE_ARCHITECTURE.md` |
| Kubernetes Intelligence 로드맵 | `docs/KUBERNETES_INTELLIGENCE_ROADMAP.md` |
| 상태·AI 경계 ADR | `docs/adr/0001-deterministic-event-state-and-ai-boundary.md` |
| P0 Codex UltraGoal | `docs/CODEX_ULTRAGOAL_KUBERNETES_INTELLIGENCE.md` |
| P0 운영/마이그레이션 런북 | `docs/P0_RUNBOOK.md` |
| GitHub MCP + 로컬 LLM 설계 | `docs/GITHUB_MCP_LOCAL_LLM.md` |
| 보안 레이어 후속 조치 | `docs/SECURITY_LAYERING_NOTES.md` |
| 테스트 | `tests/test_check_release.py`, `tests/test_knowledge_export.py` |

## 현재 동작 요약

1. GitHub Actions가 `gh api /user/starred --paginate`로 star 저장소 목록을 `repos.txt`에 저장한다.
2. Release collector가 저장소별 목록을 끝까지 페이지 조회하고 GitHub Release ID로 중복을 제거한다.
3. `check_release.py`의 commit mode는 `.cache/events.sqlite3`의 event/outbox를 갱신한다. 기존 `.cache/releases.json`은 삭제·덮어쓰기 없이 마이그레이션 기준선으로만 읽는다.
4. 임계값 미만 event는 outbox에 남아 다음 실행까지 누적된다. Slack 2xx 확인 후에만 delivered로 바뀌며 429/5xx/timeout은 재시도 대상이다.
5. 수동 실행의 기본 preview는 수집 결과·feed를 보여주되 event DB, outbox, legacy cache, last notification을 변경하거나 Slack을 호출하지 않는다.
6. workflow는 private starred repository 정보 노출 위험 때문에 inventory와 release feed를 artifact로 업로드하지 않는다. CLI가 만드는 로컬 feed는 신뢰할 수 있는 소비자만 사용한다.

이 절은 **P0 Release 경로**만 설명한다. GHSA, registry, AI 분석, Critical/High/Digest routing, public visibility filtering은 후속 이슈 #7~#12이며 현재 구현으로 가정하지 않는다. 정확한 운영·복구 절차는 `docs/P0_RUNBOOK.md`를 따른다.

## Kubernetes Intelligence 목표

Epic #3은 프로젝트를 다음 구조로 확장한다.

```text
starred inventory
  -> Kubernetes ecosystem classification
  -> Releases / GHSA / maintainer announcements
  -> durable event store + notification outbox
  -> deterministic priority policy
  -> optional local LLM/Codex advisory analysis
  -> Critical / High / Digest Slack delivery
```

우선순위:

- P0 #4: durable event/outbox와 no-loss delivery
- P0 #5: 모든 unseen Release incremental 수집
- P0 #6: preview/concurrency/state safety
- P1 #7~#10: project registry, GHSA, AI analysis, 확장 Slack routing
- P2 #11~#12: maintainer announcements, visibility/Knowledge/CI hardening

P0 작업을 시작할 때는 `docs/CODEX_ULTRAGOAL_KUBERNETES_INTELLIGENCE.md`를 실행 기준으로 사용한다.

## 상태와 AI의 강제 경계

- Normalized raw event가 원본 사실의 source of truth다. P0의 수집 원천은 GitHub Release다.
- Python/SQLite가 event identity, 중복, outbox, retry, delivered 상태를 소유한다.
- Slack 2xx 확인 전에는 delivered 처리하지 않는다. Actions cache는 영구 저장소가 아니므로 exactly-once 보장은 없다.
- 수동 preview는 운영 state와 Slack을 변경하지 않는다.
- LLM은 요약, 카테고리, 운영 영향, 확인 권고만 작성한다.
- LLM은 신규/중복 판정, outbox 상태 변경, Slack 직접 호출, deterministic severity 하향을 수행하지 않는다.
- GitHub title/body, release note, advisory description은 untrusted input으로 취급한다.

## 필요한 GitHub Secrets

- `GH_PAT`: GitHub API 접근용 PAT. starred repo와 release 조회가 가능한 읽기 권한을 사용한다.
- `SLACK_WEBHOOK_URL`: Slack Incoming Webhook URL.

향후 로컬 LLM을 활성화하더라도 API key는 Actions Secret 또는 로컬 secret store로만 주입한다.

## GitHub MCP / 로컬 LLM 경계

- GitHub MCP는 선택적 읽기 전용 수집면이다. `stargazers`, `repos`, `actions` toolset 정도만 우선 고려한다.
- Python 스크립트가 상태, 중복 방지, 알림 정책 판단의 source of truth다.
- 로컬 LLM은 normalized event/feed를 읽어 요약/분류/우선순위 초안만 만든다.
- LLM이 cache/event DB, GitHub Actions output, Slack 전송 여부를 바꾸면 안 된다.
- Codex는 구현·테스트·중요 이벤트 deep analysis에 사용하고 매 이벤트의 state controller로 사용하지 않는다.

## 작업 원칙

1. workflow 변경 시 `.github/workflows/notify-starred-releases.yml`, README, `docs/AI_PROJECT_CONTEXT.md`, `docs/P0_RUNBOOK.md` 설명을 함께 맞춘다.
2. script 변경 시 최소 아래를 실행한다.
   ```bash
   python3 -m py_compile .github/scripts/check_release.py
   python3 -m unittest discover -s tests -v
   git diff --check
   ```
3. `config.yaml`의 repo 표기는 `owner/repo` 또는 `owner / repo`가 섞여 있으며 script가 `owner/repo`로 normalize한다.
4. 캐시/첫 실행/중복 알림 동작은 민감하다. 수정 시 fixture test와 상태 전이 test를 먼저 추가하거나 갱신한다.
5. 다른 repo와 연동할 때는 이 프로젝트를 “GitHub official-signal source + deterministic event/outbox + Slack notifier”로 보고, 쓰기 경계와 secret 경계를 분리한다.
6. P0 migration은 기존 `.cache/releases.json`을 삭제하거나 원본 위에 덮어쓰지 않는다. DB/cache가 malformed이면 빈 상태로 대체하지 말고 fail closed한다.
7. workspace가 dirty하면 기존 사용자 변경을 건드리지 말고 별도 worktree를 사용한다.
8. main에 직접 commit하지 않고 feature branch/PR을 사용한다.
9. 구현되지 않은 roadmap 항목을 완료됐다고 문서화하지 않는다.

## 안전 규칙

- `GH_PAT`, `SLACK_WEBHOOK_URL`, Slack webhook URL, LLM API key는 커밋 금지.
- `.cache/`, `repos.txt`, GitHub Actions output 파일은 런타임 산출물이다. 명시 요청 없이는 repo에 추가하지 않는다.
- 알림 폭주를 막기 위해 first-run/cache-miss 동작은 fail-safe를 기본으로 한다.
- preview 실행은 state mutation과 Slack 전송이 없어야 한다.
- GitHub MCP를 붙일 때는 가능한 `GITHUB_READ_ONLY=1`과 최소 toolset을 사용한다.
- 외부 입력을 GitHub Actions inline shell code에 직접 expression 보간하지 않는다.
- P0 workflow는 inventory/feed artifact를 업로드하지 않는다. public Knowledge export의 private/internal filtering은 #12 작업 전까지 보장하지 않는다.
- malformed DB/cache를 자동 빈 state로 교체해 과거 상태를 잃지 않는다.

## 상세 문서

- 전체 프로젝트 컨텍스트: `docs/AI_PROJECT_CONTEXT.md`
- 목표 아키텍처: `docs/KUBERNETES_INTELLIGENCE_ARCHITECTURE.md`
- 단계별 로드맵: `docs/KUBERNETES_INTELLIGENCE_ROADMAP.md`
- 상태·AI 결정: `docs/adr/0001-deterministic-event-state-and-ai-boundary.md`
- Codex P0 실행 프롬프트: `docs/CODEX_ULTRAGOAL_KUBERNETES_INTELLIGENCE.md`
- P0 운영/마이그레이션: `docs/P0_RUNBOOK.md`
- GitHub MCP + 로컬 LLM 설계: `docs/GITHUB_MCP_LOCAL_LLM.md`
- 보안 레이어 후속 조치: `docs/SECURITY_LAYERING_NOTES.md`
