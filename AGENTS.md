# AGENTS.md — github-stars-notification repo local guidance

이 저장소는 GitHub에서 사용자가 star한 저장소들의 최신 release를 감지하고 Slack으로 알림을 보내는 GitHub Actions 기반 자동화 프로젝트다. 다른 OMX/Codex 세션은 먼저 `docs/AI_PROJECT_CONTEXT.md`를 읽고 작업한다.

## 기본 언어와 톤

- 기본 응답 언어: 한국어.
- 이 repo는 운영 자동화 성격이 있으므로 추측보다 workflow/script/config 근거를 우선한다.
- Slack/GitHub 토큰과 webhook은 민감 정보다. 절대 파일에 저장하거나 커밋하지 않는다.

## 핵심 경로

| 목적 | 경로 |
| --- | --- |
| 프로젝트 루트 | `/home/dongdorrong/github/private/github-stars-notification` |
| GitHub Actions workflow | `.github/workflows/notify-starred-releases.yml` |
| release 감지 스크립트 | `.github/scripts/check_release.py` |
| Python dependency pin | `.github/scripts/requirements.txt` |
| 관심 프로젝트 설정 | `config.yaml` |
| 상세 handoff 문서 | `docs/AI_PROJECT_CONTEXT.md` |

## 현재 동작 요약

1. GitHub Actions가 `gh api /user/starred --paginate`로 star 저장소 목록을 `repos.txt`에 저장한다.
2. `.github/scripts/check_release.py`가 각 저장소의 latest release를 조회한다.
3. `.cache/releases.json`과 비교해 새 release를 찾는다.
4. 새 release가 있으면 Slack payload를 GitHub Actions output으로 기록한다.
5. Slack GitHub Action이 `SLACK_WEBHOOK`으로 메시지를 보낸다.

## 필요한 GitHub Secrets

- `GH_PAT`: GitHub API 접근용 PAT. README는 `repo:read` 권한 필요로 설명한다.
- `SLACK_WEBHOOK`: Slack Incoming Webhook URL.

## 작업 원칙

1. workflow 변경 시 `.github/workflows/notify-starred-releases.yml`과 README 설명을 함께 맞춘다.
2. script 변경 시 최소 `python -m py_compile .github/scripts/check_release.py`를 실행한다.
3. `config.yaml`의 repo 표기는 `owner/repo` 또는 `owner / repo`가 섞여 있으며 script가 `owner/repo`로 normalize한다.
4. 캐시/첫 실행/중복 알림 동작은 민감하므로 수정 전후로 `docs/AI_PROJECT_CONTEXT.md`의 “주의점”을 확인한다.
5. 다른 repo와 연동할 때는 이 프로젝트를 “GitHub starred release source + Slack notifier”로 보고, 쓰기 경계와 secret 경계를 분리한다.

## 안전 규칙

- `GH_PAT`, `SLACK_WEBHOOK`, Slack payload 예시의 실제 webhook URL은 커밋 금지.
- `.cache/`, `repos.txt`, GitHub Actions output 파일은 런타임 산출물이다. 명시 요청 없이는 repo에 추가하지 않는다.
- 알림 폭주를 막기 위해 first-run 동작과 cache 동작을 변경할 때는 dry-run 또는 fixture 테스트를 먼저 설계한다.

## 상세 문서

- 전체 프로젝트 컨텍스트: `docs/AI_PROJECT_CONTEXT.md`
