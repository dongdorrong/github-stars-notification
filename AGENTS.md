# AGENTS.md — github-stars-notification repo local guidance

이 저장소는 GitHub에서 사용자가 star한 저장소들의 최신 release를 감지하고 Slack으로 알림을 보내는 GitHub Actions 기반 자동화 프로젝트다. 다른 OMX/Codex 세션은 먼저 `docs/AI_PROJECT_CONTEXT.md`와 `docs/GITHUB_MCP_LOCAL_LLM.md`를 읽고 작업한다.

## 기본 언어와 톤

- 기본 응답 언어: 한국어.
- 이 repo는 운영 자동화 성격이 있으므로 추측보다 workflow/script/config 근거를 우선한다.
- Slack/GitHub 토큰과 webhook은 민감 정보다. 절대 파일에 저장하거나 커밋하지 않는다.

## 핵심 경로

| 목적 | 경로 |
| --- | --- |
| 프로젝트 루트 | `/home/dongdorrong/github/private/github-stars-notification` |
| GitHub Actions workflow | `.github/workflows/notify-starred-releases.yml` |
| release 감지/정책/Feed 스크립트 | `.github/scripts/check_release.py` |
| Python dependency pin | `.github/scripts/requirements.txt` |
| 관심 프로젝트/알림 정책 설정 | `config.yaml` |
| 상세 handoff 문서 | `docs/AI_PROJECT_CONTEXT.md` |
| GitHub MCP + 로컬 LLM 설계 | `docs/GITHUB_MCP_LOCAL_LLM.md` |
| 보안 레이어 후속 조치 | `docs/SECURITY_LAYERING_NOTES.md` |
| 테스트 | `tests/test_check_release.py` |

## 현재 동작 요약

1. GitHub Actions가 `gh api /user/starred --paginate`로 star 저장소 목록을 `repos.txt`에 저장한다.
2. `.github/scripts/check_release.py`가 각 저장소의 latest release를 조회한다.
3. `.cache/releases.json`과 비교해 새 release만 추린다.
4. `config.yaml`의 `notification` 정책으로 Slack 전송 여부를 결정한다.
5. Slack payload와 `.cache/release-feed.json`을 생성한다.
6. `has_new=true`이면 workflow가 `SLACK_WEBHOOK_URL`로 Slack 메시지를 보낸다.
7. workflow는 `release-feed` artifact를 업로드해 다른 앱/세션이 결과를 재사용할 수 있게 한다.

## 필요한 GitHub Secrets

- `GH_PAT`: GitHub API 접근용 PAT. starred repo와 release 조회가 가능한 읽기 권한을 사용한다.
- `SLACK_WEBHOOK_URL`: Slack Incoming Webhook URL.

## GitHub MCP / 로컬 LLM 경계

- GitHub MCP는 선택적 읽기 전용 수집면이다. `stargazers`, `repos`, `actions` toolset 정도만 우선 고려한다.
- Python 스크립트가 상태, 중복 방지, 알림 정책 판단의 source of truth다.
- 로컬 LLM은 `.cache/release-feed.json`을 읽어 요약/분류/우선순위 초안만 만든다.
- LLM이 `.cache/releases.json`, GitHub Actions output, Slack 전송 여부를 바꾸면 안 된다.

## 작업 원칙

1. workflow 변경 시 `.github/workflows/notify-starred-releases.yml`, README, `docs/AI_PROJECT_CONTEXT.md` 설명을 함께 맞춘다.
2. script 변경 시 최소 아래를 실행한다.
   ```bash
   python3 -m py_compile .github/scripts/check_release.py
   python3 -m unittest discover -s tests -v
   ```
3. `config.yaml`의 repo 표기는 `owner/repo` 또는 `owner / repo`가 섞여 있으며 script가 `owner/repo`로 normalize한다.
4. 캐시/첫 실행/중복 알림 동작은 민감하다. 수정 시 fixture 테스트를 먼저 추가하거나 갱신한다.
5. 다른 repo와 연동할 때는 이 프로젝트를 “GitHub starred release source + deterministic release feed + Slack notifier”로 보고, 쓰기 경계와 secret 경계를 분리한다.

## 안전 규칙

- `GH_PAT`, `SLACK_WEBHOOK_URL`, Slack webhook URL은 커밋 금지.
- `.cache/`, `repos.txt`, GitHub Actions output 파일은 런타임 산출물이다. 명시 요청 없이는 repo에 추가하지 않는다.
- 알림 폭주를 막기 위해 first-run 동작과 cache 동작을 변경할 때는 dry-run 또는 fixture 테스트를 먼저 설계한다.
- GitHub MCP를 붙일 때는 가능한 `GITHUB_READ_ONLY=1`과 최소 toolset을 사용한다.

## 상세 문서

- 전체 프로젝트 컨텍스트: `docs/AI_PROJECT_CONTEXT.md`
- GitHub MCP + 로컬 LLM 설계: `docs/GITHUB_MCP_LOCAL_LLM.md`
- 보안 레이어 후속 조치: `docs/SECURITY_LAYERING_NOTES.md`
