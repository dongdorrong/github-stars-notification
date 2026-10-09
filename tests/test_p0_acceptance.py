"""Token-free, multi-run acceptance tests for reliable release notification."""

from __future__ import annotations

import json
import importlib.util
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from starwatch.event_store import EventStore
from starwatch.notifier import SlackResult, deliver
from starwatch.pipeline import run_pipeline
from starwatch.policy import select
from starwatch.release_collector import FixtureReleaseSource, collect_releases, normalize_release
from starwatch.slack_payload import SlackChunk


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github" / "scripts" / "check_release.py"
POLICY = {"notification": {"min_release_count": 5, "special_project_always_notify": True,
                           "first_run_notify": False, "max_slack_text_length": 35_000}}


def release(release_id: int, *, published: str = "2026-10-01T00:00:00Z") -> dict:
    return {
        "id": release_id,
        "tag_name": f"v{release_id}",
        "name": f"Release {release_id}",
        "published_at": published,
        "html_url": f"https://github.com/owner/repo/releases/tag/v{release_id}",
    }


def event(release_id: int):
    return normalize_release("owner/repo", release(release_id))


class FakeTransport:
    def __init__(self, *outcomes: SlackResult | Exception):
        self.outcomes = list(outcomes)
        self.payloads: list[dict[str, str]] = []

    def send(self, payload: dict[str, str]) -> SlackResult:
        self.payloads.append(payload)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class P0AcceptanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "state.sqlite"

    def open_store(self, *, preview: bool = False) -> EventStore:
        store = EventStore.open(self.db_path, preview=preview)
        self.addCleanup(store.close)
        return store

    def cli(self, *args: object, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *(str(arg) for arg in args)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=20,
            env=env,
        )

    def fixture_paths(self, data: dict) -> tuple[Path, Path, Path, Path]:
        repos = self.root / "repos.txt"
        repos.write_text("owner/repo\n", encoding="utf-8")
        fixture = self.root / "releases.json"
        fixture.write_text(json.dumps(data), encoding="utf-8")
        config = self.root / "config.yaml"
        config.write_text("notification:\n  min_release_count: 5\n  first_run_notify: false\n", encoding="utf-8")
        feed = self.root / "feed.json"
        return repos, fixture, config, feed

    def pipeline_fixture(self, raw_releases: list[dict], *, send: bool = False,
                         transport: FakeTransport | None = None):
        fixture = self.root / "pipeline-releases.json"
        fixture.write_text(json.dumps({"owner/repo": raw_releases}), encoding="utf-8")
        return run_pipeline(
            state_path=self.db_path,
            legacy_path=self.root / "legacy.json",
            repos=["owner/repo"],
            source=FixtureReleaseSource(fixture),
            config={"special_projects": [], **POLICY},
            mode="commit", send_slack=send, transport=transport,
        )

    def test_full_pipeline_accumulates_four_then_sends_five_once(self) -> None:
        self.pipeline_fixture([])  # Safe empty first-run baseline.
        four = self.pipeline_fixture([release(i) for i in range(1, 5)])
        self.assertEqual(len(four.pending), 4)
        self.assertFalse(four.decision.should_notify)

        transport = FakeTransport(SlackResult(200))
        fifth = self.pipeline_fixture([release(i) for i in range(1, 6)], send=True, transport=transport)
        self.assertEqual(len(fifth.pending), 5)
        self.assertTrue(fifth.delivery_succeeded)
        self.assertEqual(len(transport.payloads), 1)
        self.assertEqual(transport.payloads[0]["text"].count("github:release:"), 5)

        repeat = FakeTransport(SlackResult(200))
        again = self.pipeline_fixture([release(i) for i in range(5, 0, -1)], send=True, transport=repeat)
        self.assertFalse(again.decision.should_notify)
        self.assertEqual(repeat.payloads, [])
        self.assertEqual(self.open_store().pending(), [])

    def test_cache_miss_suppresses_bulk_bootstrap_even_with_slack_enabled(self) -> None:
        transport = FakeTransport(SlackResult(200))
        initial = self.pipeline_fixture([release(i) for i in range(1, 11)], send=True, transport=transport)
        self.assertTrue(initial.first_run)
        self.assertFalse(initial.decision.should_notify)
        self.assertEqual(initial.pending, [])
        self.assertEqual(transport.payloads, [])
        store = self.open_store()
        self.assertEqual(len(store.event_ids()), 10)  # Observed, not discarded.

    def test_preview_does_not_send_or_consume_a_release_needed_by_later_commit(self) -> None:
        self.pipeline_fixture([])
        before = self.db_path.read_bytes()
        fixture = self.root / "preview-releases.json"
        fixture.write_text(json.dumps({"owner/repo": [release(1)]}), encoding="utf-8")
        transport = FakeTransport(SlackResult(200))
        preview = run_pipeline(
            state_path=self.db_path, legacy_path=self.root / "legacy.json",
            repos=["owner/repo"], source=FixtureReleaseSource(fixture),
            config={"special_projects": [], **POLICY}, mode="preview",
            transport=transport,
        )
        self.assertEqual([item["event_id"] for item in preview.new_events], ["github:release:1"])
        self.assertEqual(transport.payloads, [])
        self.assertEqual(self.db_path.read_bytes(), before)

        committed = self.pipeline_fixture([release(1)])
        self.assertEqual([item["event_id"] for item in committed.new_events], ["github:release:1"])
        self.assertEqual([item["event_id"] for item in committed.pending], ["github:release:1"])

    def test_four_pending_then_one_next_run_selects_all_five(self) -> None:
        first = self.open_store()
        with first.transaction():
            for release_id in range(1, 5):
                first.upsert(event(release_id))
        self.assertFalse(select(first.pending(), POLICY).should_notify)
        self.assertEqual(len(first.pending()), 4)
        first.close()

        second = self.open_store()
        with second.transaction():
            second.upsert(event(5))
        candidates = second.pending()
        self.assertTrue(select(candidates, POLICY).should_notify)
        self.assertEqual({item["event_id"] for item in candidates}, {
            f"github:release:{release_id}" for release_id in range(1, 6)
        })

    def test_partial_payload_success_retries_only_failed_chunk_below_threshold(self) -> None:
        store = self.open_store()
        with store.transaction():
            for release_id in range(1, 6):
                store.upsert(event(release_id))
        chunks = [
            SlackChunk({"text": "first"}, ["github:release:1", "github:release:2", "github:release:3"]),
            SlackChunk({"text": "second"}, ["github:release:4", "github:release:5"]),
        ]
        failed = FakeTransport(SlackResult(200), SlackResult(500))
        self.assertFalse(deliver(store, chunks, failed))
        self.assertEqual([payload["text"] for payload in failed.payloads], ["first", "second"])
        self.assertEqual({item["event_id"] for item in store.pending()}, {
            "github:release:4", "github:release:5"
        })
        self.assertEqual(select(store.pending(), POLICY).reason, "retry_failed_delivery")

        retry = FakeTransport(SlackResult(200))
        self.assertTrue(deliver(store, [chunks[1]], retry))
        self.assertEqual(retry.payloads, [{"text": "second"}])
        self.assertEqual(store.pending(), [])
        self.assertFalse(select(store.pending(), POLICY).should_notify)

    def test_rate_limit_respects_retry_after_without_marking_delivered(self) -> None:
        store = self.open_store()
        with store.transaction():
            store.upsert(event(1))
        chunk = SlackChunk({"text": "one"}, ["github:release:1"])
        self.assertFalse(deliver(store, [chunk], FakeTransport(SlackResult(429, retry_after=120))))
        row = store.db.execute(
            "SELECT notification_state, next_attempt_at, notified_at FROM notification_outbox WHERE event_id=?",
            ("github:release:1",),
        ).fetchone()
        self.assertEqual(row[0], "DELIVERY_FAILED")
        self.assertIsNotNone(row[1])
        self.assertIsNone(row[2])
        self.assertEqual(store.pending(), [])  # Not retryable until Retry-After elapses.

    def test_transport_timeout_remains_retryable_and_never_delivered(self) -> None:
        store = self.open_store()
        with store.transaction():
            store.upsert(event(1))
        chunk = SlackChunk({"text": "one"}, ["github:release:1"])
        self.assertFalse(deliver(store, [chunk], FakeTransport(TimeoutError("simulated timeout"))))
        self.assertEqual(select(store.pending(), POLICY).reason, "retry_failed_delivery")
        retry = FakeTransport(SlackResult(200))
        self.assertTrue(deliver(store, [chunk], retry))
        self.assertEqual(store.pending(), [])

    def test_preview_store_does_not_create_database_or_mutate_existing_bytes(self) -> None:
        preview = self.open_store(preview=True)
        with preview.transaction():
            preview.upsert(event(1))
        preview.close()
        self.assertFalse(self.db_path.exists())

        committed = self.open_store()
        with committed.transaction():
            committed.upsert(event(2))
        committed.close()
        before = self.db_path.read_bytes()
        preview = self.open_store(preview=True)
        with preview.transaction():
            preview.upsert(event(3))
        preview.close()
        self.assertEqual(self.db_path.read_bytes(), before)
        reopened = self.open_store()
        self.assertEqual(reopened.event_ids(), {"github:release:2"})

    def test_malformed_database_fails_closed_without_replacement(self) -> None:
        self.db_path.write_bytes(b"this is not a SQLite database")
        before = self.db_path.read_bytes()
        for preview in (False, True):
            with self.assertRaises(ValueError):
                EventStore.open(self.db_path, preview=preview)
        self.assertEqual(self.db_path.read_bytes(), before)

    def test_version_one_database_with_missing_tables_fails_closed(self) -> None:
        with sqlite3.connect(self.db_path) as db:
            db.execute("PRAGMA user_version=1")
            db.execute("CREATE TABLE unrelated(id INTEGER)")
        before = self.db_path.read_bytes()
        for preview in (False, True):
            with self.assertRaises(ValueError):
                EventStore.open(self.db_path, preview=preview)
        self.assertEqual(self.db_path.read_bytes(), before)

    def test_reordered_duplicate_and_backdated_page_two_release_are_seen_once(self) -> None:
        fixture = self.root / "pages.json"
        fixture.write_text(json.dumps({"owner/repo": {"pages": [
            [release(3), release(2), release(2)],
            [release(1, published="2020-01-01T00:00:00Z"), release(3)],
        ]}}), encoding="utf-8")
        source = FixtureReleaseSource(fixture)
        seen = {"github:release:2"}
        collected = collect_releases(["owner/repo"], source, seen_event_ids=seen)
        self.assertEqual([item.event_id for item in collected.events], ["github:release:3", "github:release:1"])

    def test_cli_preview_fixture_is_token_free_and_state_free(self) -> None:
        repos, fixture, config, feed = self.fixture_paths({"owner/repo": [release(1)]})
        result = self.cli(
            "--repos-file", repos, "--fixture-releases", fixture, "--state-db", self.db_path,
            "--cache-path", self.root / "legacy.json", "--config", config, "--feed-path", feed,
            "--github-output", self.root / "output.txt", "--mode", "preview", "--no-sleep",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.db_path.exists())
        self.assertFalse((self.root / "legacy.json").exists())
        self.assertFalse((self.root / "last_notification.txt").exists())
        report = json.loads(feed.read_text(encoding="utf-8"))
        self.assertEqual(report["scanned_repos"], 1)
        self.assertFalse(report["notify"])  # First-run baseline is fail-safe.

    def test_cli_preview_preserves_existing_db_legacy_and_last_notification(self) -> None:
        repos, fixture, config, feed = self.fixture_paths({"owner/repo": [release(1), release(2)]})
        store = self.open_store()
        with store.transaction():
            store.upsert(event(1))
            store.set_meta("repo_initialized:owner/repo", "2026-10-01T00:00:00Z")
            store.set_meta("legacy_migrated", "legacy_cache")
        store.close()
        legacy = self.root / "legacy.json"
        legacy.write_text(json.dumps({"owner/repo": {"tag": "v1", "published": "2026-10-01T00:00:00Z"}}), encoding="utf-8")
        last = self.root / "last_notification.txt"
        last.write_text("2026-09-30T00:00:00Z", encoding="utf-8")
        before = {path: path.read_bytes() for path in (self.db_path, legacy, last)}

        result = self.cli(
            "--repos-file", repos, "--fixture-releases", fixture, "--state-db", self.db_path,
            "--cache-path", legacy, "--config", config, "--feed-path", feed,
            "--mode", "preview", "--no-sleep",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        self.assertEqual(json.loads(feed.read_text(encoding="utf-8"))["release_count"], 1)

    def test_legacy_fixture_cli_arguments_remain_available(self) -> None:
        repos, fixture, config, feed = self.fixture_paths({"owner/repo": release(1)})
        result = self.cli(
            "--repos-file", repos, "--fixture-releases", fixture,
            "--cache-path", self.root / "legacy.json", "--config", config,
            "--feed-path", feed, "--github-output", self.root / "output.txt", "--no-sleep",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(feed.exists())
        self.assertTrue((self.root / "output.txt").exists())

    def test_cli_malformed_legacy_cache_fails_without_creating_state(self) -> None:
        repos, fixture, config, feed = self.fixture_paths({"owner/repo": [release(1)]})
        legacy = self.root / "legacy.json"
        legacy.write_text("{broken", encoding="utf-8")
        result = self.cli(
            "--repos-file", repos, "--fixture-releases", fixture, "--state-db", self.db_path,
            "--cache-path", legacy, "--config", config, "--feed-path", feed,
            "--mode", "commit", "--no-sleep",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(legacy.read_text(encoding="utf-8"), "{broken")
        self.assertFalse(self.db_path.exists())

    def test_cli_fixture_cannot_construct_real_slack_transport(self) -> None:
        repos, fixture, config, feed = self.fixture_paths({"owner/repo": [release(1)]})
        env = os.environ.copy()
        env["SLACK_WEBHOOK_URL"] = "https://example.invalid/not-a-webhook"
        result = self.cli(
            "--repos-file", repos, "--fixture-releases", fixture, "--state-db", self.db_path,
            "--cache-path", self.root / "legacy.json", "--config", config, "--feed-path", feed,
            "--mode", "commit", "--send-slack", "--no-sleep", env=env,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ValueError", result.stderr)
        self.assertFalse(self.db_path.exists())
        self.assertFalse(feed.exists())

        # CLI redacts errors; separately prove construction never reaches the
        # real transport boundary even when the environment contains a URL.
        spec = importlib.util.spec_from_file_location("acceptance_check_release", SCRIPT)
        script = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[spec.name] = script
        spec.loader.exec_module(script)
        args = script.build_arg_parser().parse_args([
            "--repos-file", str(repos), "--fixture-releases", str(fixture),
            "--state-db", str(self.db_path), "--cache-path", str(self.root / "legacy.json"),
            "--config", str(config), "--feed-path", str(feed), "--mode", "commit", "--send-slack",
        ])
        with mock.patch.dict(os.environ, {"SLACK_WEBHOOK_URL": "https://example.invalid/not-a-webhook"}):
            with mock.patch.object(script, "WebhookTransport") as real_transport:
                with self.assertRaises(ValueError):
                    script.run(args)
                real_transport.assert_not_called()

    def test_preview_rejects_feed_and_output_aliases_of_protected_state(self) -> None:
        repos, fixture, config, feed = self.fixture_paths({"owner/repo": [release(1)]})
        store = self.open_store()
        store.close()
        legacy = self.root / "legacy.json"
        legacy.write_text("{}", encoding="utf-8")
        last = self.root / "last_notification.txt"
        last.write_text("2026-10-01", encoding="utf-8")
        symlink = self.root / "db-link"
        symlink.symlink_to(self.db_path)
        hardlink = self.root / "cache-link"
        os.link(legacy, hardlink)
        protected = (self.db_path, legacy, last)
        before = {path: path.read_bytes() for path in protected}
        for flag, alias in (
            ("--feed-path", self.db_path),
            ("--github-output", legacy),
            ("--feed-path", last),
            ("--feed-path", symlink),
            ("--github-output", hardlink),
        ):
            with self.subTest(flag=flag, alias=alias.name):
                result = self.cli(
                    "--repos-file", repos, "--fixture-releases", fixture,
                    "--state-db", self.db_path, "--cache-path", legacy,
                    "--config", config, "--feed-path", feed,
                    flag, alias, "--mode", "preview", "--no-sleep",
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("ValueError", result.stderr)
                self.assertEqual({path: path.read_bytes() for path in protected}, before)


if __name__ == "__main__":
    unittest.main()
