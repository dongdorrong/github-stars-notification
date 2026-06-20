# AI Project Context — GitHub Stars Release Notification

> 이 문서는 다른 OMX/Codex 세션이 `/home/dongdorrong/github/private/github-stars-notification` 프로젝트를 빠르게 이해하고, 다른 레포지토리/애플리케이션과 연동 작업을 이어가기 위한 handoff 문서다.

## 1. 프로젝트 한 줄 요약

GitHub에서 star한 저장소 목록을 조회하고, 각 저장소의 최신 GitHub Release 변화를 감지해 정책에 맞으면 Slack으로 알림을 보내며, 다른 앱/로컬 LLM이 재사용할 수 있는 deterministic release feed를 남기는 자동화 repo다.

## 2. 현재 목적

- GitHub starred repositories의 release 변화를 주기적으로 확인한다.
- `.cache/releases.json`과 비교해 중복 알림을 막는다.
- 새 release가 `config.yaml`의 정책을 만족하면 Slack으로 사람이 읽기 쉬운 메시지를 보낸다.
- `config.yaml`에 등록한 관심 프로젝트는 Slack 메시지에서 `⭐`로 강조하고, 정책상 5개 미만이어도 바로 알릴 수 있다.
- `.cache/release-feed.json`을 생성해 향후 다른 프로젝트/애플리케이션, SQLite/PostgreSQL, 로컬 LLM 요약 파이프라인으로 확장할 수 있게 한다.

## 3. Repo 구조

```text
/home/dongdorrong/github/private/github-stars-notification/
├── .gitignore
├── AGENTS.md
├── README.md
├── config.yaml
├── docs/
│   ├── AI_PROJECT_CONTEXT.md
│   └── GITHUB_MCP_LOCAL_LLM.md
├── images/
│   └── sample.png
├── tests/
│   └── test_check_release.py
└── .github/
    ├── scripts/
    │   ├── check_release.py
    │   └── requirements.txt
    └── workflows/
        └── notify-starred-releases.yml
```

## 4. 핵심 파일별 역할

| 파일 | 역할 |
| --- | --- |
| `README.md` | 사용자용 프로젝트 설명, secrets 설정, 정책, 로컬 테스트, feed/LLM 연결 설명 |
| `AGENTS.md` | repo-local AI 작업 원칙과 안전 경계 |
| `config.yaml` | 관심 프로젝트, 알림 정책, feed 경로, LLM 역할 경계 설정 |
| `docs/AI_PROJECT_CONTEXT.md` | 다른 세션/레포 연동용 handoff 문서 |
| `docs/GITHUB_MCP_LOCAL_LLM.md` | GitHub MCP + 로컬 LLM 연동 결론과 안전 경계 |
| `docs/SECURITY_LAYERING_NOTES.md` | 치명 보안 조치 결과와 레이어별 후속 작업 코멘트 |
| `.github/workflows/notify-starred-releases.yml` | GitHub Actions 실행 트리거, dependency 설치, star 목록 조회, release 감지, Slack 전송, feed artifact 업로드 |
| `.github/scripts/check_release.py` | release 조회, cache 비교, 알림 정책 판단, Slack payload 및 release feed 생성 |
| `.github/scripts/requirements.txt` | `PyYAML`, `PyGithub` 버전 pin. `requests`는 직접 사용하지 않아 제거됨 |
| `tests/test_check_release.py` | token 없이 핵심 정책/캐시/feed 동작을 검증하는 unittest |
| `.gitignore` | `.cache/`, `repos.txt`, `.env`, Python runtime artifact 제외 |

## 5. Workflow 실제 흐름

현재 workflow 파일 기준:

```yaml
on:
  schedule: [cron: '0 23,5,8 * * *']
  workflow_dispatch:
```

주의:

- cron은 GitHub Actions 기준 UTC다.
- 현재 값은 **매일 한국시간 08:00, 14:00, 17:00** 실행이다.

실행 단계:

1. `actions/checkout@v4`
2. `actions/setup-python@v5` with `python-version: '3.x'`
3. `.github/scripts/requirements.txt` 설치
4. `.cache` restore/save 준비 (`actions/cache@v4`, run별 key + restore prefix)
5. `gh api /user/starred --paginate | jq -r '.[].full_name' > repos.txt`
6. `python .github/scripts/check_release.py`
7. `.cache/release-feed.json`을 `release-feed` artifact로 업로드
8. GitHub Step Summary에 결과 요약
9. `steps.detect.outputs.has_new == 'true'`면 `curl`로 `SLACK_WEBHOOK_URL`에 Slack payload 전송

## 6. Script 동작 상세

스크립트: `.github/scripts/check_release.py`

입력:

- `GH_TOKEN`: GitHub API token. fixture 테스트가 아니면 필수
- `GITHUB_OUTPUT`: GitHub Actions output file path. 로컬에서는 `--github-output`로 대체 가능
- `repos.txt`: workflow의 “List starred repos” step에서 생성
- `config.yaml`: 관심 프로젝트/알림/feed/LLM 설정
- `.cache/releases.json`: 이전 release state cache
- 선택: `--fixture-releases <json>`으로 token 없는 로컬 테스트 가능

출력:

- GitHub Actions outputs
  - `has_new=true|false`
  - `payloads=<Slack payload json array>`
  - `message_count=<count>`
  - `feed_path=<path>`
  - `release_count=<count>`
  - `special_release_count=<count>`
  - `notify_reason=<reason>`
- `.cache/releases.json` 갱신
- `.cache/release-feed.json` 생성
- 알림을 보낸 경우 `.cache/last_notification.txt` 갱신

주요 함수/개념:

| 함수/개념 | 역할 |
| --- | --- |
| `normalize_repo_name` | `owner / repo`를 `owner/repo`로 normalize |
| `load_config` | `config.yaml` 로드, 기본값 merge, 정책값 normalize |
| `get_github_release_fetcher` | PyGithub 기반 live release fetcher 생성 |
| `load_fixture_fetcher` | JSON fixture 기반 token-free fetcher 생성 |
| `detect_releases` | 현재 latest release를 cache와 비교해 새 release만 추림 |
| `decide_notification` | `min_release_count`, `special_project_always_notify`, `first_run_notify` 정책 적용 |
| `build_slack_payloads` | Slack text payload 생성 및 길이 기준 분할 |
| `build_release_feed` | 앱/로컬 LLM 연동용 deterministic JSON feed 생성 |

첫 실행 동작:

- `.cache/releases.json`이 없으면 `first_run=True`.
- latest release가 있는 모든 starred repo가 새 release 후보가 된다.
- `notification.first_run_notify=false`이면 캐시와 feed만 만들고 Slack은 보내지 않는다.
- 기본값은 기존 동작과 맞춰 `first_run_notify=true`다.

중복 방지:

- 일반 실행에서는 repo별 이전 `tag` 또는 `published`가 달라진 경우만 새 release로 본다.
- 새 릴리스가 임계값 미만이라 Slack을 보내지 않아도 cache는 갱신되므로, 같은 release가 다음 실행에서 반복 알림 후보가 되지 않는다.

## 7. 현재 설정 파일

`config.yaml`의 핵심 설정:

```yaml
special_projects:
  - "kubernetes / kubernetes"
  - "grafana/grafana"
  - "argoproj / argo-cd"
  - "kubernetes-sigs / karpenter"
  - "kubernetes-sigs / aws-load-balancer-controller"
  - "aws / amazon-vpc-cni-k8s"
  - "etcd-io / etcd"
  - "istio / istio"

notification:
  min_release_count: 5
  special_project_always_notify: true
  first_run_notify: true
  max_slack_text_length: 35000

feed:
  output_path: ".cache/release-feed.json"

llm:
  enabled: false
  provider: "local"
  role: "summarize_and_prioritize_only"
```

## 8. Secrets / 인증 경계

필수 GitHub Repository Secrets:

| Secret | 용도 |
| --- | --- |
| `GH_PAT` | `gh api /user/starred` 및 PyGithub API 호출 |
| `SLACK_WEBHOOK_URL` | Slack Incoming Webhook URL |

주의:

- 실제 token/webhook 값은 절대 repo에 커밋하지 않는다.
- `.env`가 필요하면 `.gitignore`에 의해 제외된다. 그래도 shell history와 파일 권한에 주의한다.
- 다른 레포지토리와 연동할 때도 secret은 각 실행 환경의 secret store를 사용한다.

## 9. GitHub MCP + 로컬 LLM 결론

이 프로젝트는 GitHub MCP와 로컬 LLM을 붙이기 좋은 구조지만, 역할을 분리해야 안전하다.

- GitHub MCP: starred repositories, repo metadata, workflow/release context를 읽는 선택적 수집면
- Python: cache/state/duplicate detection/notification policy의 source of truth
- Local LLM: release feed를 읽고 요약/분류/우선순위/문장 다듬기만 수행

구체 설계와 로컬 Docker 예시는 `docs/GITHUB_MCP_LOCAL_LLM.md`에 있다.

## 10. 다른 레포지토리/애플리케이션과 연동할 때의 경계

### 읽기 source

- starred repo 목록: 현재 GitHub CLI `gh api /user/starred`, 향후 GitHub MCP `list_starred_repositories` 가능
- latest release: 현재 PyGithub `get_latest_release()`
- 관심 프로젝트/정책: `config.yaml`
- 이전 상태: `.cache/releases.json`, 향후 SQLite/PostgreSQL event store 가능
- release feed: `.cache/release-feed.json`

### 쓰기 output

- 현재: Slack webhook
- 현재: GitHub Actions artifact `release-feed`
- 향후 후보:
  - SQLite/PostgreSQL release history table
  - 웹 UI/API용 release feed
  - Discord/Email/Notion/Velog 등 추가 채널
  - LLM 요약 결과 저장소(`llm_summary` 등 별도 필드/테이블)

### 권장 확장 방향

- release event 원본과 LLM 요약 결과를 분리한다.
- Slack output은 notifier adapter로 더 분리할 수 있다.
- 관심 프로젝트는 YAML 파일뿐 아니라 DB/API에서 관리할 수 있게 확장할 수 있다.
- cache file이 커지거나 여러 앱이 동시에 읽게 되면 SQLite/PostgreSQL로 state store를 옮긴다.

## 11. 로컬 검증 명령

문법 확인:

```bash
cd /home/dongdorrong/github/private/github-stars-notification
python3 -m py_compile .github/scripts/check_release.py
```

단위 테스트:

```bash
python3 -m unittest discover -s tests -v
```

fixture smoke test:

```bash
cat > /tmp/repos.txt <<'EOF'
grafana / grafana
other/repo
EOF

cat > /tmp/releases.json <<'EOF'
{
  "grafana/grafana": {
    "tag_name": "v12.0.0",
    "name": "Release v12.0.0",
    "published_at": "2026-06-20 10:00:00",
    "html_url": "https://github.com/grafana/grafana/releases/tag/v12.0.0"
  }
}
EOF

python3 .github/scripts/check_release.py \
  --repos-file /tmp/repos.txt \
  --fixture-releases /tmp/releases.json \
  --cache-path /tmp/releases-cache.json \
  --feed-path /tmp/release-feed.json \
  --github-output /tmp/github-output.txt \
  --no-sleep
```

실제 script 실행에는 아래가 필요하다.

```bash
export GH_TOKEN=...
export GITHUB_OUTPUT=/tmp/github-output.txt
gh api /user/starred --paginate | jq -r '.[].full_name' > repos.txt
python3 .github/scripts/check_release.py
```

## 12. 다음 세션에서 먼저 할 일

다른 OMX/Codex 세션이 시작하면 아래를 먼저 확인한다.

```bash
cd /home/dongdorrong/github/private/github-stars-notification
git status --short --branch
sed -n '1,220p' AGENTS.md
sed -n '1,320p' docs/AI_PROJECT_CONTEXT.md
sed -n '1,260p' docs/GITHUB_MCP_LOCAL_LLM.md
sed -n '1,220p' docs/SECURITY_LAYERING_NOTES.md
python3 -m py_compile .github/scripts/check_release.py
python3 -m unittest discover -s tests -v
```

workflow를 바꾸는 작업이면 추가로:

```bash
sed -n '1,240p' .github/workflows/notify-starred-releases.yml
sed -n '1,360p' .github/scripts/check_release.py
```
