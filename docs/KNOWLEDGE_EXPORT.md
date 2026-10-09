# Visibility-safe Knowledge JSONL export

`scripts/export_knowledge_jsonl.py`는 기존 fordongdorrong `KnowledgeDocument` envelope을 유지하면서 event/analysis/revision을 별도 문서로 내보내는 **읽기 전용 로컬 명령**이다. GitHub, Slack, LLM을 호출하지 않고 SQLite event/outbox/cursor를 수정하지 않는다. 공개 업로드는 별도 정책 검토 전까지 기본 비활성이다.

```bash
# 공개 가능 event만; feed v1의 releases[]는 이번 실행에서 처음 발견한 항목만 뜻한다.
python3 scripts/export_knowledge_jsonl.py --feed .cache/release-feed.json --output knowledge-public.jsonl

# SQLite v1/v2의 raw event, v2 AI 분석, revision을 결정적으로 내보낸다.
python3 scripts/export_knowledge_jsonl.py --db .cache/events.sqlite3 --output knowledge-public.jsonl

# private/internal은 명시적인 private destination과 파일 출력이 함께 필요하다.
python3 scripts/export_knowledge_jsonl.py --db .cache/events.sqlite3 \
  --destination private --include-private --output /private/destination/knowledge.jsonl
```

기본값은 `visibility: public`으로 명시된 event만 내보낸다. DB의 Release/Issue/Discussion/RSS event는 `state_metadata.repository_visibility:<lowercase-repo>`가 있으면 과거 payload보다 **현재 inventory visibility**가 우선한다. 따라서 공개→비공개 전환된 저장소의 현재 raw, 과거 revision, AI 문서를 모두 공개 출력에서 제외한다. Global GHSA는 공개 원천이므로 이 override 대상이 아니다. `private`, `internal`, `unknown`은 공개 출력에서 제외하며, `unknown`은 private opt-in에서도 제외한다. `--include-private`만 주거나 stdout으로 private 내용을 출력하려는 CLI 호출은 실패한다. 출력 경로가 입력 feed/DB 또는 legacy cache와 같으면 실패한다. Private destination의 실제 접근 제어·암호화는 운영자가 확인해야 한다.

## 문서 계약

JSONL은 UTF-8, `sort_keys`와 안정적인 `document_id` 순서로 생성한다. 같은 정지 상태 DB/Feed를 반복 내보내면 동일한 논리 문서가 나오며 수입 측은 `document_id` upsert로 중복을 피한다.

| 종류 | document_id | 주요 내용 |
| --- | --- | --- |
| Raw event | `event:<event_id>` | 공식 title/body, project, visibility, 원본 content hash, provenance, Release tag/body 또는 GHSA affected/patched metadata |
| AI/fallback 분석 | `analysis:<event_id>:<content_hash>:<prompt_version>:<provider>:<model>` | 별도 분석 요약·schema/model/prompt metadata, parent link |
| Revision | `revision:<event_id>:<content_hash>` | 이전/현재 관측 원문 및 provenance, event parent link |

공통 envelope `source_id`, `document_id`, `title`, `body`, `uri`, `content_hash`, `created_at`, `updated_at`, `visibility`, `lifecycle`, `deleted_at`, `indexable`, `metadata`에 `schema_version: github-stars-knowledge/v2`, `event_id`, `document_type`, `project`, `categories`, `related_document_ids`를 더한다. 보안 권고의 `vulnerabilities`에는 API가 제공한 affected range와 first patched version을 보존한다. 데이터가 없으면 지어내지 않는다. Withdrawn advisory는 `lifecycle: withdrawn`으로 감사 가능하게 남긴다. P0의 ID 없는 구형 feed fixture는 한 호환 기간 동안 `releases/<repo>/<tag>` ID를 유지한다. 운영 DB는 Release ID 기반 `event:<event_id>`를 사용한다.

Feed v1의 `new_releases[]`와 `releases[]`가 동시에 있으면 반드시 동일해야 한다. Feed 형식·DB schema·payload JSON·identity/content hash가 깨지면 빈 export로 조용히 대체하지 않고 실패한다. SQLite는 `mode=ro&immutable=1`로 정지 상태 snapshot을 읽는다. 실행 중 WAL에 기록된 미체크포인트 변경을 읽는 용도로는 사용하지 않는다. 운영 export 전 state save가 완료된 DB snapshot을 사용한다.

## 보안 및 롤백

공개 artifact는 기본 생성/업로드되지 않는다. 이 명령의 `--output`은 로컬 파일 생성일 뿐 Actions artifact upload를 의미하지 않는다. 출력에 들어가는 event와 metadata는 공통 구조적 redactor를 통과한다. 그래도 원본 Release/Advisory 본문은 신뢰하지 않는 사용자 데이터이므로 공개 전에 visibility와 내용 정책을 추가 검토한다. 롤백은 생성된 JSONL 파일과 downstream 인덱스만 별도 폐기하면 되며 SQLite event/outbox에는 복구 작업이 필요하지 않다.

```bash
python3 -m unittest tests.test_knowledge_export tests.test_knowledge_v2 -v
```

## Sanitized local artifacts

`config.yaml`의 `artifacts.inventory/release_feed/knowledge_export.enabled`를 명시적으로 켜면 collector CLI가 feed 옆 `public-artifacts/`에 승인된 공개 필드와 manifest를 생성합니다. `public_only=false`는 거부됩니다. 같은 파일을 다른 내용으로 덮어쓰지 않으므로 별도 실행 디렉터리를 사용합니다. Actions는 이 폴더도 자동 업로드하지 않습니다. 외부 업로드가 필요하면 manifest를 검토한 뒤 명시적 retention(권장3일)의 비밀 없는 별도 게시 절차를 사용합니다. DB 업로드는 금지합니다.

Public GHSA 원천이라고 비공개 project mapping을 공개하지 않습니다. 매핑된 프로젝트의 현재 visibility가 public임을 확인할 수 없으면 raw/AI/revision cohort 전체를 공개 export에서 제외합니다. 미매핑 public GHSA는 raw 감사용 export가 가능합니다.
