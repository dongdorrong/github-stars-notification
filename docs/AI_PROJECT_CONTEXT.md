# AI Project Context — GitHub Stars Release Notification

> 이 문서는 다른 OMX/Codex 세션이 `/home/dongdorrong/github/private/github-stars-notification` 프로젝트를 빠르게 이해하고, 다른 레포지토리/애플리케이션과 연동 작업을 이어가기 위한 handoff 문서다.

## 1. 프로젝트 한 줄 요약

이 프로젝트는 사용자가 GitHub에서 star한 저장소 목록을 조회하고, 각 저장소의 최신 GitHub Release를 감지해 Slack으로 알림을 보내는 GitHub Actions 자동화 repo다.

## 2. 현재 목적

- GitHub starred repositories의 release 변화를 주기적으로 확인한다.
- 새 release가 감지되면 Slack으로 사람이 읽기 쉬운 메시지를 보낸다.
- `config.yaml`에 등록한 관심 프로젝트는 Slack 메시지에서 `⭐`로 강조한다.
- 향후 다른 프로젝트/애플리케이션과 연동해 release feed, 관심 프로젝트 관리, 알림 채널 확장 같은 기능으로 발전시킬 수 있다.

## 3. Repo 구조

```text
/home/dongdorrong/github/private/github-stars-notification/
├── AGENTS.md
├── README.md
├── config.yaml
├── docs/
│   └── AI_PROJECT_CONTEXT.md
├── images/
│   └── sample.png
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
| `README.md` | 사용자용 프로젝트 설명, secret 설정, Slack 알림 형식 설명 |
| `config.yaml` | 관심 프로젝트 목록. `special_projects`에 repo 이름을 등록 |
| `.github/workflows/notify-starred-releases.yml` | GitHub Actions 실행 트리거, dependency 설치, star 목록 조회, release 감지, Slack 전송, cache 저장 |
| `.github/scripts/check_release.py` | `repos.txt`를 읽고 latest release를 조회해 새 release Slack payload를 생성 |
| `.github/scripts/requirements.txt` | `PyYAML`, `requests`, `PyGithub` 버전 pin |
| `images/sample.png` | README용 알림 예시 이미지 |

## 5. Workflow 실제 흐름

현재 workflow 파일 기준:

```yaml
on:
  schedule: [cron: '0 9 * * 1']
  workflow_dispatch:
```

주의:

- cron은 GitHub Actions 기준 UTC다.
- 현재 값은 **매주 월요일 09:00 UTC** 실행이다.
- README에는 “매일 오전 9시 자동 체크”라고 되어 있어 현재 workflow와 설명이 다르다. 다음 수정 시 둘 중 하나를 맞춰야 한다.

실행 단계:

1. `actions/checkout@v4`
2. `actions/setup-python@v5` with `python-version: '3.x'`
3. `.github/scripts/requirements.txt` 설치
4. `.cache` restore
5. `gh api /user/starred --paginate | jq -r '.[].full_name' > repos.txt`
6. `python .github/scripts/check_release.py`
7. `steps.detect.outputs.has_new == 'true'`면 Slack 전송
8. `.cache` save

## 6. Script 동작 상세

스크립트: `.github/scripts/check_release.py`

입력:

- `GH_TOKEN`: GitHub API token
- `GITHUB_OUTPUT`: GitHub Actions output file path
- `repos.txt`: workflow의 “List starred repos” step에서 생성
- `config.yaml`: 관심 프로젝트 목록
- `.cache/releases.json`: 이전 release state cache

출력:

- `has_new=true|false`
- `payload=<Slack payload json escaped for GitHub Actions output>`
- `.cache/releases.json` 갱신

주요 함수:

| 함수 | 역할 |
| --- | --- |
| `normalize_repo_name` | `owner / repo`를 `owner/repo`로 normalize |
| `get_latest_release` | PyGithub로 latest release 조회. 404면 release 없음으로 처리 |
| `load_cache` / `save_cache` | `.cache/releases.json` 읽기/쓰기 |
| `is_first_run` | cache 파일이 없으면 첫 실행으로 판단 |
| `load_config` | `config.yaml` 로드 및 special project normalize |
| `format_release_info` | Slack block section 생성 |

첫 실행 동작:

- `.cache/releases.json`이 없으면 `first_run=True`
- 이때는 latest release가 있는 모든 starred repo가 `new_releases`에 포함된다.
- Slack header는 `🌟 *스타 저장소의 현재 릴리스 목록입니다*`가 된다.

일반 실행 동작:

- 이전 cache의 `published` 날짜보다 latest release 날짜가 더 최신이면 새 release로 판단한다.
- release는 `published` 기준 최신순 정렬된다.

## 7. 현재 설정 파일

`config.yaml`에는 다음 관심 프로젝트가 있다.

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
```

표기 공백은 script에서 normalize된다.

## 8. Secrets / 인증 경계

필수 GitHub Repository Secrets:

| Secret | 용도 |
| --- | --- |
| `GH_PAT` | `gh api /user/starred` 및 PyGithub API 호출 |
| `SLACK_WEBHOOK` | Slack Incoming Webhook URL |

주의:

- 실제 token/webhook 값은 절대 repo에 커밋하지 않는다.
- 다른 레포지토리와 연동할 때도 secret은 각 실행 환경의 secret store를 사용한다.
- 로컬 테스트용 `.env`가 필요하면 `.gitignore` 추가 후 사용해야 한다. 현재 repo에는 `.env`/`.gitignore`가 없다.

## 9. 알려진 주의점 / 개선 후보

아래는 현재 코드/문서에서 확인되는 주의점이다. 바로 수정하라는 의미가 아니라, 다음 세션이 오해하지 않도록 남긴다.

1. **README와 workflow schedule 불일치**
   - README: 매일 오전 9시
   - workflow: `0 9 * * 1` = 매주 월요일 09:00 UTC

2. **GitHub Actions cache key가 고정값**
   - restore/save key가 `${{ runner.os }}-releases-cache`로 고정되어 있다.
   - GitHub Actions cache는 key 처리 방식이 중요하므로, cache가 실제로 매 실행 최신 상태로 유지되는지 workflow run에서 확인해야 한다.

3. **첫 실행 시 알림량이 많을 수 있음**
   - cache가 없으면 release가 있는 모든 starred repo가 Slack에 전송된다.
   - 연동 앱으로 확장할 때는 first-run bootstrap과 notification을 분리하는 것이 안전할 수 있다.

4. **latest release 기준만 사용**
   - pre-release, draft release, tag-only release 정책은 명시되어 있지 않다.
   - PyGithub `get_latest_release()`가 반환하는 기준에 의존한다.

5. **테스트 구조 없음**
   - 현재 fixture/unit test가 없다.
   - script 수정이 커지면 GitHub API를 mock한 test가 필요하다.

## 10. 다른 레포지토리/애플리케이션과 연동할 때의 경계

이 프로젝트를 다른 앱과 연결할 때 권장 경계:

### 읽기 source

- starred repo 목록: GitHub API `/user/starred`
- latest release: GitHub Releases API via PyGithub
- 관심 프로젝트: `config.yaml`
- 이전 상태: `.cache/releases.json` 또는 향후 DB/state store

### 쓰기 output

- 현재: Slack webhook
- 향후 후보:
  - SQLite/PostgreSQL release history table
  - 웹 UI/API용 release feed
  - Discord/Email/Notion/Velog 등 추가 채널

### 권장 확장 방향

- `check_release.py`의 release detection 로직과 Slack formatting 로직을 분리한다.
- release state를 `.cache/releases.json`만이 아니라 DB에도 저장할 수 있게 adapter를 둔다.
- Slack output은 notifier adapter로 분리한다.
- 관심 프로젝트는 YAML 파일뿐 아니라 DB/API에서 관리할 수 있게 확장한다.

## 11. 로컬 검증 명령

문법 확인:

```bash
cd /home/dongdorrong/github/private/github-stars-notification
python3 -m py_compile .github/scripts/check_release.py
```

dependency 설치 검증이 필요하면, 네트워크 접근이 가능한 환경에서:

```bash
python3 -m pip install -r .github/scripts/requirements.txt
```

실제 script 실행에는 아래가 필요하다.

```bash
export GH_TOKEN=...
export GITHUB_OUTPUT=/tmp/github-output.txt
gh api /user/starred --paginate | jq -r '.[].full_name' > repos.txt
python .github/scripts/check_release.py
```

단, 로컬에서 실제 token을 사용할 때는 shell history/secrets 노출에 주의한다.

## 12. 다음 세션에서 먼저 할 일

다른 OMX/Codex 세션이 시작하면 아래를 먼저 확인한다.

```bash
cd /home/dongdorrong/github/private/github-stars-notification
git status --short --branch
sed -n '1,220p' AGENTS.md
sed -n '1,280p' docs/AI_PROJECT_CONTEXT.md
python3 -m py_compile .github/scripts/check_release.py
```

workflow를 바꾸는 작업이면 추가로:

```bash
sed -n '1,240p' .github/workflows/notify-starred-releases.yml
sed -n '1,220p' .github/scripts/check_release.py
```
