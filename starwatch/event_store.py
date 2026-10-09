"""SQLite source events and notification outbox. No network operations live here."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA_VERSION = 2
SCHEMA_V1 = """
CREATE TABLE events (
 event_id TEXT PRIMARY KEY, event_type TEXT NOT NULL, repository TEXT NOT NULL,
 source_id TEXT NOT NULL, published_at TEXT NOT NULL, first_seen_at TEXT NOT NULL,
 last_seen_at TEXT NOT NULL, content_hash TEXT NOT NULL, payload_json TEXT NOT NULL
);
CREATE TABLE notification_outbox (
 event_id TEXT PRIMARY KEY REFERENCES events(event_id), priority TEXT NOT NULL,
 notification_state TEXT NOT NULL, notify_attempts INTEGER NOT NULL DEFAULT 0,
 first_queued_at TEXT NOT NULL, next_attempt_at TEXT, notified_at TEXT,
 last_error TEXT, lease_until TEXT
);
CREATE TABLE state_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
"""
SCHEMA_V2 = """
ALTER TABLE notification_outbox ADD COLUMN suppression_reason TEXT;
CREATE TABLE event_revisions (
 event_id TEXT NOT NULL REFERENCES events(event_id), content_hash TEXT NOT NULL,
 payload_json TEXT NOT NULL, observed_at TEXT NOT NULL,
 PRIMARY KEY (event_id, content_hash)
);
CREATE TABLE ai_analyses (
 event_id TEXT NOT NULL REFERENCES events(event_id), event_content_hash TEXT NOT NULL,
 schema_version TEXT NOT NULL, prompt_version TEXT NOT NULL,
 provider TEXT NOT NULL, model TEXT NOT NULL, analysis_json TEXT NOT NULL,
 created_at TEXT NOT NULL,
 PRIMARY KEY (event_id, event_content_hash, schema_version, prompt_version, provider, model)
);
CREATE TABLE routing_decisions (
 decision_id INTEGER PRIMARY KEY, event_id TEXT NOT NULL REFERENCES events(event_id),
 event_content_hash TEXT NOT NULL, policy_version TEXT NOT NULL,
 decision_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX routing_decisions_event_latest ON routing_decisions(event_id, decision_id DESC);
CREATE TABLE delivery_attempts (
 event_id TEXT NOT NULL REFERENCES events(event_id), attempt_number INTEGER NOT NULL,
 event_content_hash TEXT NOT NULL, route TEXT NOT NULL,
 result TEXT NOT NULL, error_category TEXT, started_at TEXT NOT NULL,
 completed_at TEXT, next_attempt_at TEXT,
 PRIMARY KEY (event_id, attempt_number)
);
"""
REQUIRED_V1 = {
    "events": {"event_id", "event_type", "repository", "source_id", "published_at", "first_seen_at", "last_seen_at", "content_hash", "payload_json"},
    "notification_outbox": {"event_id", "priority", "notification_state", "notify_attempts", "first_queued_at", "next_attempt_at", "notified_at", "last_error", "lease_until"},
    "state_metadata": {"key", "value", "updated_at"},
}
REQUIRED_V2 = {
    **REQUIRED_V1,
    "notification_outbox": REQUIRED_V1["notification_outbox"] | {"suppression_reason"},
    "event_revisions": {"event_id", "content_hash", "payload_json", "observed_at"},
    "ai_analyses": {"event_id", "event_content_hash", "schema_version", "prompt_version", "provider", "model", "analysis_json", "created_at"},
    "routing_decisions": {"decision_id", "event_id", "event_content_hash", "policy_version", "decision_json", "created_at"},
    "delivery_attempts": {"event_id", "attempt_number", "event_content_hash", "route", "result", "error_category", "started_at", "completed_at", "next_attempt_at"},
}
KEYS_V1 = {"events": ("event_id",), "notification_outbox": ("event_id",), "state_metadata": ("key",)}
KEYS_V2 = {**KEYS_V1, "event_revisions": ("event_id", "content_hash"),
           "ai_analyses": ("event_id", "event_content_hash", "schema_version", "prompt_version", "provider", "model"),
           "routing_decisions": ("decision_id",), "delivery_attempts": ("event_id", "attempt_number")}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class EventStore:
    def __init__(self, connection: sqlite3.Connection, *, transient: bool = False):
        self.db = connection
        self.db.row_factory = sqlite3.Row
        self.transient = transient

    @classmethod
    def open(cls, path: Path, *, preview: bool = False) -> "EventStore":
        path = Path(path)
        exists = path.exists()
        if exists and (not path.is_file() or path.stat().st_size == 0):
            raise ValueError("state database is empty or not a file")
        if preview:
            db = sqlite3.connect(":memory:")
            try:
                db.execute("PRAGMA foreign_keys=ON")
                if exists:
                    source = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
                    try:
                        cls._validate(source)
                        source.backup(db)
                    finally:
                        source.close()
                    cls._migrate(db)
                else:
                    db.executescript(SCHEMA_V1 + SCHEMA_V2)
                    db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            except Exception:
                db.close()
                raise
            return cls(db, transient=True)
        if exists:
            source = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
            try:
                cls._validate(source)
            finally:
                source.close()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(path)
        db.execute("PRAGMA foreign_keys=ON")
        try:
            if not exists:
                db.executescript(SCHEMA_V1 + SCHEMA_V2)
                db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            else:
                cls._migrate(db)
        except Exception:
            db.close()
            raise
        return cls(db)

    @staticmethod
    def _validate(db: sqlite3.Connection) -> None:
        try:
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("state database integrity check failed")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (1, SCHEMA_VERSION):
                raise ValueError("unsupported state database schema version")
            required = REQUIRED_V1 if version == 1 else REQUIRED_V2
            keys = KEYS_V1 if version == 1 else KEYS_V2
            for table, columns in required.items():
                actual = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
                if not columns <= actual:
                    raise ValueError("state database schema is malformed")
            for table, columns in keys.items():
                actual = tuple(row[1] for row in sorted(db.execute(f"PRAGMA table_info({table})"), key=lambda row: row[5]) if row[5])
                if actual != columns:
                    raise ValueError("state database primary key is malformed")
            for table in ("notification_outbox", *(REQUIRED_V2.keys() - REQUIRED_V1.keys() if version == SCHEMA_VERSION else ())):
                fk = list(db.execute(f"PRAGMA foreign_key_list({table})"))
                if not any(row[2] == "events" and row[3] == "event_id" for row in fk):
                    raise ValueError("state database foreign key is malformed")
            if db.execute("PRAGMA foreign_key_check").fetchone():
                raise ValueError("state database foreign key integrity failed")
        except sqlite3.DatabaseError as exc:
            raise ValueError("state database is malformed") from exc

    @classmethod
    def _migrate(cls, db: sqlite3.Connection) -> None:
        """Upgrade a validated v1 DB atomically; preview calls this only on its RAM copy."""
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version == SCHEMA_VERSION:
            cls._validate(db)
            return
        if version != 1:
            raise ValueError("unsupported state database schema version")
        cls._validate(db)
        db.execute("BEGIN IMMEDIATE")
        try:
            # executescript() implicitly commits, so migration DDL is executed
            # statement-by-statement inside the same transaction as version bump.
            for statement in SCHEMA_V2.split(";"):
                if statement.strip():
                    db.execute(statement)
            db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            cls._validate(db)
        except Exception:
            db.rollback()
            raise
        else:
            db.commit()

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except Exception:
            self.db.rollback()
            raise
        else:
            self.db.commit()

    def close(self) -> None:
        self.db.close()

    def get_meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM state_metadata WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.db.execute("INSERT INTO state_metadata VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at", (key, value, now()))

    def event_ids(self) -> set[str]:
        return {row[0] for row in self.db.execute("SELECT event_id FROM events")}

    def _write_event(self, data: dict, timestamp: str) -> tuple[bool, bool, bool]:
        """Write raw facts and immutable hash revisions; caller owns the transaction."""
        required = ("event_id", "event_type", "source_id", "published_at", "content_hash")
        if (any(not isinstance(data.get(key), str) or not data[key] for key in required)
                or not isinstance(data.get("repository"), str)):
            raise ValueError("normalized event has missing required fields")
        event_id = data["event_id"]
        previous = self.db.execute(
            "SELECT content_hash, payload_json FROM events WHERE event_id=?", (event_id,)
        ).fetchone()
        old_payload = json.loads(previous[1]) if previous is not None else None
        preserve_primary = False
        if old_payload is not None and data["event_type"] == "github_security_advisory":
            old_source = str((old_payload.get("provenance") or {}).get("collector") or "")
            new_source = str((data.get("provenance") or {}).get("collector") or "")
            old_global, new_global = old_source.startswith("global_advisories"), new_source.startswith("global_advisories")
            if old_global and not new_global:
                preserve_primary = True
            elif old_global == new_global:
                old_updated, new_updated = old_payload.get("updated_at"), data.get("updated_at")
                if isinstance(old_updated, str) and isinstance(new_updated, str):
                    preserve_primary = (new_updated < old_updated or
                                        (new_updated == old_updated and bool(old_payload.get("withdrawn_at"))
                                         and not data.get("withdrawn_at")))
        changed = previous is None or previous[0] != data["content_hash"]
        if (changed and previous is not None
                and data["event_type"] == "github_security_advisory"
                and isinstance(data.get("material_hash"), str)):
            if old_payload.get("event_type") == "github_security_advisory" and old_payload.get("material_hash"):
                changed = old_payload["material_hash"] != data["material_hash"]
        if preserve_primary:
            changed = False
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        if preserve_primary:
            self.db.execute("UPDATE events SET last_seen_at=? WHERE event_id=?", (timestamp, event_id))
        else:
            self.db.execute("""INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_id) DO UPDATE SET last_seen_at=excluded.last_seen_at,
                event_type=excluded.event_type, repository=excluded.repository,
                source_id=excluded.source_id, published_at=excluded.published_at,
                content_hash=excluded.content_hash, payload_json=excluded.payload_json""",
                (event_id, data["event_type"], data["repository"], data["source_id"],
                 data["published_at"], timestamp, timestamp, data["content_hash"], payload))
        if previous is not None:
            self.db.execute("""INSERT OR IGNORE INTO event_revisions
                (event_id, content_hash, payload_json, observed_at) VALUES (?, ?, ?, ?)""",
                (event_id, previous[0], previous[1], timestamp))
        self.db.execute("""INSERT OR IGNORE INTO event_revisions
            (event_id, content_hash, payload_json, observed_at) VALUES (?, ?, ?, ?)""",
            (event_id, data["content_hash"], payload, timestamp))
        return previous is None, changed, preserve_primary

    def upsert(self, event, *, suppress: bool = False) -> None:
        data = event.as_dict()
        timestamp = now()
        previous = self.db.execute("SELECT payload_json FROM events WHERE event_id=?", (event.event_id,)).fetchone()
        was_draft = bool(json.loads(previous[0]).get("draft")) if previous else False
        self._write_event(data, timestamp)
        self.db.execute("""INSERT OR IGNORE INTO notification_outbox
            (event_id, priority, notification_state, first_queued_at)
            VALUES (?, ?, ?, ?)""",
            (event.event_id, "SPECIAL" if event.is_special else "NORMAL",
             "SUPPRESSED" if suppress or event.draft else "PENDING_NOTIFICATION", timestamp))
        self.db.execute("UPDATE notification_outbox SET priority=? WHERE event_id=?",
                        ("SPECIAL" if event.is_special else "NORMAL", event.event_id))
        if event.draft:
            # A release can be unpublished after discovery. Do not notify a
            # now-draft release, but never rewrite a delivered acknowledgement
            # or an in-flight lease whose sender owns the result.
            self.db.execute("""UPDATE notification_outbox SET notification_state='SUPPRESSED',
                next_attempt_at=NULL, last_error=NULL
                WHERE event_id=? AND notification_state IN ('PENDING_NOTIFICATION','DELIVERY_FAILED')""",
                (event.event_id,))
        if was_draft and not event.draft:
            self.db.execute("""UPDATE notification_outbox SET notification_state='PENDING_NOTIFICATION',
                next_attempt_at=NULL, last_error=NULL
                WHERE event_id=? AND notification_state='SUPPRESSED'""", (event.event_id,))

    def upsert_raw(self, event: dict, *, suppress: bool = False,
                   suppression_reason: str | None = None) -> bool:
        """Persist a non-Release source event; return True only for a new ID.

        A changed content hash requeues a delivered/suppressed event, but an
        unchanged repeat never changes its acknowledgement state. This method
        deliberately does not infer trust, project mapping, or priority.
        """
        if event.get("event_type") == "github_release":
            raise ValueError("release events must use the P0 upsert path")
        timestamp = now()
        created, changed, preserved = self._write_event(event, timestamp)
        if preserved:
            return False
        state = "SUPPRESSED" if suppress else "PENDING_NOTIFICATION"
        self.db.execute("""INSERT OR IGNORE INTO notification_outbox
            (event_id, priority, notification_state, first_queued_at, suppression_reason)
            VALUES (?, 'NORMAL', ?, ?, ?)""",
            (event["event_id"], state, timestamp, suppression_reason if suppress else None))
        if changed and not created:
            self.db.execute("""UPDATE notification_outbox SET notification_state=?,
                next_attempt_at=NULL, last_error=NULL, lease_until=NULL,
                suppression_reason=?, first_queued_at=?
                WHERE event_id=? AND notification_state != 'DELIVERY_IN_PROGRESS'""",
                (state, suppression_reason if suppress else None, timestamp, event["event_id"]))
            # An in-flight sender owns its lease. Keep its acknowledgement
            # valid, but remember the new suppression decision for post-ack
            # requeue/suppression of the revised content.
            self.db.execute("""UPDATE notification_outbox SET suppression_reason=?
                WHERE event_id=? AND notification_state='DELIVERY_IN_PROGRESS'""",
                (suppression_reason if suppress else None, event["event_id"]))
        elif suppress:
            # A policy tightening must suppress an already queued unchanged
            # event, without rewriting a completed delivery acknowledgement.
            self.db.execute("""UPDATE notification_outbox SET
                notification_state='SUPPRESSED', suppression_reason=?,
                next_attempt_at=NULL, last_error=NULL
                WHERE event_id=? AND notification_state IN
                ('PENDING_NOTIFICATION','DELIVERY_FAILED','SUPPRESSED')""",
                (suppression_reason, event["event_id"]))
        return created

    def get_analysis(self, event_id: str, content_hash: str, schema_version: str,
                     prompt_version: str, provider: str, model: str) -> dict | None:
        row = self.db.execute("""SELECT analysis_json FROM ai_analyses WHERE event_id=?
            AND event_content_hash=? AND schema_version=? AND prompt_version=?
            AND provider=? AND model=?""",
            (event_id, content_hash, schema_version, prompt_version, provider, model)).fetchone()
        return json.loads(row[0]) if row else None

    def save_analysis(self, analysis: dict) -> None:
        """Cache a caller-validated analysis without replacing raw event facts."""
        from .analysis import validate_analysis

        validate_analysis(analysis)
        keys = ("event_id", "event_content_hash", "schema_version", "prompt_version", "provider", "model")
        if any(not isinstance(analysis.get(key), str) or not analysis[key] for key in keys):
            raise ValueError("analysis cache key is incomplete")
        self.db.execute("""INSERT OR IGNORE INTO ai_analyses
            (event_id, event_content_hash, schema_version, prompt_version,
             provider, model, analysis_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (*[analysis[key] for key in keys],
             json.dumps(analysis, ensure_ascii=False, sort_keys=True), analysis.get("created_at") or now()))

    def save_routing(self, event_id: str, decision: dict) -> None:
        row = self.db.execute("SELECT content_hash FROM events WHERE event_id=?", (event_id,)).fetchone()
        if row is None:
            raise ValueError("routing event does not exist")
        version = decision.get("policy_version", "v1")
        payload = json.dumps(decision, ensure_ascii=False, sort_keys=True)
        previous = self.db.execute("""SELECT event_content_hash, policy_version, decision_json
            FROM routing_decisions WHERE event_id=? ORDER BY decision_id DESC LIMIT 1""",
            (event_id,)).fetchone()
        if previous is not None and tuple(previous) == (row[0], version, payload):
            return
        self.db.execute("""INSERT INTO routing_decisions
            (event_id, event_content_hash, policy_version, decision_json, created_at)
            VALUES (?, ?, ?, ?, ?)""",
            (event_id, row[0], version, payload, now()))

    def revisions(self, event_id: str) -> list[dict]:
        rows = self.db.execute("""SELECT content_hash, payload_json, observed_at
            FROM event_revisions WHERE event_id=? ORDER BY observed_at, content_hash""", (event_id,))
        return [dict(content_hash=row[0], payload=json.loads(row[1]), observed_at=row[2]) for row in rows]

    def pending(self, *, include_delayed: bool = False) -> list[dict]:
        timestamp = now()
        rows = self.db.execute("""SELECT e.payload_json, o.notification_state, o.event_id,
                   o.next_attempt_at, o.first_queued_at, o.priority, o.suppression_reason
            FROM notification_outbox o JOIN events e USING(event_id)
            WHERE o.notification_state IN ('PENDING_NOTIFICATION','DELIVERY_FAILED')
            ORDER BY e.published_at, e.event_id""").fetchall()
        pending = []
        for row in rows:
            payload = json.loads(row[0])
            if payload.get("draft"):
                raise ValueError("draft release has retryable outbox state")
            if not include_delayed and row[3] is not None and row[3] > timestamp:
                continue
            pending.append(dict(payload, notification_state=row[1],
                                first_queued_at=row[4], priority=row[5],
                                suppression_reason=row[6]))
        return pending

    def recover_expired_leases(self) -> None:
        with self.transaction():
            rows = self.db.execute("""SELECT o.event_id, e.payload_json FROM notification_outbox o
                JOIN events e USING(event_id) WHERE o.notification_state='DELIVERY_IN_PROGRESS'
                AND o.lease_until <= ?""", (now(),)).fetchall()
            for row in rows:
                draft = bool(json.loads(row[1]).get("draft"))
                self.db.execute("""UPDATE notification_outbox SET notification_state=?,
                    next_attempt_at=NULL, lease_until=NULL, last_error=? WHERE event_id=?""",
                    ("SUPPRESSED" if draft else "DELIVERY_FAILED",
                     None if draft else "lease_expired", row[0]))
                self.db.execute("""UPDATE delivery_attempts SET result='lease_expired',
                    error_category='lease_expired', completed_at=? WHERE event_id=?
                    AND result='in_progress'""", (now(), row[0]))

    def claim(self, event_ids: list[str], lease_seconds: int = 300) -> bool:
        if not event_ids:
            return False
        until = (datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)).isoformat(timespec="seconds").replace("+00:00", "Z")
        with self.transaction():
            # Expired claims can be retried after a crash; unexpired leases are never stolen.
            placeholders = ",".join("?" for _ in event_ids)
            eligible = self.db.execute(f"""SELECT COUNT(*) FROM notification_outbox WHERE event_id IN ({placeholders})
                AND notification_state IN ('PENDING_NOTIFICATION','DELIVERY_FAILED')
                AND (next_attempt_at IS NULL OR next_attempt_at <= ?)""", (*event_ids, now())).fetchone()[0]
            if eligible != len(event_ids):
                return False
            self.db.execute(f"""UPDATE notification_outbox SET notification_state='DELIVERY_IN_PROGRESS',
                notify_attempts=notify_attempts+1, lease_until=? WHERE event_id IN ({placeholders})""", (until, *event_ids))
            for event_id in event_ids:
                row = self.db.execute("""SELECT o.notify_attempts, o.priority, e.content_hash,
                    (SELECT decision_json FROM routing_decisions r WHERE r.event_id=o.event_id
                     ORDER BY decision_id DESC LIMIT 1) AS decision_json
                    FROM notification_outbox o JOIN events e USING(event_id) WHERE o.event_id=?""",
                    (event_id,)).fetchone()
                decision = json.loads(row[3]) if row[3] else {}
                route = decision.get("route") or row[1]
                self.db.execute("""INSERT INTO delivery_attempts
                    (event_id, attempt_number, event_content_hash, route, result, started_at)
                    VALUES (?, ?, ?, ?, 'in_progress', ?)""",
                    (event_id, row[0], row[2], route, now()))
            return True

    def acknowledge(self, event_ids: list[str]) -> None:
        with self.transaction():
            claimed = [event_id for event_id in event_ids if self.db.execute(
                "SELECT 1 FROM notification_outbox WHERE event_id=? AND notification_state='DELIVERY_IN_PROGRESS'",
                (event_id,)).fetchone()]
            self.db.executemany("""UPDATE notification_outbox SET notification_state='DELIVERED',
                notified_at=?, next_attempt_at=NULL, last_error=NULL, lease_until=NULL
                WHERE event_id=? AND notification_state='DELIVERY_IN_PROGRESS'""",
                [(now(), event_id) for event_id in event_ids])
            self.db.executemany("""UPDATE delivery_attempts SET result='delivered',
                completed_at=?, error_category=NULL, next_attempt_at=NULL
                WHERE event_id=? AND result='in_progress'""", [(now(), event_id) for event_id in claimed])
            for event_id in claimed:
                row = self.db.execute("""SELECT e.event_type, e.content_hash, o.suppression_reason,
                    a.event_content_hash FROM events e JOIN notification_outbox o USING(event_id)
                    JOIN delivery_attempts a USING(event_id) WHERE e.event_id=?
                    ORDER BY a.attempt_number DESC LIMIT 1""", (event_id,)).fetchone()
                if row[0] != "github_release" and row[1] != row[3]:
                    state = "SUPPRESSED" if row[2] else "PENDING_NOTIFICATION"
                    self.db.execute("""UPDATE notification_outbox SET notification_state=?,
                        first_queued_at=?, next_attempt_at=NULL WHERE event_id=?""",
                        (state, now(), event_id))

    def fail(self, event_ids: list[str], category: str, retry_after: int | None = None,
             *, endpoint_backoff: bool = False) -> None:
        delay = max(0, retry_after) if retry_after is not None else (60 if endpoint_backoff else 0)
        next_at = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat(timespec="seconds").replace("+00:00", "Z") if delay else None
        with self.transaction():
            if endpoint_backoff and next_at:
                self.set_meta("slack_retry_after", next_at)
            claimed = [event_id for event_id in event_ids if self.db.execute(
                "SELECT 1 FROM notification_outbox WHERE event_id=? AND notification_state='DELIVERY_IN_PROGRESS'",
                (event_id,)).fetchone()]
            self.db.executemany("""UPDATE notification_outbox SET notification_state='DELIVERY_FAILED',
                next_attempt_at=?, last_error=?, lease_until=NULL WHERE event_id=?
                AND notification_state='DELIVERY_IN_PROGRESS'""",
                [(next_at, category, event_id) for event_id in event_ids])
            self.db.executemany("""UPDATE delivery_attempts SET result='failed',
                error_category=?, completed_at=?, next_attempt_at=?
                WHERE event_id=? AND result='in_progress'""",
                [(category, now(), next_at, event_id) for event_id in claimed])

    def defer_after_rate_limit(self, event_ids: list[str]) -> None:
        """Keep selected-but-unsent chunks retryable without counting attempts."""
        deadline = self.get_meta("slack_retry_after")
        with self.transaction():
            self.db.executemany("""UPDATE notification_outbox SET notification_state='DELIVERY_FAILED',
                next_attempt_at=?, last_error='batch_rate_limited'
                WHERE event_id=? AND notification_state IN ('PENDING_NOTIFICATION','DELIVERY_FAILED')""",
                [(deadline, event_id) for event_id in event_ids])
