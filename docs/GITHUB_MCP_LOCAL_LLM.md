# GitHub MCP + 로컬 LLM 연동 설계

> 결론: **GitHub MCP는 GitHub 데이터 수집면**, **Python 스크립트는 상태/중복/알림 제어면**, **로컬 LLM은 요약/분류/우선순위 보조면**으로 둔다. LLM이 캐시를 바꾸거나 알림 전송 여부를 결정하지 않는다.

## 왜 이렇게 나누나

이 프로젝트는 “새 릴리스인지 아닌지”와 “이미 알림을 보냈는지”가 핵심이다. 이 둘은 결정적이어야 하므로 LLM에 맡기지 않는다. 대신 LLM은 사람이 읽는 부분을 개선하는 데 쓴다.

| 계층 | 맡는 일 | 금지할 일 |
| --- | --- | --- |
| GitHub MCP / `gh` / PyGithub | starred repo, release, workflow context 조회 | 캐시 변경, Slack 전송 결정 |
| `.github/scripts/check_release.py` + `starwatch/` | SQLite event/outbox, 중복 방지, 정책 판단, Slack payload·응답, feed 생성 | 자연어 요약 품질에 집착하기 |
| 로컬 LLM | release feed 요약, 카테고리 분류, 중요도 초안, 메시지 문장 다듬기 | 새 릴리스 판정, 알림 임계값 override, 상태 파일 수정 |

## 현재 구현된 연결 지점

`check_release.py`는 매 실행마다 deterministic JSON feed를 만든다.

```text
.cache/release-feed.json
```

feed에는 다음 계약이 들어간다.

- `releases[]`: 이번 실행에서 새 ID로 수집한 릴리스 목록 (미전달 누적 전체는 `pending_count`로 표시)
- `notify`, `notify_reason`: Python 정책 엔진의 알림 판단
- `policy`: `config.yaml`에서 읽은 알림 정책
- `mode`, `pending_before_delivery_count`, `pending_count`, `delivery_succeeded`: 실행 모드, 전송 전/후 미전달 수, 전달 결과. `pending_count`는 지연 재시도 중인 event를 포함한 전송 후 수다.
- `llm_contract`: 로컬 LLM의 허용 작업과 변경하면 안 되는 상태·전달 경계
- `mcp_contract`: GitHub MCP의 선택적 읽기 전용 수집 경계

P0 GitHub Actions는 잠재적으로 private starred repository metadata가 포함되는 feed/inventory를 artifact로 업로드하지 않는다. `.cache/release-feed.json`과 `.cache/stars-inventory.json`은 로컬 실행 산출물이다. public/private 분류가 강제되기 전에는 신뢰할 수 있는 소비자에게만 전달한다(#12 후속).

## 로컬 LLM에 넘길 프롬프트 예시

```text
아래 JSON은 github-stars-notification의 deterministic release feed다.

너의 역할:
- releases[]를 DevOps/Kubernetes/Observability/Security/AI 등으로 분류한다.
- 사람이 오늘 확인할 우선순위를 1~5로 제안한다.
- Slack 또는 블로그 소재용 요약을 한국어로 짧게 만든다.

금지:
- notify 값을 바꾸지 않는다.
- 새 릴리스/중복 여부를 다시 판정하지 않는다.
- cache/state 파일 수정을 제안하지 않는다.

JSON:
<.cache/release-feed.json 내용>
```

## GitHub MCP를 붙일 때

공식 GitHub MCP 서버는 원격 서버와 로컬 Docker 실행을 모두 제공한다. 로컬에서 붙일 때는 읽기 전용과 필요한 toolset만 켜는 쪽이 안전하다.

예시 Docker 실행 경계:

```bash
export GITHUB_PAT=...

docker run -i --rm \
  -e GITHUB_PERSONAL_ACCESS_TOKEN="$GITHUB_PAT" \
  -e GITHUB_READ_ONLY=1 \
  -e GITHUB_TOOLSETS="stargazers,repos,actions" \
  ghcr.io/github/github-mcp-server
```

권장 사용:

1. MCP의 `list_starred_repositories` 같은 읽기 도구로 starred repo 후보를 가져온다.
2. 결과를 `repos.txt`와 `.cache/stars-inventory.json` 같은 deterministic collector output으로 저장한다.
3. `check_release.py`와 `starwatch/`가 모든 Release를 수집하고 event DB/outbox에서 중복·알림·전달 상태를 판단한다.
4. `.cache/release-feed.json`을 로컬 LLM에 넘겨 요약을 만든다.

## 고도화 포인트

- GitHub Actions 운영 경로는 `gh api` + PyGithub Release 목록 수집을 유지한다.
- 로컬 실험 경로는 MCP/로컬 LLM을 붙여도 된다.
- P0의 event/outbox는 SQLite에 저장한다. 다른 애플리케이션과 공유하거나 영구 내구성이 필요할 때 별도 저장소·복제 방식을 검토한다.
- LLM 결과는 `llm_summary` 같은 별도 필드/테이블에 저장하고, release event 원본과 분리한다.

## 참고 링크

- GitHub 공식 MCP 서버: <https://github.com/github/github-mcp-server>
- GitHub Copilot MCP 문서: <https://docs.github.com/en/copilot/how-tos/provide-context/use-mcp-in-your-ide/extend-copilot-chat-with-mcp>
