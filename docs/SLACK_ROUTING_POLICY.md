# Slack routing policy (P1)

`starwatch.routing` is a pure policy/formatting layer. The caller persists the returned audit decision, selects the current outbox cohort, gates rollout, sends chunks through the existing notifier, and acknowledges only chunk event IDs after Slack 2xx. This module never calls Slack, modifies SQLite, or changes source trust.

## Decision inputs and audit

`decide(event, project, config, analysis=None)` returns `policy_version`, `deterministic_reasons`, `deterministic_floor`, `ai_suggested_priority`, `effective_priority`, `route`, and `suppression_reason`. Route values are `CRITICAL`, `HIGH`, `DIGEST`, and `SUPPRESSED`. The default policy version is `v1`.

| Raw signal | Minimum route |
| --- | --- |
| Critical GHSA | CRITICAL |
| High GHSA | HIGH |
| Medium GHSA on a critical-tier project | HIGH |
| Other advisory or regular Release | DIGEST |
| Explicit project `routing.release_floor` / `advisory_floor` | Configured minimum |
| Legacy `is_special` Release without explicit floor | HIGH (immediate) |
| Trusted source text containing a high-risk security phrase | CRITICAL |
| Trusted breaking/removal/migration phrase | HIGH |

Security/breaking keyword promotion requires deterministic `source_trust >= 85`; official Releases and advisories default to trust 100 when no explicit trust field exists. This is intentionally a conservative word/phrase rule, not an LLM inference. `release_floor` does not automatically raise an informational announcement; its own content and trust are evaluated separately. The LLM can promote `effective_priority` only; it cannot lower the floor, alter trust, or override suppression. Invalid/absent AI impact contributes no promotion.

Suppression is explicit and takes precedence over all promotions: ignored/disabled project, draft, disabled signal, disabled prerelease, unmapped/ambiguous or withdrawn advisory, announcement trust below 85, or non-public/unknown visibility for a public destination. Discussion, Issue, and RSS events require both `signals.announcement: true` and their respective `signals.discussions/issues/rss: true`; missing source-specific opt-in suppresses. The decision retains the deterministic floor and suppression reason for audit. Unknown visibility is not public. The default routing destination is private; production configuration must explicitly set the real destination visibility. The caller is responsible for not including a suppressed event in a Slack batch and for retaining its event/reason in state.

## Digest eligibility

`select(pending_events, config, now)` returns eligible `PENDING_NOTIFICATION` or `DELIVERY_FAILED` rows without mutating them. CRITICAL/HIGH are immediate. DIGEST is due when *any* condition holds:

- eligible digest count is at least `routing.digest.min_count` (default 5);
- the oldest event is at least `routing.digest.max_age_hours` old (default 24 hours);
- the current KST hour is in `routing.digest.send_hours_kst` (default `[17]`); or
- any digest row has `DELIVERY_FAILED`, which bypasses the count/window for retry.

The clock must be timezone-aware. SQLite timestamps remain UTC; KST is used only for window evaluation. A below-threshold DIGEST stays pending; `select` never deletes it. A caller should pass outbox rows currently eligible by `next_attempt_at`, so Slack Retry-After/backoff remains authoritative.

Suggested configuration:

```yaml
routing:
  policy_version: v1
  destination_visibility: private
  allow_ai_promotion: true
  critical:
    immediate: true
  high:
    immediate: true
    max_batch_size: 10
  digest:
    min_count: 5
    max_age_hours: 24
    send_hours_kst: [17]
    max_batch_size: 50
```

## Message and delivery boundary

`build_routed_chunks(selected, max_length, config)` generates `SlackChunk` objects with the exact selected event IDs, one ID per line, separated by route in CRITICAL → HIGH → DIGEST order and constrained by `max_length` and route batch sizes. Within each route, input order is stable. `notification_batch[]` should use the flattened chunk ID order so its ordered mapping matches the generated Slack chunks. CRITICAL uses one event per chunk. HIGH/DIGEST cap at 10/50 events by default. The formatter uses source title, project, official HTTPS URL, deterministic reasons, optional Korean summary and one recommended *review* action; it does not execute actions. GHSA/CVE/affected/patched text is shown where present. All untrusted text is escaped to prevent `<@USER>`, `<!channel>`, `<!here>`, and fake Slack-link mention injection. Links containing markup/control characters, user info, or query strings are rendered as plain `official source` text rather than linked. Long lines may be truncated; full raw facts remain in the event store.

The existing notifier owns 2xx acknowledgement, 429 `Retry-After`, 5xx/timeout retry, and partial-chunk success. If Slack accepts a message but state persistence fails, a later retry may duplicate it: delivery is **at least once**, not exactly once. Rollout gating remains a caller responsibility: shadow mode must never send new GHSA/announcement routes to production Slack, and feature-branch preview must never invoke transport.
