# Kubernetes Ecosystem Intelligence Watcher

GitHub starred 저장소의 공식 Release, GHSA 및 명시적으로 허용한 maintainer 공지를 수집하는 결정적 Python/SQLite watcher입니다. AI는 선택적 요약 계층이며 이벤트 식별·outbox·Slack 전송 권한을 갖지 않습니다.

## 실행 경계

- Python **3.12**, SQLite schema **2**. [의존성 관리](docs/DEPENDENCIES.md).
- 기본 `intelligence.mode: shadow`: 새 GHSA/공지의 상태·분석·정책을 평가하지만 Slack으로 보내지 않습니다. 기존 Release pending 누적과 특별 프로젝트 즉시 알림은 유지합니다.
- Preview는 SQLite의 메모리 복사본만 변경합니다. Slack, 실제 LLM, DB/cache save가 없습니다.
- Schedule은 기존 KST **08:00 / 14:00 / 17:00**입니다. commit mode는 기본 브랜치만 허용합니다.
- 본 기능 PR의 검증은 fixture와 feature-branch preview만 사용합니다. 병합/운영 rollout은 별도 승인 사항입니다.

## 구성

| 계층 | 구현/계약 |
| --- | --- |
| Registry | [`config/projects.yaml`](config/projects.yaml), 명시 정책 → owner/topic/name/description → ambiguous. alias는 하나의 canonical 정책으로 연결합니다. |
| Release | GitHub Release ID, 최근 bounded pagination + 저장된 overlap reconciliation. 매 실행 전체 이력 발견을 주장하지 않습니다. |
| GHSA | Global public API의 modified window + Link continuation, 별도 withdrawn scan. Repository Advisory는 선택적 capability-degraded source입니다. |
| Announcements | 프로젝트별 opt-in Discussions/labelled Issues/HTTPS RSS. trust·source provenance는 결정적입니다. |
| AI | strict JSON schema, 분리된 cache, 입력은 untrusted envelope. 기본 disabled이며 결정적 fallback을 사용합니다. |
| Routing | CRITICAL/HIGH 즉시, DIGEST 수량/시간/KST window, SUPPRESSED 사유 감사. AI는 security floor를 내리지 못합니다. |
| Delivery | 2xx 이후 ack, 429 Retry-After, 5xx/timeout retry, 부분 성공 event만 ack. exactly-once 보장은 없습니다. |
| Knowledge | read-only DB/feed JSONL; public만 기본 export, raw/AI/revision 문서 분리. |

## 설치와 검증

```bash
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install --require-hashes -r .github/scripts/requirements-dev.txt
python -m py_compile .github/scripts/check_release.py
python -m compileall -q .github/scripts starwatch scripts
python -m unittest discover -s tests -v
python scripts/check_contracts.py
python -m ruff check --select E4,E7,E9,F .github/scripts starwatch scripts
python -m pip_audit --disable-pip --no-deps -r .github/scripts/requirements.txt
git diff --check
```

정확한 security/format/lock CI 명령은 [의존성 런북](docs/DEPENDENCIES.md)과 `.github/workflows/ci.yml`을 따릅니다. Tests는 실 토큰·Slack·LLM 없이 실행합니다.

## 로컬 fixture preview

```bash
tmp_dir="$(mktemp -d)"
printf 'example/project\n' > "$tmp_dir/repos.txt"
printf '{"example/project":[{"id":1,"tag_name":"v1","published_at":"2026-10-01T00:00:00Z"}]}' > "$tmp_dir/releases.json"
python .github/scripts/check_release.py --mode preview \
  --repos-file "$tmp_dir/repos.txt" --fixture-releases "$tmp_dir/releases.json" \
  --cache-path "$tmp_dir/legacy.json" --state-db "$tmp_dir/events.sqlite3" \
  --feed-path "$tmp_dir/feed.json" --github-output "$tmp_dir/outputs.txt"
test ! -e "$tmp_dir/events.sqlite3"
```

실제 read-only preview는 이미 주입된 `GH_TOKEN`과 inventory를 사용합니다. feature branch에서는 `--send-slack`이나 commit workflow를 실행하지 않습니다. `--sleep-seconds`/`--no-sleep`은 문서화된 deprecated no-op입니다.

## 설정

`config.yaml`은 JSON-compatible YAML입니다. JSON 표기는 별도 YAML 설치가 없는 fixture 환경에서도 중첩 정책을 정확히 읽게 합니다.

- `intelligence`: shadow/canary/full, registry 경로, signal page/runtime 상한.
- `analysis.enabled: false`: 실제 endpoint/key는 환경변수 이름으로만 지정합니다.
- `routing.destination_visibility: private`: 공개 목적지는 `public`으로 명시하며 private/internal/unknown은 차단됩니다.
- `artifacts.*.enabled: false`: 기본 Actions는 inventory/feed/DB를 업로드하지 않습니다.
- `special_projects`: 한 호환 기간 유지. registry와 충돌하면 오류이며 특별 Release 즉시 알림을 조용히 제거하지 않습니다.
- `notification.cutover_pending_policy: suppress_existing`: 최초 legacy cutover의 관측 backlog는 저장하되 SUPPRESSED. 이후 신규 Release부터 정상 pending입니다.

초기 registry 10개는 명시 정책입니다. 현재 inventory 확인에서는 8개가 starred이고 `grafana/grafana`, `prometheus-operator/kube-prometheus`는 unstarred지만 명시 추적 정책으로 유지합니다. `aws/karpenter`는 `aws/karpenter-provider-aws`로 이동한 **별도** 프로젝트이며 `kubernetes-sigs/karpenter`의 alias가 아닙니다.

## Feed 및 Knowledge 계약

`github-stars-release-feed/v1`을 additive 확장합니다.

- `releases[] == new_releases[]`: 이번 invocation에서 처음 발견한 **Release**만.
- `pending_releases[]`: 전송 전 eligible Release pending.
- `new_events[]` / `pending_events[]`: 새 source를 포함한 이벤트.
- `notification_batch[]`: 생성된 모든 `slack_chunks[].event_ids`의 정확한 순서 union. 현재 알림 분석의 LLM 입력입니다.
- `pending_count`: 지연 retry를 포함한 전송 후 pending.
- `intelligence`: 분류/신호/API오류/AI/cache/route/visibility/deferred 수치만.

로컬 feed는 private raw 정보를 포함할 수 있으므로 공개 artifact로 간주하지 않습니다. secret-like 값은 출력 경계에서 구조적으로 지웁니다. 공개 export는 별도 visibility 검증을 거칩니다.

```bash
python scripts/export_knowledge_jsonl.py --state-db .cache/events.sqlite3 --output /tmp/intelligence.jsonl
# 이전 discovery feed 소비자도 호환:
python scripts/export_knowledge_jsonl.py --feed .cache/release-feed.json --output /tmp/discoveries.jsonl
```

## 운영 문서

- [아키텍처](docs/KUBERNETES_INTELLIGENCE_ARCHITECTURE.md) / [로드맵](docs/KUBERNETES_INTELLIGENCE_ROADMAP.md) / [AI 세션 컨텍스트](docs/AI_PROJECT_CONTEXT.md)
- [P0 상태·cutover](docs/P0_RUNBOOK.md) / [schema·migration](docs/EVENT_SCHEMA_REFERENCE.md)
- [Registry](docs/PROJECT_REGISTRY.md) / [GHSA API](docs/SECURITY_ADVISORY_RUNBOOK.md)
- [로컬 LLM](docs/LOCAL_LLM_RUNBOOK.md) / [Slack routing](docs/SLACK_ROUTING_POLICY.md)
- [신뢰 공지](docs/MAINTAINER_ANNOUNCEMENT_SOURCES.md) / [Knowledge](docs/KNOWLEDGE_EXPORT.md)
- [보안](docs/SECURITY_LAYERING_NOTES.md) / [Rollout 및 rollback](docs/P1_P2_ROLLOUT_RUNBOOK.md)

## 잔여 위험

Actions cache는 영구 DB가 아닙니다. Slack 성공과 DB/cache 저장 사이 crash는 중복을 만들 수 있습니다. Reconciliation은 지속적인 정상 commit과 metadata 보존을 전제로 하며 최대 역사 발견 지연을 보장하지 않습니다. v2 DB는 P0 코드가 직접 열 수 없으므로 rollback은 전송 중단·백업 복구·별도 cache namespace 절차가 필요합니다. 실제 LLM 품질 및 운영 canary 전달은 이 PR의 안전 검증 범위 밖입니다.
