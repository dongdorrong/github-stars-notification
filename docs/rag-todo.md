# Knowledge integration status — github-stars-notification

The implemented contract is [KNOWLEDGE_EXPORT.md](KNOWLEDGE_EXPORT.md). This replaces the original tag-name document-ID proposal: GitHub Release IDs, not mutable tags, are authoritative.

## Implemented

- Read-only `scripts/export_knowledge_jsonl.py` accepts the compatible release feed or SQLite v1/v2 state; it performs no network, Slack, LLM, cursor or outbox writes.
- Existing fordongdorrong KnowledgeDocument envelope is preserved. Raw event, AI analysis and revision documents have separate stable linked IDs.
- Public-only is default. Private/internal require explicit opt-in and a private destination; unknown is excluded. Current project visibility also gates mapped GHSA export.
- Release body, advisory affected/patched versions, provenance and model/prompt metadata are retained in the appropriate document type.
- Deterministic ordering, structured value redaction, schema validation and repeated-export/import idempotency are fixture-tested.
- This repository does not write to a vector database or operate central embeddings/search.

## Remaining operator integration

- Configure the central fordongdorrong importer and its private destination access policy separately.
- Validate authorized end-to-end import/search in that system; local export fixtures do not prove deployment there.
- Never upload event DB, raw inventory or unsanitized feed as public Actions artifacts.
