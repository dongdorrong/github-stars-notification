"""SQLite source events and notification outbox. No network operations live here."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA_VERSION = 1
SCHEMA = """
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
REQUIRED = {
    "events": {"event_id", "event_type", "repository", "source_id", "published_at", "first_seen_at", "last_seen_at", "content_hash", "payload_json"},
    "notification_outbox": {"event_id", "priority", "notification_state", "notify_attempts", "first_queued_at", "next_attempt_at", "notified_at", "last_error", "lease_until"},
    "state_metadata": {"key", "value", "updated_at"},
}


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
            if exists:
                source = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
                try:
                    cls._validate(source)
                    source.backup(db)
                finally:
                    source.close()
            else:
                db.executescript(SCHEMA)
                db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
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
        if not exists:
            db.executescript(SCHEMA)
            db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        return cls(db)

    @staticmethod
    def _validate(db: sqlite3.Connection) -> None:
        try:
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("state database integrity check failed")
            if db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
                raise ValueError("unsupported state database schema version")
            for table, columns in REQUIRED.items():
                actual = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
                if not columns <= actual:
                    raise ValueError("state database schema is malformed")
            for table, key in (("events", "event_id"), ("notification_outbox", "event_id"), ("state_metadata", "key")):
                if not any(row[1] == key and row[5] == 1 for row in db.execute(f"PRAGMA table_info({table})")):
                    raise ValueError("state database primary key is malformed")
            fk = list(db.execute("PRAGMA foreign_key_list(notification_outbox)"))
            if not any(row[2] == "events" and row[3] == "event_id" for row in fk):
                raise ValueError("state database foreign key is malformed")
            if db.execute("PRAGMA foreign_key_check").fetchone():
                raise ValueError("state database foreign key integrity failed")
        except sqlite3.DatabaseError as exc:
            raise ValueError("state database is malformed") from exc

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

    def upsert(self, event, *, suppress: bool = False) -> None:
        data = event.as_dict()
        timestamp = now()
        previous = self.db.execute("SELECT payload_json FROM events WHERE event_id=?", (event.event_id,)).fetchone()
        was_draft = bool(json.loads(previous[0]).get("draft")) if previous else False
        self.db.execute("""INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_id) DO UPDATE SET last_seen_at=excluded.last_seen_at,
            event_type=excluded.event_type, repository=excluded.repository,
            source_id=excluded.source_id, published_at=excluded.published_at,
            content_hash=excluded.content_hash, payload_json=excluded.payload_json""",
            (event.event_id, event.event_type, event.repository, event.source_id,
             event.published_at, timestamp, timestamp, event.content_hash,
             json.dumps(data, ensure_ascii=False, sort_keys=True)))
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

    def pending(self, *, include_delayed: bool = False) -> list[dict]:
        timestamp = now()
        rows = self.db.execute("""SELECT e.payload_json, o.notification_state, o.event_id,
                   o.next_attempt_at
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
            pending.append(dict(payload, notification_state=row[1]))
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
            return True

    def acknowledge(self, event_ids: list[str]) -> None:
        with self.transaction():
            self.db.executemany("""UPDATE notification_outbox SET notification_state='DELIVERED',
                notified_at=?, next_attempt_at=NULL, last_error=NULL, lease_until=NULL
                WHERE event_id=? AND notification_state='DELIVERY_IN_PROGRESS'""",
                [(now(), event_id) for event_id in event_ids])

    def fail(self, event_ids: list[str], category: str, retry_after: int | None = None,
             *, endpoint_backoff: bool = False) -> None:
        delay = max(0, retry_after) if retry_after is not None else (60 if endpoint_backoff else 0)
        next_at = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat(timespec="seconds").replace("+00:00", "Z") if delay else None
        with self.transaction():
            if endpoint_backoff and next_at:
                self.set_meta("slack_retry_after", next_at)
            self.db.executemany("""UPDATE notification_outbox SET notification_state='DELIVERY_FAILED',
                next_attempt_at=?, last_error=?, lease_until=NULL WHERE event_id=?
                AND notification_state='DELIVERY_IN_PROGRESS'""",
                [(next_at, category, event_id) for event_id in event_ids])

    def defer_after_rate_limit(self, event_ids: list[str]) -> None:
        """Keep selected-but-unsent chunks retryable without counting attempts."""
        deadline = self.get_meta("slack_retry_after")
        with self.transaction():
            self.db.executemany("""UPDATE notification_outbox SET notification_state='DELIVERY_FAILED',
                next_attempt_at=?, last_error='batch_rate_limited'
                WHERE event_id=? AND notification_state IN ('PENDING_NOTIFICATION','DELIVERY_FAILED')""",
                [(deadline, event_id) for event_id in event_ids])
