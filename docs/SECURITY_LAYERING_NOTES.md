# Security Layering Notes for OMX Sessions

> 목적: 치명 취약점은 현재 수정했고, 나머지는 다른 OMX 세션이 레이어를 나눠 개발할 때 놓치지 않도록 남기는 보안 작업 코멘트다.

## 이미 조치한 치명 경로

### 1. Slack payload shell injection 차단

- 위치: `.github/workflows/notify-starred-releases.yml`
- 배경: GitHub release title/name/url은 외부 저장소 관리자가 제어할 수 있는 값이다.
- 당시 조치: `${{ toJSON(matrix) }}`를 inline shell single-quoted 문자열에 직접 삽입하지 않고 `env.SLACK_PAYLOAD`로 전달했다. P0에서는 별도 workflow notify job을 없애고 Python notifier가 payload를 전송한다.
- 유지 규칙: workflow `run:` 블록 안에서 GitHub expression으로 외부 입력을 직접 문자열 보간하지 않는다. 필요한 값은 `env:`로 넘기고 shell에서는 double quote로 감싼다.

### 2. Snyk direct vulnerable dependency 제거

- 위치: `.github/scripts/requirements.txt`
- 배경: `requests==2.31.0`은 Snyk 기준 direct vulnerabilities가 있고, 현재 코드에서는 직접 import하지 않는다.
- 조치: direct dependency에서 제거했다.
- 유지 규칙: 나중에 HTTP client가 직접 필요해지면 최신 non-vulnerable 버전을 명시하고, `verify=False`/임시 파일 처리/credential forwarding 경로를 테스트한다.

## 레이어별 후속 작업 코멘트

### Layer 1 — Workflow hardening

- P0 적용: `permissions: contents: read`, 고정 concurrency group, `cancel-in-progress: false`, timeout, 수동 preview 기본값과 기본 브랜치 commit 제한.
- `actions/checkout`, `setup-python`, `cache`, `upload-artifact`는 운영 안정화 단계에서 full commit SHA pinning을 검토한다.
- GitHub Actions inline script에서는 `${{ ... }}` expression을 직접 shell code로 만들지 않는다.

### Layer 2 — Artifact/data boundary

- P0 workflow는 private starred repo 이름·Release URL·title의 노출 가능성 때문에 inventory/feed artifact 업로드를 하지 않는다.
- 로컬 `.cache/release-feed.json`과 Knowledge exporter의 visibility filtering은 아직 보장하지 않는다. public-only 입력 확인 없이 공개 export하지 않는다(#12).
- 로컬 앱/DB 연동 시 release 원본 이벤트와 LLM 요약 결과를 분리 저장한다.

### Layer 3 — Dependency/security scanning

- Snyk Open Source 또는 `pip-audit`를 CI에 추가해 requirements 변경 시 자동으로 실패시키는 gate를 둔다.
- Snyk Code/CodeQL/Semgrep 중 하나를 PR check로 추가해 shell injection, path traversal, unsafe YAML, secrets handling을 반복 점검한다.
- dependency update PR은 기능 변경 PR과 분리한다.

### Layer 4 — GitHub MCP / Local LLM integration

- GitHub MCP는 read-only collector로만 둔다.
- Python/SQLite가 event/outbox, 중복 판정, notification decision의 source of truth다.
- LLM은 `.cache/release-feed.json`의 요약/분류/우선순위 초안만 맡는다.
- LLM output이 Slack 전송 여부, event DB/outbox, GitHub Actions output을 직접 바꾸면 안 된다.

### Layer 5 — P0 상태 보존 잔여 위험

- `.cache/events.sqlite3`는 Actions cache에 보존되지만 cache eviction/stale restore/save failure가 가능하다. 영구 DB나 exactly-once 보장으로 문서화하지 않는다.
- Slack 성공 직후 DB acknowledgement 또는 cache save 전 실패하면 중복 전송될 수 있다. 이전 `.cache/releases.json`은 P0 이후 갱신되지 않으므로 rollback 때 이전 workflow를 바로 재가동하지 않는다.
- malformed DB/legacy cache는 빈 상태로 자동 대체하지 않는다. 운영 절차는 [P0 런북](P0_RUNBOOK.md)을 따른다.

## 다음 세션에서 먼저 볼 파일

```bash
sed -n '1,140p' .github/workflows/notify-starred-releases.yml
sed -n '1,80p' .github/scripts/requirements.txt
sed -n '1,220p' docs/SECURITY_LAYERING_NOTES.md
```
