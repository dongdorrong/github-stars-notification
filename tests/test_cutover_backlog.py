"""Legacy cutover must baseline observed releases without a Slack backlog."""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

from starwatch.event_store import EventStore
from starwatch.notifier import SlackResult


SCRIPT = Path(__file__).resolve().parents[1] / ".github/scripts/check_release.py"
spec = importlib.util.spec_from_file_location("cutover_check_release", SCRIPT)
script = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = script
spec.loader.exec_module(script)


def release(number: int) -> dict:
    return {"id": number, "tag_name": f"v{number}",
            "published_at": "2026-10-03T00:00:00Z",
            "html_url": f"https://example.invalid/releases/{number}"}


class RecordingTransport:
    def __init__(self):
        self.calls = 0

    def send(self, payload):
        self.calls += 1
        return SlackResult(200)


class CutoverBacklogTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.db = self.root / "events.sqlite3"
        self.legacy = self.root / "releases.json"
        self.legacy.write_text(json.dumps({"owner/repo": {
            "tag": "v1", "published": "2026-10-01T00:00:00Z"}}))
        self.legacy_bytes = self.legacy.read_bytes()
        self.repos = self.root / "repos.txt"
        self.repos.write_text("owner/repo\n")
        self.config = self.root / "config.yaml"
        self.config.write_text("notification:\n  min_release_count: 1\n  first_run_notify: true\n")
        self.fixture = self.root / "fixture.json"
        self.feed = self.root / "feed.json"
        self.output = self.root / "output.txt"

    def run_fixture(self, releases, *, mode="commit", send=False, transport=None):
        self.fixture.write_text(json.dumps({"owner/repo": releases}))
        args = script.build_arg_parser().parse_args([
            "--repos-file", str(self.repos), "--cache-path", str(self.legacy),
            "--state-db", str(self.db), "--fixture-releases", str(self.fixture),
            "--config", str(self.config), "--feed-path", str(self.feed),
            "--github-output", str(self.output), "--mode", mode,
            *(["--send-slack"] if send else []),
        ])
        status = script.run(args, transport=transport)
        return status, json.loads(self.feed.read_text())

    def test_first_commit_suppresses_71_observed_then_only_new_release_is_pending(self):
        transport = RecordingTransport()
        status, first = self.run_fixture([release(n) for n in range(1, 72)],
                                         send=True, transport=transport)
        self.assertEqual(status, 0)
        self.assertEqual(first["new_release_count"], 71)
        self.assertEqual(first["cutover_backlog_suppressed_count"], 71)
        self.assertTrue(first["cutover_policy_applied"])
        self.assertIn("cutover_policy_applied=true", self.output.read_text())
        self.assertIn("cutover_backlog_suppressed_count=71", self.output.read_text())
        self.assertEqual(first["pending_count"], 0)
        self.assertEqual(first["notification_batch_count"], 0)
        self.assertEqual(transport.calls, 0)
        self.assertEqual(self.legacy.read_bytes(), self.legacy_bytes)
        store = EventStore.open(self.db, preview=True)
        try:
            self.assertEqual(len(store.event_ids()), 71)
            self.assertEqual(store.get_meta("legacy_cutover_pending_policy"), "suppress_existing")
            self.assertEqual(dict(store.db.execute(
                "SELECT notification_state, COUNT(*) FROM notification_outbox "
                "GROUP BY notification_state").fetchall()), {"SUPPRESSED": 71})
        finally:
            store.close()

        status, second = self.run_fixture([release(n) for n in range(1, 73)])
        self.assertEqual(status, 0)
        self.assertEqual(second["new_release_count"], 1)
        self.assertEqual(second["cutover_backlog_suppressed_count"], 0)
        self.assertFalse(second["cutover_policy_applied"])
        self.assertEqual(second["pending_release_count"], 1)
        self.assertEqual([item["event_id"] for item in second["notification_batch"]],
                         ["github:release:72"])
        self.assertEqual(second["pending_count"], 1)
        self.assertEqual(self.legacy.read_bytes(), self.legacy_bytes)

    def test_preview_simulates_cutover_without_writing_db_or_legacy(self):
        transport = RecordingTransport()
        status, feed = self.run_fixture([release(1)], mode="preview", transport=transport)
        self.assertEqual(status, 0)
        self.assertEqual(feed["cutover_backlog_suppressed_count"], 1)
        self.assertEqual(feed["pending_count"], 0)
        self.assertFalse(self.db.exists())
        self.assertEqual(self.legacy.read_bytes(), self.legacy_bytes)
        self.assertEqual(transport.calls, 0)

    def test_invalid_cutover_policy_fails_before_state_creation(self):
        self.config.write_text("notification:\n  cutover_pending_policy: unknown\n")
        with self.assertRaisesRegex(ValueError, "cutover_pending_policy"):
            self.run_fixture([release(1)])
        self.assertFalse(self.db.exists())
        self.assertEqual(self.legacy.read_bytes(), self.legacy_bytes)

    def test_existing_migrated_pending_and_retry_rows_are_not_suppressed(self):
        self.run_fixture([release(1), release(2)])
        store = EventStore.open(self.db)
        try:
            with store.transaction():
                store.db.execute("UPDATE notification_outbox SET notification_state='PENDING_NOTIFICATION' "
                                 "WHERE event_id='github:release:1'")
                store.db.execute("UPDATE notification_outbox SET notification_state='DELIVERY_FAILED' "
                                 "WHERE event_id='github:release:2'")
        finally:
            store.close()
        status, feed = self.run_fixture([release(1), release(2)])
        self.assertEqual(status, 0)
        self.assertEqual(feed["cutover_backlog_suppressed_count"], 0)
        self.assertEqual(feed["pending_count"], 2)
        store = EventStore.open(self.db, preview=True)
        try:
            self.assertEqual(dict(store.db.execute(
                "SELECT event_id, notification_state FROM notification_outbox").fetchall()), {
                    "github:release:1": "PENDING_NOTIFICATION",
                    "github:release:2": "DELIVERY_FAILED",
                })
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
