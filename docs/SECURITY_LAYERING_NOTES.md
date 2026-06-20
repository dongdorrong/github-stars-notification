# Security Layering Notes for OMX Sessions

> 목적: 치명 취약점은 현재 수정했고, 나머지는 다른 OMX 세션이 레이어를 나눠 개발할 때 놓치지 않도록 남기는 보안 작업 코멘트다.

## 이미 조치한 치명 경로

### 1. Slack payload shell injection 차단

- 위치: `.github/workflows/notify-starred-releases.yml`
- 배경: GitHub release title/name/url은 외부 저장소 관리자가 제어할 수 있는 값이다.
- 조치: `${{ toJSON(matrix) }}`를 inline shell single-quoted 문자열에 직접 삽입하지 않고 `env.SLACK_PAYLOAD`로 전달한다.
- 유지 규칙: workflow `run:` 블록 안에서 GitHub expression으로 외부 입력을 직접 문자열 보간하지 않는다. 필요한 값은 `env:`로 넘기고 shell에서는 double quote로 감싼다.

### 2. Snyk direct vulnerable dependency 제거

- 위치: `.github/scripts/requirements.txt`
- 배경: `requests==2.31.0`은 Snyk 기준 direct vulnerabilities가 있고, 현재 코드에서는 직접 import하지 않는다.
- 조치: direct dependency에서 제거했다.
- 유지 규칙: 나중에 HTTP client가 직접 필요해지면 최신 non-vulnerable 버전을 명시하고, `verify=False`/임시 파일 처리/credential forwarding 경로를 테스트한다.

## 레이어별 후속 작업 코멘트

### Layer 1 — Workflow hardening

- `permissions: contents: read`를 workflow 또는 job 단위로 명시한다.
- `actions/checkout`, `setup-python`, `cache`, `upload-artifact`는 운영 안정화 단계에서 full commit SHA pinning을 검토한다.
- GitHub Actions inline script에서는 `${{ ... }}` expression을 직접 shell code로 만들지 않는다.

### Layer 2 — Artifact/data boundary

- `.cache/release-feed.json` artifact에 private starred repo 이름, release URL, release title이 들어갈 수 있는지 확인한다.
- private repo가 포함될 수 있으면 feed 업로드를 config로 끄거나 private metadata redaction 레이어를 둔다.
- 로컬 앱/DB 연동 시 release 원본 이벤트와 LLM 요약 결과를 분리 저장한다.

### Layer 3 — Dependency/security scanning

- Snyk Open Source 또는 `pip-audit`를 CI에 추가해 requirements 변경 시 자동으로 실패시키는 gate를 둔다.
- Snyk Code/CodeQL/Semgrep 중 하나를 PR check로 추가해 shell injection, path traversal, unsafe YAML, secrets handling을 반복 점검한다.
- dependency update PR은 기능 변경 PR과 분리한다.

### Layer 4 — GitHub MCP / Local LLM integration

- GitHub MCP는 read-only collector로만 둔다.
- Python script가 cache/state/duplicate detection/notification decision의 source of truth다.
- LLM은 `.cache/release-feed.json`의 요약/분류/우선순위 초안만 맡는다.
- LLM output이 Slack 전송 여부, cache, GitHub Actions output을 직접 바꾸면 안 된다.

## 다음 세션에서 먼저 볼 파일

```bash
sed -n '1,140p' .github/workflows/notify-starred-releases.yml
sed -n '1,80p' .github/scripts/requirements.txt
sed -n '1,220p' docs/SECURITY_LAYERING_NOTES.md
```
