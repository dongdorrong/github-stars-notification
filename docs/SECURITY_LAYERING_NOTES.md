# Security layering notes — Kubernetes Intelligence

> 적용 범위: P0 Release는 운영 중, P1/P2 코드는 feature branch에서 검증 중이다. 최종 HEAD PR CI·원격 preview, 운영 shadow/canary/full 증거가 나오기 전까지 배포 완료나 live integration 성공을 주장하지 않는다.

## 1. GitHub Actions와 API 경계

- Notifier workflow는 `contents: read`, concurrency, 기본 브랜치 commit gate, 30분 timeout, Python 3.12, reviewed commit SHA의 checkout/setup-python/cache action을 사용한다. PR CI는 별도 workflow이고 production Secret 없이 수행한다. 외부 Release/Advisory/Issue/Discussion/RSS text를 `${{ ... }}`로 inline shell source에 삽입하지 않는다.
- Feature branch에서는 `workflow_dispatch(mode=preview)`만 허용한다. Preview에는 Slack secret이 주입되지 않고 DB cache save가 skip돼야 하며 실 LLM을 호출하지 않는다. 이 step 결과는 **원격 run에서 확인해야 할 불변식**이다.
- GitHub adapter는 REST API version `2022-11-28`, read-only GET/고정 GraphQL query, 유한 page/응답 크기/시간 budget을 사용한다. Pagination Link는 `https://api.github.com`의 예상 endpoint만 수용한다. Global GHSA는 공개 원천, Repository GHSA는 기본 비활성·권한 거부 시 해당 source만 degraded이다. 오류 response body/header/token/private URL은 로그에 넣지 않는다. REST 403은 권한 부족 또는 rate limit일 수 있어 안전 범주로 분류한다.
- Slack Incoming Webhook은 코드의 POST 경계만 허용한다. 2xx가 확인된 chunk의 event ID만 ack하고 429 `Retry-After`, 5xx/timeout, 부분 성공은 재시도 상태로 남긴다. Slack 성공 후 DB/cache save 실패에는 중복 가능성이 있어 exactly-once를 주장하지 않는다. [Slack 공식 rate-limit 문서](https://docs.slack.dev/apis/web-api/rate-limits/)는 webhook도 429+Retry-After 대상임을 명시한다.

## 2. Secret·artifact·visibility

- `GH_PAT`, `SLACK_WEBHOOK_URL`, `LLM_BASE_URL`, `LLM_API_KEY`의 **값**은 환경/secret store에만 존재해야 한다. Source, fixture, 문서, feed, outputs, summary, 로그, artifact, PR에 복사하지 않는다. `starwatch.security.redact`는 Authorization/env/query/webhook/token 형식 및 중첩 JSON secret key 값을 구조적으로 가린다. Redaction은 마지막 방어선이며 가능한 처음부터 민감 필드를 출력 구조에 넣지 않는다.
- Inventory/feed/Knowledge artifact upload는 기본 **off**다. Actions 공개 로그에는 repository 실명 대신 ordinal/keyed short reference와 집계만 쓴다. Private/internal/unknown repository raw name/URL을 공개 artifact에 넣지 않는다.
- Knowledge JSONL은 읽기 전용 CLI가 public-only 기본으로 내보낸다. Private/internal은 명시적 private destination+opt-in, unknown은 제외한다. Repository-origin DB event는 최신 `repository_visibility:<repo>` metadata가 과거 payload의 public 주장보다 우선한다. Global GHSA가 public source여도 mapped project의 최신 visibility가 private/internal/unknown이면 public export와 delivery에서 제외한다. [Knowledge export](KNOWLEDGE_EXPORT.md) 참조.
- `.cache/events.sqlite3`와 `.cache/releases.json`을 artifact로 올리지 않는다. Legacy cache는 read-only다. Malformed DB/cache는 빈 상태로 대체하지 않는다. Actions cache eviction/stale restore는 잔여 위험이다.

## 3. Untrusted content, AI, announcement

- Release notes, GHSA description, Issue/Discussion body, RSS XML은 모두 untrusted data다. LLM prompt의 고정 system policy와 serialized `untrusted_event_json` envelope을 분리하고 input/output 길이, JSON schema, enum, unknown fields, confidence를 검증한다. No tools, shell, browsing, GitHub write or Slack write are exposed to AI. 모델은 identity/duplicate/source trust/outbox/priority floor를 변경할 수 없다.
- `analysis.enabled: false`가 기본이고 preview는 설정과 무관하게 fallback-only다. Optional OpenAI-compatible endpoint/key는 env 이름으로만 설정하며 401/403 오류 본문을 읽지 않고 429/5xx/timeout은 bounded retry 후 결정적 fallback으로 간다. Socket idle timeout뿐 아니라 전체 HTTP response hard deadline을 적용한다. 실제 유료/로컬 endpoint 검증은 이번 작업에서 하지 않는다.
- Announcements는 registry에서 project/source/category/label/feed를 **명시**한 경우만 수집한다. General contributor unlabeled Issue는 Slack 후보가 아니다. RSS는 HTTPS allowlist, DNS/IP 검사, redirect allowlist, 1MB 크기, content-type, DTD/entity 금지 XML parser를 적용한다. AI는 trust를 높일 수 없다.
- Slack formatter는 `<@USER>`, `<!channel>`, `<!here>`, `<http://...|...>` 등 외부 source text의 mention/link syntax를 escape한다. Formatter에 unbounded body를 싣지 않는다.

## 4. Supply chain·state rollback

- Python 3.12를 workflow/.python-version과 맞추고 runtime/development dependencies를 hash lock으로 설치한다. PyGithub `_rawData` 사용은 내부 API라 업그레이드 때 page당 요청 수 회귀가 필수다. 별도 PR CI는 compile/test/schema/registry/lint/audit/lock/no-write 검증을 production secret 없이 수행해야 한다. 각 gate의 실제 원격 성공은 PR check에서만 확정한다.
- Schema v1→v2 migration은 검증·transaction으로 진행하며 event/outbox/ack을 보존한다. P0 코드는 v2 DB를 열 수 없으므로 code revert만으로 rollback하지 않는다. 사전 v1 백업과 migration 이후의 delivery/revision audit을 대조한다. v2를 빈 DB 또는 낡은 legacy cache로 조용히 대체하지 않는다.
- 신규 signal Slack은 `intelligence.mode: shadow` 기본으로 억제한다. Canary/full은 운영자 승인과 별도 안전 검증이 필요하다. [P1/P2 rollout runbook](P1_P2_ROLLOUT_RUNBOOK.md)에 단계별 중지·복구 조건을 둔다.

## 검증 및 미해결 리스크

Token-free fixture는 secret fixture가 feed/log/output/export에 남지 않는지, preview byte invariance, RSS SSRF/redirect/oversize/XML, malformed state, Slack ack/retry, AI injection/fallback을 검증해야 한다. 최종 HEAD에서 전체 suite와 PR CI가 녹색이어야 한다. Live optional source 권한/비공개 네트워크 LLM 품질, 실제 운영 Slack ack, private destination 접근 제어, GitHub API 순서·rate-limit 변화는 fixture만으로 해소되지 않는 운영 검증 gap이다.
