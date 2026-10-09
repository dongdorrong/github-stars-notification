# GitHub MCP와 로컬 LLM의 읽기·분석 경계

> GitHub API adapter는 공식 source의 **읽기**, Python/SQLite는 stable ID·cursor·outbox·routing·ack, LLM은 검증된 보조 요약을 맡는다. MCP는 현재 운영 pipeline의 필수 구성요소가 아니다. 실제 로컬 LLM endpoint는 이번 feature branch 검증에서 호출하지 않는다.

## 현재 연결

`check_release.py`가 생성하는 로컬 feed `github-stars-release-feed/v1`은 `new_releases[]`와 호환 alias `releases[]`(이번 실행 discovery), `pending_releases[]`(전송 전 eligible pending), `notification_batch[]`(생성된 Slack chunk의 ID 순서와 동일)를 구분한다. 외부 로컬 분석이 **현재 알림**을 볼 때는 `notification_batch[]`를 사용한다. Feed는 비공개 repo 내용을 포함할 수 있으므로 공개 artifact가 아니다.

Pipeline 내부의 `starwatch.analysis.AnalysisService`는 normalized event 사본을 고정 system policy와 분리한 `untrusted_event_json` envelope에 넣는다. 응답은 `schemas/ai-analysis-v1.json`에 맞는 JSON만 허용하고 event ID/hash, provider/model, prompt version, 절단 여부, UTC time은 코드가 채운다. 성공 결과와 결정적 fallback은 raw `events.payload_json`이 아니라 `ai_analyses`의 서로 다른 provider/model key에 저장한다. 캐시는 event ID/content hash/schema/prompt/provider/model에 의존한다. Preview는 설정상 AI가 켜져 있어도 fallback-only이며 실 endpoint를 호출하지 않는다.

```yaml
analysis:
  enabled: false
  provider: openai_compatible
  base_url_env: LLM_BASE_URL
  api_key_env: LLM_API_KEY
  model: local-model
  timeout_seconds: 30
  max_retries: 1
  fail_open_to_fallback: true
```

Endpoint와 key **값**은 환경/비밀 저장소에서만 가져온다. HTTP는 OpenAI-compatible `POST /v1/chat/completions`만 호출하며 redirect를 따르지 않는다. 401/403/429/5xx/timeout/invalid JSON은 안전 범주로 fallback하고 응답 오류 본문을 읽거나 기록하지 않는다. No tools, browsing, shell, GitHub write, Slack write are exposed to the model. AI의 `impact`는 결정적 보안 floor를 하향할 수 없고 SUPPRESSED도 승격하지 못한다. [로컬 LLM 런북](LOCAL_LLM_RUNBOOK.md)에 설정·fixture·실패 경계를 기록한다.

## GitHub read-only API와 선택적 MCP

운영 adapter는 `X-GitHub-Api-Version: 2022-11-28`을 고정하고 `GET` 및 고정된 read-only GraphQL `query`만 허용한다. Release는 pinned PyGithub 목록 page, Global GHSA는 `GET /advisories?modified=...`와 Link cursor, 옵션 Repository GHSA는 `GET /repos/{owner}/{repo}/security-advisories`, Issue는 `GET /repos/{owner}/{repo}/issues?since=...`, Discussion은 GraphQL `repository.discussions`의 `UPDATED_AT` 순서와 `pageInfo` cursor를 사용한다. GHSA/Discussion/Issue의 권한 실패는 해당 source의 safe category로 기록하고 원문 응답·token·URL은 공개 로그에 출력하지 않는다. Repository GHSA는 기본 비활성이다. GitHub rate limit은 response header와 403/429 범주로 처리하되 소스별 진행 상태를 잘못 전진시키지 않는다.

GitHub MCP를 로컬에서 별도 실험할 때는 공식 서버의 read-only mode와 필요한 toolset만 사용하고, 운영 cursor/event/outbox를 직접 쓰지 않는다. MCP 결과의 body/title도 untrusted data로 취급한다. MCP가 Slack 결정을 내리거나 `repos.txt`/SQLite를 무검증 대체하는 경로는 없다. 공식 [GitHub MCP server](https://github.com/github/github-mcp-server)와 [Copilot MCP 가이드](https://docs.github.com/en/copilot/how-tos/provide-context/use-mcp-in-your-ide/extend-copilot-chat-with-mcp)를 참고한다.

API 원문: [Global GHSA](https://docs.github.com/en/rest/security-advisories/global-advisories?apiVersion=2022-11-28), [Repository GHSA](https://docs.github.com/en/rest/security-advisories/repository-advisories?apiVersion=2022-11-28), [REST Issues](https://docs.github.com/en/rest/issues/issues?apiVersion=2022-11-28), [GraphQL Discussions](https://docs.github.com/en/graphql/guides/using-the-graphql-api-for-discussions), [REST pagination](https://docs.github.com/en/rest/using-the-rest-api/using-pagination-in-the-rest-api).

## 롤아웃 중지 조건

`config.yaml`의 기본 `intelligence.mode: shadow`, `analysis.enabled: false`를 유지하면 새 GHSA/announcement는 운영 Slack으로 가지 않는다. Feature branch preview는 DB save·Slack·실 LLM 호출을 하지 않아야 한다. 실제 endpoint 품질/가용성, Repository GHSA capability, 운영 Slack ack는 fixture만으로 증명할 수 없으며 승인된 [P1/P2 단계별 롤아웃](P1_P2_ROLLOUT_RUNBOOK.md)에서 별도 확인해야 한다. 원격 preview/PR CI 최종 HEAD 결과는 검증 전까지 미기재한다.
