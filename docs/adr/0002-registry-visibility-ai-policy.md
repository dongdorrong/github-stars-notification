# ADR-0002: Registry authority, visibility and AI priority boundary

- Status: Proposed in feature branch (operator rollout not performed)
- Date: 2026-10-09
- Related: Epic #3, issues #7, #9, #10, #12, [ADR-0001](0001-deterministic-event-state-and-ai-boundary.md)

## Context

P0's `special_projects` and Release-only outbox cannot express official project ownership, GHSA mapping, opt-in maintainer sources, or destination visibility. Model-generated summaries must not become authority over event identity, source trust, security floor or delivery state. Historical public payloads can also become unsafe if a repository later becomes private.

## Decision

1. `config/projects.yaml` plus `schemas/project-registry.schema.json` own explicit, versioned project policy. Canonical lowercase `owner/repo` plus aliases map renames to one row. The explicit registry wins over heuristics and AI. Legacy `special_projects` are read for one compatibility period; a conflicting release floor fails validation rather than silently dropping immediate delivery.
2. Classification is deterministic: explicit policy/ignore, known owner, exact topic/name/description evidence, then `AMBIGUOUS`. Only ambiguity may receive optional AI advice. AI cannot enable collectors or change tier.
3. Inventory visibility is `public|private|internal|unknown`. Missing/invalid values are `unknown`. Public Knowledge export requires explicit public visibility; private/internal require both explicit opt-in and a private destination. Repository-origin DB export consults the newest `repository_visibility:<repo>` metadata, so old public event/revision/analysis payloads do not outrank a current private inventory. Global GHSA remains a public source, but mapped project visibility must also be public for public export or delivery; private/internal/unknown mappings fail closed.
4. Raw event facts and revisions, `ai_analyses`, routing decisions, and outbox/delivery audit are physically separate SQLite v2 structures. Valid AI JSON is advisory only. Code supplies event identity/hash/model/provenance, validates schema, and caches by event/hash/schema/prompt/provider/model. Deterministic fallback is separately keyed and cannot masquerade as a successful model response.
5. Routing computes a deterministic floor before AI. Valid AI may promote but never demote a Critical/High security floor, raise source trust, override suppression, change duplicates/outbox, or call Slack. `SUPPRESSED` remains suppressed even with a high model suggestion. `intelligence.mode: shadow` is the merge default; canary/full need explicit operator decisions. P0 Release delivery remains active in shadow.
6. Source text is untrusted. Fixed system policy and serialized event envelope are separate; no tools or write API are available to the model. Slack formatter escapes mention syntax; output redaction protects logs/feed/export but is not a substitute for source visibility gating.

## Consequences

- Fewer accidental alert floods or privacy leaks, with deterministic fixture tests and auditable reason codes.
- Registry maintenance, inventory visibility freshness, schema migration and source capability checks are operational obligations. Unknown visibility excludes public export rather than guessing.
- v1→v2 SQLite migration is one-way for P0 code: v1 code cannot open v2. A pre-migration v1 backup and post-migration ack/revision comparison are needed for rollback; do not reset to an empty DB.
- The model may be absent or wrong; fallback preserves official-source pipeline behavior. Real LLM/Slack production behavior still requires later authorized rollout evidence.
- GitHub Actions cache and Slack acknowledgement remain at-least-once, not exactly-once.

## Alternatives rejected

- Derive tiers/trust from LLM: non-deterministic and unsafe for security routing.
- Default public visibility when inventory is missing: can leak private/internal repositories.
- Auto-enable all Discussions/Issues/RSS: high-noise, untrusted content and API cost.
- Store model output only inside raw `payload_json`: loses provenance/identity boundary and cannot cleanly cache/revise analysis.
- Automatically downgrade DB to v1 on code rollback: would discard v2 history and risk duplicate Slack delivery.
