from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from starwatch.event_store import EventStore
from starwatch.release_collector import FixtureReleaseSource, normalize_release
from starwatch.pipeline import run_pipeline


def event(number=1, **overrides):
    raw = {"id": number, "tag_name": f"v{number}", "published_at": "2026-10-02T00:00:00Z", **overrides}
    return normalize_release("owner/repo", raw, fixture=True)


class EventStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "events.sqlite3"

    def test_upsert_preserves_first_seen_and_updates_raw_payload(self):
        store = EventStore.open(self.db_path)
        with store.transaction():
            store.upsert(event())
        first = store.db.execute("SELECT first_seen_at, content_hash FROM events").fetchone()
        changed = replace(event(body="edited release notes", published_at="2026-10-03T00:00:00Z"), is_special=True)
        with store.transaction():
            store.upsert(changed)
        row = store.db.execute("SELECT * FROM events").fetchone()
        self.assertEqual(row["first_seen_at"], first["first_seen_at"])
        self.assertNotEqual(row["content_hash"], first["content_hash"])
        self.assertEqual(json.loads(row["payload_json"])["body"], "edited release notes")
        self.assertEqual(row["published_at"], "2026-10-03T00:00:00Z")
        self.assertEqual(store.db.execute("SELECT priority FROM notification_outbox").fetchone()[0], "SPECIAL")
        self.assertEqual(store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)
        store.close()

    def test_transaction_rolls_back_event_and_outbox(self):
        store = EventStore.open(self.db_path)
        with self.assertRaisesRegex(RuntimeError, "abort"):
            with store.transaction():
                store.upsert(event())
                raise RuntimeError("abort")
        self.assertEqual(store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0)
        self.assertEqual(store.db.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0], 0)
        store.close()

    def test_unknown_schema_version_fails_closed(self):
        store = EventStore.open(self.db_path)
        store.db.execute("PRAGMA user_version=999")
        store.close()
        before = self.db_path.read_bytes()
        with self.assertRaisesRegex(ValueError, "schema version"):
            EventStore.open(self.db_path)
        self.assertEqual(self.db_path.read_bytes(), before)

    def test_expired_delivery_lease_becomes_retryable(self):
        store = EventStore.open(self.db_path)
        with store.transaction():
            store.upsert(event())
        self.assertTrue(store.claim([event().event_id]))
        with store.transaction():
            store.db.execute("UPDATE notification_outbox SET lease_until='2020-01-01T00:00:00Z'")
        self.assertEqual(store.pending(), [])
        store.recover_expired_leases()
        self.assertEqual(store.pending()[0]["notification_state"], "DELIVERY_FAILED")
        store.close()

    def test_expired_delivery_lease_on_now_draft_is_suppressed(self):
        store = EventStore.open(self.db_path)
        with store.transaction():
            store.upsert(event())
        self.assertTrue(store.claim([event().event_id]))
        with store.transaction():
            store.upsert(event(draft=True))
            store.db.execute("UPDATE notification_outbox SET lease_until='2020-01-01T00:00:00Z'")
        store.recover_expired_leases()
        self.assertEqual(store.pending(), [])
        self.assertEqual(store.db.execute("SELECT notification_state FROM notification_outbox").fetchone()[0], "SUPPRESSED")
        store.close()

    def test_draft_becomes_pending_when_published(self):
        store = EventStore.open(self.db_path)
        with store.transaction():
            store.upsert(event(draft=True), suppress=True)
        self.assertEqual(store.pending(), [])
        with store.transaction():
            store.upsert(event(draft=False))
        self.assertEqual(len(store.pending()), 1)
        store.close()

    def test_published_draft_published_is_not_sent_while_draft(self):
        store = EventStore.open(self.db_path)
        with store.transaction():
            store.upsert(event())
        self.assertEqual(len(store.pending()), 1)
        with store.transaction():
            store.upsert(event(draft=True))
        self.assertEqual(store.pending(), [])
        self.assertEqual(store.db.execute("SELECT notification_state FROM notification_outbox").fetchone()[0], "SUPPRESSED")
        with store.transaction():
            store.upsert(event(draft=False))
        self.assertEqual(len(store.pending()), 1)
        store.close()

    def test_failed_delivery_turning_draft_stays_suppressed_until_published(self):
        store = EventStore.open(self.db_path)
        with store.transaction():
            store.upsert(event())
        self.assertTrue(store.claim([event().event_id]))
        store.fail([event().event_id], "server_error")
        with store.transaction():
            store.upsert(event(draft=True))
        self.assertEqual(store.pending(), [])
        with store.transaction():
            store.upsert(event(draft=False))
        self.assertEqual(store.pending()[0]["notification_state"], "PENDING_NOTIFICATION")
        store.close()

    def test_delivered_release_turning_draft_stays_delivered(self):
        store = EventStore.open(self.db_path)
        with store.transaction():
            store.upsert(event())
        self.assertTrue(store.claim([event().event_id]))
        store.acknowledge([event().event_id])
        with store.transaction():
            store.upsert(event(draft=True))
            store.upsert(event(draft=False))
        self.assertEqual(store.pending(), [])
        self.assertEqual(store.db.execute("SELECT notification_state FROM notification_outbox").fetchone()[0], "DELIVERED")
        store.close()

    def test_corrupt_draft_retryable_state_fails_closed(self):
        store = EventStore.open(self.db_path)
        with store.transaction():
            store.upsert(event(draft=True), suppress=True)
            store.db.execute("UPDATE notification_outbox SET notification_state='DELIVERY_FAILED'")
        with self.assertRaisesRegex(ValueError, "draft release has retryable outbox state"):
            store.pending()
        self.assertEqual(store.db.execute("SELECT notification_state FROM notification_outbox").fetchone()[0],
                         "DELIVERY_FAILED")
        store.close()

    def test_legacy_baseline_normalizes_timestamp_formats_and_offsets(self):
        legacy = self.root / "legacy.json"
        legacy.write_text(json.dumps({"owner/repo": {"tag": "v1", "published": "2026-10-02 00:00:00"}}))
        fixture = self.root / "fixture.json"
        fixture.write_text(json.dumps({"owner/repo": [
            {"id": 1, "tag_name": "v1", "published_at": "2026-10-02T09:00:00+09:00"},
            {"id": 2, "tag_name": "v2", "published_at": "2026-10-03T00:00:00Z"},
        ]}))
        config = {"special_projects": [], "notification": {"min_release_count": 5,
                  "special_project_always_notify": False, "first_run_notify": False,
                  "cutover_pending_policy": "preserve_pending"}}
        result = run_pipeline(state_path=self.db_path, legacy_path=legacy, repos=["owner/repo"],
                              source=FixtureReleaseSource(fixture), config=config, mode="commit")
        self.assertEqual(len(result.pending), 1)
        self.assertEqual(result.pending[0]["event_id"], "github:release:2")


if __name__ == "__main__":
    unittest.main()
