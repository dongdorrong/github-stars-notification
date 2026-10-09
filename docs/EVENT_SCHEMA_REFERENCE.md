# Event store schema v2

SQLite is the authoritative local state for event identity and notification acknowledgement. The legacy `.cache/releases.json` is a read-only cutover hint, not a migration target. Every connection validates `PRAGMA quick_check`, schema version, required columns, primary/foreign keys, and foreign-key integrity before use. A malformed or unknown-version file fails closed; it is never replaced with an empty database.

## Tables and ownership

| Table | Key | Purpose |
| --- | --- | --- |
| `events` | `event_id` | Current normalized **raw** source fact, source ID, project repository, published/first/last seen times, content hash and JSON payload. AI output is not stored here. |
| `event_revisions` | `(event_id, content_hash)` | Immutable observed raw payload for each distinct content hash. Existing v1 rows are added on the first v2 re-observation; migration does not invent older revisions. |
| `notification_outbox` | `event_id` | Priority, pending/in-progress/delivered/failed/suppressed state, retry and lease fields, plus nullable v2 `suppression_reason`. Original v1 columns and values remain unchanged. |
| `ai_analyses` | `(event_id, event_content_hash, schema_version, prompt_version, provider, model)` | Validated AI/fallback analysis JSON and creation time, cached separately from raw facts. Both the analyzer and store enforce the v1 schema before a successful insert. |
| `routing_decisions` | `decision_id` | Deterministic/AI advisory route decision JSON, policy version and contemporaneous event hash. A byte-equivalent latest decision on the same hash is not duplicated; changes append audit. |
| `delivery_attempts` | `(event_id, attempt_number)` | Route and event hash actually claimed, start/completion time, delivered/failed/lease-expired result, safe error category and retry time. Pre-v2 attempts are not fabricated. |
| `state_metadata` | `key` | Existing cursor, cutover and other checkpoint values. |

P0 release identity remains `github:release:<numeric release ID>`. GHSA identity is `github:ghsa:<GHSA-ID>`. Announcement collectors use their own stable IDs; the store does not derive identity from title or LLM text. `upsert_raw` requires `event_id`, `event_type`, `repository`, `source_id`, `published_at`, and `content_hash`; `repository` may be the empty string for an unmapped Global Advisory, which must be suppressed by caller policy. The store does not decide source trust or priority. Release objects continue through `upsert` and its original outbox semantics.

`upsert_raw` inserts a first observation as pending or suppressed according to caller policy. A repeated hash does not reopen an acknowledgement. A changed hash retains the previous revision and requeues a delivered/suppressed non-release event unless caller policy suppresses the update. If a revision arrives while a previous version is in flight, that lease remains valid; acknowledgement of the old content then queues or suppresses the latest version. Source policy must pass a suppression reason for auditable suppressed events. New collectors and routing remain inactive unless their configured rollout mode enables them.

## v1 → v2 migration and preview

On commit-mode open, the store validates the existing v1 file before opening it writable. DDL, nullable outbox column addition and `PRAGMA user_version=2` run in one `BEGIN IMMEDIATE` transaction, followed by v2 validation before commit. Any DDL/validation error rolls back to v1 without replacing event/outbox rows. Opening v2 again is idempotent. Preview opens the source read-only, validates it, copies it into memory, and performs the same migration only on the memory copy. Preview leaves the source file bytes and version unchanged.

Before rollout, retain a separately verified v1 database backup and the legacy cache. **P0 code does not understand v2** and cannot be used directly against an upgraded file. Rollback therefore means stopping commit runs and restoring the pre-upgrade v1 backup, or using a reviewed data export/downgrade procedure; blindly changing `user_version` is unsafe. A backup restore loses v2-only revisions, analyses, routing and delivery-attempt audit, and may replay any acknowledgement newer than the backup. Reconcile Slack delivery history before resuming. This store and GitHub Actions cache do not provide exactly-once delivery.
