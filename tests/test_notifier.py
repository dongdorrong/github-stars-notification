from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from starwatch.event_store import EventStore
from starwatch.notifier import SlackResult, deliver
from starwatch.pipeline import run_pipeline
from starwatch.release_collector import FixtureReleaseSource, normalize_release
from starwatch.slack_payload import SlackChunk


class FakeTransport:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = 0

    def send(self, payload):
        self.calls += 1
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class NotifierTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "events.sqlite3"

    def store(self):
        store = EventStore.open(self.db_path)
        self.addCleanup(store.close)
        with store.transaction():
            for number in (1, 2):
                store.upsert(normalize_release("owner/repo", {
                    "id": number, "tag_name": f"v{number}", "published_at": "2026-10-02T00:00:00Z"
                }, fixture=True))
        return store

    def test_unexpected_transport_bug_is_sanitized_and_retryable(self):
        store = self.store()
        transport = FakeTransport(RuntimeError("secret-webhook-url"))
        with self.assertRaisesRegex(RuntimeError, "unexpected Slack transport failure") as error:
            deliver(store, [SlackChunk({"text": "one"}, ["github:release:1"])], transport)
        self.assertNotIn("secret-webhook-url", str(error.exception))
        self.assertEqual(store.pending()[0]["notification_state"], "DELIVERY_FAILED")
        self.assertEqual(store.db.execute("SELECT last_error FROM notification_outbox WHERE event_id='github:release:1'").fetchone()[0],
                         "unexpected_transport_error")

    def test_429_stops_remaining_chunks_and_blocks_next_run_until_deadline(self):
        store = self.store()
        chunks = [SlackChunk({"text": str(number)}, [f"github:release:{number}"]) for number in (1, 2)]
        transport = FakeTransport(SlackResult(429, retry_after=120), SlackResult(200))
        self.assertFalse(deliver(store, chunks, transport))
        self.assertEqual(transport.calls, 1)
        self.assertIsNotNone(store.get_meta("slack_retry_after"))
        row = store.db.execute("SELECT notification_state, notify_attempts FROM notification_outbox WHERE event_id='github:release:2'").fetchone()
        self.assertEqual(tuple(row), ("DELIVERY_FAILED", 0))
        blocked = FakeTransport(SlackResult(200))
        self.assertFalse(deliver(store, chunks, blocked))
        self.assertEqual(blocked.calls, 0)
        with store.transaction():
            store.set_meta("slack_retry_after", "2020-01-01T00:00:00Z")
            store.db.execute("UPDATE notification_outbox SET next_attempt_at=NULL")
        retry = FakeTransport(SlackResult(200), SlackResult(200))
        self.assertTrue(deliver(store, chunks, retry))
        self.assertEqual(retry.calls, 2)

    def test_deferred_chunk_retries_below_threshold_after_first_becomes_draft(self):
        store = self.store()
        chunks = [SlackChunk({"text": str(number)}, [f"github:release:{number}"]) for number in (1, 2)]
        self.assertFalse(deliver(store, chunks, FakeTransport(SlackResult(429, retry_after=120))))
        with store.transaction():
            store.upsert(normalize_release("owner/repo", {
                "id": 1, "tag_name": "v1", "published_at": "2026-10-02T00:00:00Z", "draft": True
            }, fixture=True))
            store.set_meta("slack_retry_after", "2020-01-01T00:00:00Z")
            store.db.execute("UPDATE notification_outbox SET next_attempt_at=NULL WHERE event_id='github:release:2'")
        pending = store.pending()
        self.assertEqual([item["event_id"] for item in pending], ["github:release:2"])
        self.assertEqual(pending[0]["notification_state"], "DELIVERY_FAILED")
        from starwatch.policy import select
        self.assertEqual(select(pending, {"notification": {"min_release_count": 5,
                          "special_project_always_notify": False}}).reason, "retry_failed_delivery")
        retry = FakeTransport(SlackResult(200))
        self.assertTrue(deliver(store, [chunks[1]], retry))
        self.assertEqual(retry.calls, 1)

    def test_legacy_migration_never_sends_on_cutover_but_newer_pending_can_send_later(self):
        legacy = self.root / "legacy.json"
        legacy.write_text(json.dumps({"owner/repo": {"tag": "v1", "published": "2026-10-01 00:00:00"}}))
        fixture = self.root / "fixture.json"
        fixture.write_text(json.dumps({"owner/repo": [
            {"id": 1, "tag_name": "v1", "published_at": "2026-10-01T00:00:00Z"},
            {"id": 2, "tag_name": "v2", "published_at": "2026-10-02T00:00:00Z"},
        ]}))
        config = {"special_projects": [], "notification": {"min_release_count": 1,
                  "special_project_always_notify": False, "first_run_notify": False,
                  "cutover_pending_policy": "preserve_pending"}}
        transport = FakeTransport(SlackResult(200))
        kwargs = dict(state_path=self.db_path, legacy_path=legacy, repos=["owner/repo"],
                      source=FixtureReleaseSource(fixture), config=config, mode="commit",
                      send_slack=True, transport=transport)
        first = run_pipeline(**kwargs)
        self.assertEqual(transport.calls, 0)
        self.assertIsNone(first.delivery_succeeded)
        second = run_pipeline(**kwargs)
        self.assertTrue(second.delivery_succeeded)
        self.assertEqual(transport.calls, 1)


if __name__ == "__main__":
    unittest.main()
