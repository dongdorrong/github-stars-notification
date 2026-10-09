"""Schema upgrade, raw revisions and auditable delivery without live services."""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from starwatch.event_store import EventStore, SCHEMA_V1, SCHEMA_VERSION


def advisory(content_hash="sha256:first", summary="First"):
    return {
        "event_id": "github:ghsa:GHSA-aaaa-bbbb-cccc",
        "event_type": "github_advisory",
        "repository": "owner/repo",
        "source_id": "github:ghsa:GHSA-aaaa-bbbb-cccc",
        "published_at": "2026-10-01T00:00:00Z",
        "content_hash": content_hash,
        "summary": summary,
        "visibility": "public",
    }


class StoreV2Tests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "events.sqlite3"

    def legacy_v1(self):
        db = sqlite3.connect(self.path)
        db.executescript(SCHEMA_V1)
        db.execute("PRAGMA user_version=1")
        db.execute("""INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""", (
            "github:release:1", "github_release", "owner/repo", "1", "2026-10-01T00:00:00Z",
            "2026-10-01T00:00:01Z", "2026-10-01T00:00:01Z", "sha256:old",
            json.dumps({"event_id": "github:release:1", "body": "original"})))
        db.execute("""INSERT INTO notification_outbox
            (event_id, priority, notification_state, notify_attempts, first_queued_at, notified_at)
            VALUES ('github:release:1', 'SPECIAL', 'DELIVERED', 3,
                    '2026-10-01T00:00:01Z', '2026-10-01T00:00:02Z')""")
        db.execute("INSERT INTO state_metadata VALUES ('legacy_migrated', '1', '2026-10-01T00:00:01Z')")
        db.commit()
        db.close()

    def test_upgrade_preserves_v1_history_and_is_idempotent(self):
        self.legacy_v1()
        store = EventStore.open(self.path)
        self.assertEqual(store.db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
        outbox = store.db.execute("SELECT * FROM notification_outbox").fetchone()
        self.assertEqual((outbox["notification_state"], outbox["notify_attempts"],
                          outbox["notified_at"]), ("DELIVERED", 3, "2026-10-01T00:00:02Z"))
        self.assertEqual(store.get_meta("legacy_migrated"), "1")
        self.assertEqual(store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)
        store.close()
        before = self.path.read_bytes()
        EventStore.open(self.path).close()
        self.assertEqual(self.path.read_bytes(), before)

    def test_preview_migrates_only_memory_and_keeps_source_bytes(self):
        self.legacy_v1()
        before = self.path.read_bytes()
        store = EventStore.open(self.path, preview=True)
        self.assertEqual(store.db.execute("PRAGMA user_version").fetchone()[0], 2)
        with store.transaction():
            store.upsert_raw(advisory())
        self.assertEqual(store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 2)
        store.close()
        self.assertEqual(self.path.read_bytes(), before)
        with closing(sqlite3.connect(self.path)) as source:
            self.assertEqual(source.execute("PRAGMA user_version").fetchone()[0], 1)

    def test_failed_upgrade_rolls_back_schema_and_data(self):
        self.legacy_v1()
        before = self.path.read_bytes()
        with patch("starwatch.event_store.SCHEMA_V2", "ALTER TABLE notification_outbox ADD COLUMN suppression_reason TEXT; INVALID SQL;"):
            with self.assertRaises(sqlite3.DatabaseError):
                EventStore.open(self.path)
        self.assertEqual(self.path.read_bytes(), before)
        with closing(sqlite3.connect(self.path)) as source:
            self.assertEqual(source.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(source.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)

    def test_malformed_v1_or_v2_fails_closed_without_replacement(self):
        self.legacy_v1()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("DROP TABLE notification_outbox")
            db.commit()
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "schema is malformed"):
            EventStore.open(self.path)
        self.assertEqual(self.path.read_bytes(), before)
        self.path.unlink()
        store = EventStore.open(self.path)
        store.db.execute("DROP TABLE event_revisions")
        store.close()
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "schema is malformed"):
            EventStore.open(self.path, preview=True)
        self.assertEqual(self.path.read_bytes(), before)

    def test_raw_update_retains_revisions_and_requeues_only_changed_hash(self):
        store = EventStore.open(self.path)
        with store.transaction():
            self.assertTrue(store.upsert_raw(advisory(), suppress=True, suppression_reason="bootstrap"))
        self.assertEqual(store.pending(), [])
        with store.transaction():
            self.assertFalse(store.upsert_raw(advisory()))
        self.assertEqual(store.pending(), [])
        with store.transaction():
            self.assertFalse(store.upsert_raw(advisory("sha256:changed", "Changed")))
        self.assertEqual([item["event_id"] for item in store.pending()], [advisory()["event_id"]])
        self.assertEqual({item["content_hash"] for item in store.revisions(advisory()["event_id"])},
                         {"sha256:first", "sha256:changed"})
        self.assertTrue(store.claim([advisory()["event_id"]]))
        store.acknowledge([advisory()["event_id"]])
        self.assertEqual(store.pending(), [])
        with store.transaction():
            store.upsert_raw(advisory("sha256:changed", "Changed"))
        self.assertEqual(store.pending(), [])
        store.close()

    def test_unmapped_global_advisory_keeps_empty_repository_and_suppression(self):
        store = EventStore.open(self.path)
        event = advisory()
        event["repository"] = ""
        with store.transaction():
            store.upsert_raw(event, suppress=True, suppression_reason="unmapped_global_advisory")
        self.assertEqual(store.pending(), [])
        row = store.db.execute("""SELECT e.repository, o.notification_state,
            o.suppression_reason FROM events e JOIN notification_outbox o USING(event_id)""").fetchone()
        self.assertEqual(tuple(row), ("", "SUPPRESSED", "unmapped_global_advisory"))
        store.close()

    def test_tightened_suppression_applies_to_unchanged_pending(self):
        store = EventStore.open(self.path)
        with store.transaction():
            store.upsert_raw(advisory())
            store.upsert_raw(advisory(), suppress=True, suppression_reason="visibility_not_public")
        self.assertEqual(store.pending(), [])
        self.assertEqual(store.db.execute("SELECT suppression_reason FROM notification_outbox").fetchone()[0],
                         "visibility_not_public")
        store.close()

    def test_analysis_cache_and_routing_are_separate_from_raw(self):
        store = EventStore.open(self.path)
        event = advisory("sha256:" + "a" * 64)
        with store.transaction():
            store.upsert_raw(event)
            analysis = {"event_id": event["event_id"], "event_content_hash": event["content_hash"],
                        "schema_version": "k8s-intelligence-analysis/v1", "prompt_version": "v1",
                        "provider": "fixture", "model": "fake", "summary_ko": "검토",
                        "impact": "high", "categories": ["security"], "operator_attention": True,
                        "reason": "advisory", "recommended_actions": ["Review advisory"],
                        "affected_components": [], "confidence": 0.9,
                        "input_truncated": False, "created_at": "2026-10-01T00:00:00Z"}
            store.save_analysis(analysis)
            store.save_analysis(analysis)
            store.save_routing(event["event_id"], {"policy_version": "v1", "route": "HIGH"})
            store.save_routing(event["event_id"], {"policy_version": "v1", "route": "HIGH"})
        self.assertEqual(store.get_analysis(event["event_id"], event["content_hash"],
                                            "k8s-intelligence-analysis/v1", "v1", "fixture", "fake"), analysis)
        self.assertIsNone(store.get_analysis(event["event_id"], event["content_hash"],
                                             "k8s-intelligence-analysis/v1", "v2", "fixture", "fake"))
        self.assertEqual(store.db.execute("SELECT COUNT(*) FROM ai_analyses").fetchone()[0], 1)
        self.assertEqual(store.db.execute("SELECT COUNT(*) FROM routing_decisions").fetchone()[0], 1)
        with store.transaction():
            store.save_routing(event["event_id"], {"policy_version": "v1", "route": "CRITICAL"})
        self.assertEqual(store.db.execute("SELECT COUNT(*) FROM routing_decisions").fetchone()[0], 2)
        self.assertEqual(json.loads(store.db.execute("SELECT payload_json FROM events").fetchone()[0]), event)
        self.assertTrue(store.claim([event["event_id"]]))
        self.assertEqual(store.db.execute("SELECT route FROM delivery_attempts").fetchone()[0], "CRITICAL")
        store.fail([event["event_id"]], "server_error")
        attempt = store.db.execute("SELECT result, error_category FROM delivery_attempts").fetchone()
        self.assertEqual(tuple(attempt), ("failed", "server_error"))
        store.close()

    def test_schema_invalid_analysis_is_not_persisted(self):
        store = EventStore.open(self.path)
        with store.transaction():
            store.upsert_raw(advisory("sha256:" + "a" * 64))
            with self.assertRaisesRegex(ValueError, "analysis schema"):
                store.save_analysis({"event_id": advisory()["event_id"], "provider": "fake"})
        self.assertEqual(store.db.execute("SELECT COUNT(*) FROM ai_analyses").fetchone()[0], 0)
        store.close()

    def test_revision_during_inflight_delivery_is_requeued_after_ack(self):
        store = EventStore.open(self.path)
        event = advisory()
        with store.transaction():
            store.upsert_raw(event)
        self.assertTrue(store.claim([event["event_id"]]))
        with store.transaction():
            store.upsert_raw(advisory("sha256:second", "New advisory impact"))
        store.acknowledge([event["event_id"]])
        self.assertEqual([item["content_hash"] for item in store.pending()], ["sha256:second"])
        self.assertEqual(store.db.execute("SELECT result, event_content_hash FROM delivery_attempts").fetchone()[0], "delivered")
        store.close()


if __name__ == "__main__":
    unittest.main()
