# 🌟 GitHub Stars 릴리스 알림

<div align="center">

[![Workflow Status](https://github.com/dongdorrong/github-stars-notification/actions/workflows/notify-starred-releases.yml/badge.svg)](https://github.com/dongdorrong/github-stars-notification/actions)
[![GitHub stars](https://img.shields.io/github/stars/dongdorrong/github-stars-notification?style=social)](https://github.com/dongdorrong/github-stars-notification)

GitHub에서 스타를 준 저장소의 새로운 릴리스를 감지하고, <br>
정책에 맞는 경우 Slack으로 알려주는 GitHub Actions 자동화입니다. ✨

</div>

## 🤖 AI/OMX 세션 컨텍스트

다른 OMX/Codex 세션에서 이 저장소를 작업할 때는 아래 문서를 먼저 읽습니다.

- Repo-local agent guidance: [`AGENTS.md`](AGENTS.md)
- Project handoff/context: [`docs/AI_PROJECT_CONTEXT.md`](docs/AI_PROJECT_CONTEXT.md)
- Kubernetes Intelligence 목표 아키텍처: [`docs/KUBERNETES_INTELLIGENCE_ARCHITECTURE.md`](docs/KUBERNETES_INTELLIGENCE_ARCHITECTURE.md)
- 단계별 구현 로드맵: [`docs/KUBERNETES_INTELLIGENCE_ROADMAP.md`](docs/KUBERNETES_INTELLIGENCE_ROADMAP.md)
- 결정적 상태·AI 경계 ADR: [`docs/adr/0001-deterministic-event-state-and-ai-boundary.md`](docs/adr/0001-deterministic-event-state-and-ai-boundary.md)
- Codex P0 UltraGoal: [`docs/CODEX_ULTRAGOAL_KUBERNETES_INTELLIGENCE.md`](docs/CODEX_ULTRAGOAL_KUBERNETES_INTELLIGENCE.md)
- P0 운영/마이그레이션 런북: [`docs/P0_RUNBOOK.md`](docs/P0_RUNBOOK.md)
- GitHub MCP + 로컬 LLM 연동 설계: [`docs/GITHUB_MCP_LOCAL_LLM.md`](docs/GITHUB_MCP_LOCAL_LLM.md)
- 보안 레이어 후속 조치: [`docs/SECURITY_LAYERING_NOTES.md`](docs/SECURITY_LAYERING_NOTES.md)
- RAG/Knowledge Store 전환 TODO: [`docs/rag-todo.md`](docs/rag-todo.md)

## 🧭 Kubernetes Ecosystem Intelligence 확장 계획

현재 P0 구현은 starred repository의 최근 Release를 **제한된 페이지·실행 시간 예산** 안에서 incremental 수집하고 SQLite event/outbox에 보관합니다. 과거 페이지에 삽입되거나 발행 시각이 늦게 반영된 Release는 저장된 reconciliation 진행 상태를 통해 후속 실행에서 발견합니다. 한 번의 실행에서 전체 Release 이력을 확인했다는 뜻은 아닙니다. 이후 Kubernetes/Cloud Native 프로젝트 분류, GHSA, Maintainer Announcement, 선택적 로컬 LLM 분석, Critical/High/Digest routing으로 확장할 계획입니다.

```text
Starred repositories
  -> Kubernetes ecosystem classification
  -> Releases / GHSA / official announcements
  -> durable event store + notification outbox
  -> deterministic priority policy
  -> local LLM/Codex advisory analysis
  -> Critical / High / Digest Slack delivery
```

핵심 원칙:

- 원본 이벤트, notification state, AI 분석 결과를 분리합니다.
- LLM은 신규/중복 판정, 상태 변경, Slack 전송 여부를 결정하지 않습니다.
- Slack 성공 전에는 이벤트를 delivered 처리하지 않습니다.
- 수동 preview 실행은 운영 상태를 소비하지 않습니다.
- P0 workflow는 inventory/feed artifact를 업로드하지 않습니다. private/internal repository 정보의 public export 기본 제외는 #12 후속 작업입니다.

GitHub 백로그:

- Epic: [#3 Kubernetes ecosystem official intelligence watcher](https://github.com/dongdorrong/github-stars-notification/issues/3)
- P0: [#4 event/outbox](https://github.com/dongdorrong/github-stars-notification/issues/4), [#5 incremental releases](https://github.com/dongdorrong/github-stars-notification/issues/5), [#6 state-safe preview/workflow](https://github.com/dongdorrong/github-stars-notification/issues/6)
- P1: [#7 project registry](https://github.com/dongdorrong/github-stars-notification/issues/7), [#8 GHSA](https://github.com/dongdorrong/github-stars-notification/issues/8), [#9 local LLM](https://github.com/dongdorrong/github-stars-notification/issues/9), [#10 Slack routing](https://github.com/dongdorrong/github-stars-notification/issues/10)
- P2: [#11 maintainer announcements](https://github.com/dongdorrong/github-stars-notification/issues/11), [#12 visibility/Knowledge/CI](https://github.com/dongdorrong/github-stars-notification/issues/12)

> 위 다이어그램은 Epic #3의 목표 설계입니다. 이번 P0의 구현 범위는 Release 수집·상태·전달 안전성(#4~#6)이며 #7~#12는 아직 구현하지 않았습니다.

## 🎯 기능

- 🔍 GitHub 스타 저장소에서 최근 Release를 bounded pagination으로 수집하고 과거 페이지를 점진적으로 재확인 (GitHub Release ID 기준)
- 📦 전체 starred repository 메타데이터를 `.cache/stars-inventory.json`으로 생성 (private 정보 노출 방지를 위해 artifact 업로드 없음)
- ⏰ 하루 3번 자동 체크: 한국시간 08시, 14시, 17시 (UTC `23:00`, `05:00`, `08:00`)
- 💾 `.cache/events.sqlite3`의 event/outbox로 pending 누적·중복 방지·실패 재시도
- 💬 Slack Incoming Webhook 알림
- ⭐ 관심 프로젝트 강조 및 즉시 알림 정책
- 🧾 다른 앱/로컬 LLM이 읽을 수 있는 `.cache/release-feed.json` 생성
- 🧪 기본 preview와 token-free fixture로 운영 상태·Slack을 변경하지 않고 로컬 테스트 가능

<div align="center">

![GitHub Stars Notification](images/sample.png)

</div>

## ⚙️ 설정 방법

### 1️⃣ GitHub Personal Access Token (PAT) 생성

```bash
# Repository Secrets에 GH_PAT로 저장
# starred repo와 release 조회가 가능한 읽기 권한을 사용
```

### 2️⃣ Slack Webhook URL 설정

```bash
# Slack 워크스페이스에서 Incoming Webhook 생성
# Repository Secrets에 SLACK_WEBHOOK_URL로 저장
```

### 3️⃣ 관심 프로젝트와 알림 정책 설정

`config.yaml`에서 관심 프로젝트와 정책을 관리합니다.

```yaml
special_projects:
  - "kubernetes / kubernetes"
  - "grafana/grafana"

collector:
  per_page: 100
  max_incremental_pages_per_repo: 3
  max_incremental_pages_special_project: 5
  bootstrap_pages: 1
  known_only_pages_to_stop: 1
  global_budget_seconds: 900
  per_repo_budget_seconds: 60
  reconciliation_pages_per_repo: 2
  reconciliation_pages_special_project: 4
  max_reconciliation_repositories_per_run: 10
  reconciliation_shards: 8
  special_reconciliation_shards: 2

notification:
  min_release_count: 5
  special_project_always_notify: true
  first_run_notify: false
  cutover_pending_policy: suppress_existing
  max_slack_text_length: 35000

feed:
  output_path: ".cache/release-feed.json"

llm:
  enabled: false
  provider: "local"
  role: "summarize_and_prioritize_only"
```

정책 의미:

| 설정 | 의미 |
| --- | --- |
| `min_release_count` | 미전달 pending 릴리스가 이 개수 이상 누적되면 Slack 알림 후보 |
| `special_project_always_notify` | 관심 프로젝트 릴리스는 임계값 미만이어도 알림 |
| `first_run_notify` | 명시적으로 `true`로 설정할 때만 첫 수집의 기존 릴리스를 bootstrap 알림 대상으로 포함. 기본 `false`; legacy 최초 전환에서는 `cutover_pending_policy`가 우선 |
| `cutover_pending_policy` | 기본 `suppress_existing`: 최초 legacy→SQLite 전환 실행에서 발견한 Release를 저장하되 전부 `SUPPRESSED`로 처리. `preserve_pending`은 기존 legacy 날짜 경계 방식의 명시적 호환 옵션 |
| `feed.output_path` | 앱/로컬 LLM 연동용 deterministic JSON feed 경로 |

Collector 기본값은 최근 경로를 일반 저장소 최대 3 page, 관심 프로젝트 최대 5 page로 제한하고, 신규 저장소는 최근 1 page를 기준선으로 사용합니다. 한 번의 수집 예산은 900초(설정 상한 1,200초), 저장소당 60초입니다. 과거 페이지 reconciliation은 일반 저장소 8회 중 1회에 최대 2 page, 관심 프로젝트 2회 중 1회에 최대 4 page를 확인하되 한 실행에서 최대 10개 저장소만 deep scan합니다. 미뤄진 저장소와 reconciliation 진행 상태는 다음 commit 실행에 이어집니다. 이 정책은 한 실행의 완전한 이력 스캔이 아니라 API 비용과 지연 발견 사이의 절충입니다. 자세한 복구·관측 방법은 [P0 런북](docs/P0_RUNBOOK.md)을 참고하세요.

## 📬 알림 형식

새로운 릴리스가 정책을 만족하면 Slack 메시지가 전송됩니다.

```text
🚀 *새로운 릴리스 2개를 확인했습니다*
• ⭐ grafana/grafana <https://github.com/grafana/grafana/releases/tag/v12.0.0|`v12.0.0`> — Release v12.0.0 (2026-06-20) [github:release:120001]
• kubernetes/kubernetes <https://github.com/kubernetes/kubernetes/releases/tag/v1.34.0|`v1.34.0`> (2026-06-20) [github:release:120002]
```

표시 항목:

- 저장소 이름 (`owner/repo`)
- 릴리스 태그 링크
- 릴리스 이름(태그와 다를 때만)
- 발행 날짜 (`YYYY-MM-DD`)
- stable event ID (`github:release:<id>`)
- 관심 프로젝트 `⭐`

## 🧾 Release feed / 로컬 LLM 연동

`check_release.py`는 Slack 전송 여부와 무관하게 `.cache/release-feed.json`을 생성합니다. 이 파일은 신뢰할 수 있는 로컬/비공개 앱이나 로컬 LLM의 읽기 연결 지점입니다. Release title/body/URL은 외부 입력이므로 프롬프트·로그·공개 export에 그대로 신뢰하거나 게시하지 마세요.

Feed schema v1의 배열은 서로 다른 시점을 나타냅니다.

| 필드 | 의미 |
| --- | --- |
| `new_releases[]`, `new_release_count` | 이번 실행에서 처음 발견한 Release와 그 수. `releases[]`와 `release_count`는 기존 소비자용 **동일한 discovery alias**이며 알림 상태에 따라 의미가 바뀌지 않습니다. |
| `pending_releases[]`, `pending_release_count` | 전송 전 현재 알림 대상으로 선택할 수 있는 pending Release와 그 수. 지연 재시도 중인 event는 제외됩니다. |
| `notification_batch[]`, `notification_batch_count` | 생성된 Slack chunk가 표현하는 순서 그대로의 Release와 그 수. 알림 정책이 발동하지 않으면 빈 배열입니다. 부분 전송 실패 시에도 생성 당시 batch를 유지합니다. |
| `slack_chunks[]` | 각 생성 chunk의 `event_ids`와 `payload`. 모든 chunk의 `event_ids`를 순서대로 합치면 `notification_batch[].event_id`와 같습니다. |
| `pending_before_delivery_count`, `pending_count` | 지연 재시도를 포함한 전송 전·후 미전달 event 수. `pending_count`는 알림 후보 수가 아닙니다. |

현재 알림을 분석하는 로컬 LLM은 `notification_batch[]`를 사용합니다. `new_releases[]`는 이번 실행의 신규 수집 분석, `releases[]`는 기존 discovery 소비자와 Knowledge exporter의 호환용입니다. Knowledge exporter는 신규 수집분만 내보내며 누적 알림 batch를 내보내지 않습니다.

저장소별 수집 실패는 안전한 범주와 건수만 feed/Actions Step Summary에 남깁니다. 한 저장소 이상 완료되고 **시작한 저장소 중** 실패 비율이 50% 이하이며 모든 실패가 해당 저장소에 격리된 HTTP 404/429/5xx이면 정상 종료합니다. 오류나 예산으로 미룬 저장소가 있으면 `collection_degraded: true`입니다. HTTP 401/403, 미분류 오류, 50% 초과 실패, 완료 저장소 0건, 상태 검증 오류 또는 Slack 전송 실패는 종료 코드 1입니다. 치명적 수집 실패에서는 정상 수집 저장소의 event를 보존하되 Slack 전송은 하지 않습니다. 오류 정보에는 응답 본문·헤더·토큰·요청 URL을 포함하지 않습니다. 로컬 feed는 여전히 Release 원본 정보를 포함하므로 신뢰 경계 안에서만 읽습니다.

원칙:

- Python이 새 릴리스/중복/알림 여부를 결정합니다.
- 로컬 LLM은 요약, 분류, 중요도 초안만 작성합니다.
- GitHub MCP를 붙이더라도 읽기 전용 수집면으로 사용합니다.

자세한 설계는 [`docs/GITHUB_MCP_LOCAL_LLM.md`](docs/GITHUB_MCP_LOCAL_LLM.md)를 봅니다.

### fordongdorrong Knowledge export

생성된 `.cache/release-feed.json`을 GitHub API 재호출이나 Slack 전송 없이 중앙 Knowledge Store 계약(JSONL)으로 내보냅니다.

> 현재 exporter는 입력 Release를 공개 데이터로 판별·필터링하지 않습니다. private/internal 저장소가 섞인 feed에는 사용하지 말고, 신뢰할 수 있는 public-only 입력을 확인한 경우에만 실행하세요. 공개 범위 강제는 #12 후속 작업입니다.

```bash
./scripts/export_knowledge_jsonl.py \
  --feed .cache/release-feed.json \
  --output /tmp/github-stars.knowledge.jsonl
```

산출물은 `fordongdorrong` 환경에서 `fordong knowledge validate-export` / `import --dry-run`으로 검증합니다. 이 저장소의 로컬 검증에는 해당 외부 CLI가 포함되지 않습니다.

## 🚀 실행

### GitHub Actions

워크플로우는 schedule 또는 Actions 탭의 `Run workflow`로 실행됩니다. schedule은 commit mode로 상태를 갱신합니다. 수동 실행은 기본 `preview`이며 DB/outbox/legacy cache/last notification을 변경하거나 Slack을 호출하지 않습니다. 수동 commit은 운영 상태를 변경하므로 런북의 rollout 절차를 먼저 확인하세요.

매 실행에서 다음 로컬 파일을 만들지만, private starred repository 정보가 포함될 수 있어 artifact로 업로드하지 않습니다.

- `repos.txt`: `owner/repo` 전체 목록
- `.cache/stars-inventory.json`: description/topics/language/update 시각 등 분류용 메타데이터

수동 `commit`은 기본 브랜치에서만 허용됩니다. commit 경로는 상태 변경과 정책이 충족될 경우 Slack 전송을 함께 수행합니다. feature branch에서 검증할 때는 `preview`만 사용하세요. Action의 `.cache/events.sqlite3` 보존은 cache에 의존하므로 영구 내구성이나 exactly-once 전달 보장은 아닙니다.

CLI의 `--sleep-seconds`와 `--no-sleep`은 이전 호출과의 호환을 위해서만 받는 **deprecated no-op**입니다. 새 collector의 저장소별 pacing을 조정하지 않습니다. 새 명령에는 넣지 마세요.

### 로컬 fixture 테스트

실제 GitHub token 없이 preview 수집을 확인할 수 있습니다. 아래 fixture의 숫자 `id`는 live GitHub Release ID에 대응합니다.

```bash
tmp_dir="$(mktemp -d)"
cat > "$tmp_dir/repos.txt" <<'EOF'
grafana / grafana
other/repo
EOF

cat > "$tmp_dir/releases.json" <<'EOF'
{
  "grafana/grafana": {
    "id": 120001,
    "tag_name": "v12.0.0",
    "name": "Release v12.0.0",
    "published_at": "2026-06-20 10:00:00",
    "html_url": "https://github.com/grafana/grafana/releases/tag/v12.0.0"
  }
}
EOF

python3 .github/scripts/check_release.py \
  --repos-file "$tmp_dir/repos.txt" \
  --fixture-releases "$tmp_dir/releases.json" \
  --state-db "$tmp_dir/events.sqlite3" \
  --mode preview \
  --feed-path "$tmp_dir/release-feed.json" \
  --github-output "$tmp_dir/github-output.txt"
test ! -e "$tmp_dir/events.sqlite3"
```

### 실제 로컬 preview

```bash
mkdir -p .cache

gh api /user/starred --paginate \
  --jq '.[] | {full_name, description, html_url, language, topics, archived, disabled, fork, pushed_at, updated_at, stargazers_count, open_issues_count}' \
  > .cache/stars-inventory.jsonl
jq -s 'sort_by(.full_name)' .cache/stars-inventory.jsonl > .cache/stars-inventory.json
jq -r '.[].full_name' .cache/stars-inventory.json > repos.txt

python3 .github/scripts/check_release.py --mode preview
```

위 명령은 인증된 `gh` 및 스크립트용 `GH_TOKEN`이 실행 환경에 **이미** 주입된 경우에만 동작합니다. 이 live 예시는 token-free fixture 검증 범위에 포함되지 않으므로 인증된 환경에서 별도 확인이 필요합니다. 토큰과 webhook은 shell history, `.env`, Git 커밋에 남기지 않습니다. 실제 Slack 전송은 이 예시에 포함하지 않습니다. 상태 전이, 마이그레이션, rollback, 잔여 위험은 [P0 런북](docs/P0_RUNBOOK.md)을 참조하세요.

## ✅ 검증

```bash
python3 -m py_compile .github/scripts/check_release.py
python3 -m unittest discover -s tests -v
git diff --check
```

---

<div align="center">
Made with ❤️ by <a href="https://github.com/dongdorrong">dongdorrong</a>
</div>
