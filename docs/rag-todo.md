# fordongdorrong RAG / Knowledge Store TODO — github-stars-notification

- 작성일: 2026-06-27
- 중앙 주도: `/home/dongdorrong/github/private/fordongdorrong`
- 이 repo 역할: GitHub starred repository release/event feed를 제공하는 **read-only release source connector**
- 현재 feed/cache 후보: `.cache/release-feed.json`
- 현재 주요 스크립트: `.github/scripts/check_release.py`

## 1. 목표

GitHub starred repository의 release 이벤트와 중요 업데이트를 중앙 Knowledge Store에 축적해 기술블로그 소재, 사이드프로젝트 dependency update, 운영 리스크 탐지에 활용한다. Slack 알림과 RAG 인덱싱은 분리하고, 중앙 `fordongdorrong`이 임베딩/검색/요약을 담당한다.

## 2. 경계

- [ ] 이 repo에서 벡터 DB에 직접 쓰지 않는다.
- [ ] indexing export 중 Slack 알림을 보내지 않는다.
- [ ] GitHub token, Slack webhook, private repository 접근 정보는 export payload에 포함하지 않는다.
- [ ] raw release event와 LLM 요약은 분리한다.
- [ ] release feed cache는 source event snapshot이고, 중앙 Knowledge Store의 source of truth가 아니다.

## 3. 문서 식별 규칙 초안

```text
source_id: github-stars
document_id: github-stars:release:<owner>/<repo>:<tag_name>
document_id: github-stars:repo:<owner>/<repo>
uri: https://github.com/<owner>/<repo>/releases/tag/<tag_name>
visibility: public | internal
```

release 문서 예시:

```json
{
  "source_id": "github-stars",
  "document_id": "github-stars:release:owner/repo:v1.2.3",
  "title": "owner/repo v1.2.3",
  "uri": "https://github.com/owner/repo/releases/tag/v1.2.3",
  "visibility": "public",
  "content_hash": "sha256:...",
  "body": "release name + release body + notification reason",
  "updated_at": "2026-06-27T00:00:00+09:00",
  "metadata": {
    "owner": "owner",
    "repo": "repo",
    "tag_name": "v1.2.3",
    "published_at": "...",
    "html_url": "...",
    "is_special": false,
    "notify_reason": "new_release"
  }
}
```

## 4. Export 계약 TODO

### P0 — feed 구조 고정

- [ ] `.cache/release-feed.json`의 필드를 문서화한다.
- [ ] release event에 필요한 최소 필드를 확정한다.
  - [ ] owner/repo
  - [ ] tag_name
  - [ ] release_name
  - [ ] release_body
  - [ ] html_url
  - [ ] published_at
  - [ ] fetched_at
  - [ ] is_special
  - [ ] notify_reason
- [ ] private starred repo가 섞일 수 있는 경우 visibility 기본값과 export 제외 정책을 정한다.

### P1 — read-only export 추가

- [x] JSONL export 명령을 추가한다. — `scripts/export_knowledge_jsonl.py`
  - [ ] `python .github/scripts/check_release.py export-knowledge --format jsonl`
  - [x] 또는 별도 `scripts/export_knowledge_jsonl.py`
- [x] export는 기존 cache/feed를 읽기만 하고 GitHub API 호출 여부를 옵션으로 분리한다.
  - [x] `--from-cache` — 기본 동작이 cache/feed only
  - [ ] `--refresh`는 명시 호출 시에만
- [x] export 필드:
  - [x] `source_id`
  - [x] `document_id`
  - [x] `title`
  - [x] `body`
  - [x] `content_hash`
  - [x] `uri`
  - [x] `visibility`
  - [x] `created_at`
  - [x] `updated_at`
  - [x] `metadata.owner`
  - [x] `metadata.repo`
  - [x] `metadata.tag_name`
  - [x] `metadata.published_at`
  - [x] `metadata.is_special`
  - [x] `metadata.notify_reason`
- [x] Slack formatting과 Knowledge export formatting을 분리한다. — Slack path는 `.github/scripts/check_release.py`, Knowledge path는 `scripts/export_knowledge_jsonl.py`

### P2 — 품질/요약 확장

- [ ] release body가 너무 길면 중앙 chunker에 맡기고, 이 repo에서는 원문 body를 보존한다.
- [ ] LLM 요약이 필요하면 별도 metadata/document로 저장한다.
- [ ] breaking change/security/dependency 관련 태그를 중앙에서 재분류할 수 있게 raw signals를 남긴다.
- [ ] release가 draft/prerelease인지 metadata에 포함한다.

### P3 — 중앙 활용 흐름

- [ ] `fordongdorrong`에서 최근 release 이벤트를 검색해 `thing` 리서치 후보로 전달한다.
- [ ] `velog` 초안 작성 시 관련 release/event를 추천한다.
- [ ] 사이드프로젝트 dependency watchlist와 연결해 영향 분석 문서를 생성한다.
- [ ] Slack 알림 실패와 Knowledge export 실패를 별도 상태로 추적한다.

## 5. 테스트 TODO

- [x] fixture release feed로 JSONL export snapshot 테스트를 만든다. — `tests/test_knowledge_export.py`
- [x] tag/repo 기반 document_id 안정성 테스트를 만든다.
- [ ] Slack webhook/GitHub token이 payload/log에 포함되지 않는지 테스트한다.
- [ ] `--from-cache` 경로가 외부 API를 호출하지 않는지 테스트한다.
- [ ] special release 판정과 export metadata가 일치하는지 테스트한다.

## 6. 중앙 연동 수용 기준

- [x] `fordongdorrong knowledge validate-export github-stars.jsonl`이 통과한다. — 중앙 validator smoke 완료
- [ ] 같은 release를 반복 import해도 중복 문서가 생기지 않는다.
- [ ] 검색 결과 provenance에 `owner/repo`, `tag_name`, `html_url`, `published_at`이 포함된다.
- [ ] export 실행만으로 Slack 알림이 발송되지 않는다.

## 7. 현재 유용한 검증 명령

```bash
python3 -m py_compile .github/scripts/check_release.py
python3 -m unittest discover -s tests -v
```
