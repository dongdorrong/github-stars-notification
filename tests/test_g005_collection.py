"""Bounded, page-aware collection regressions; no live GitHub or Slack calls."""
from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from starwatch.event_store import EventStore
from starwatch.pipeline import run_pipeline
from starwatch.release_collector import (
    FixtureReleaseSource, FixtureSourceError, ReleasePage, collect_releases,
)

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github/scripts/check_release.py"
spec = importlib.util.spec_from_file_location("g005_check_release", SCRIPT)
script = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = script
spec.loader.exec_module(script)


def release(number: int, **extra: object) -> dict:
    return {"id": number, "tag_name": f"v{number}",
            "published_at": "2026-10-01T00:00:00Z", **extra}


class PageSource:
    def __init__(self, pages: dict[str, list[list[dict]]], *, clock=None, seconds_per_page=0):
        self.pages = pages
        self.calls: list[tuple[str, int, int]] = []
        self.clock = clock
        self.seconds_per_page = seconds_per_page

    def fetch_page(self, repository: str, page_number: int, per_page: int) -> ReleasePage:
        self.calls.append((repository, page_number, per_page))
        if self.clock is not None:
            self.clock.elapsed += self.seconds_per_page
        pages = self.pages[repository]
        items = pages[page_number - 1] if page_number <= len(pages) else []
        return ReleasePage(tuple(items), page_number, page_number < len(pages))


class FakeClock:
    def __init__(self):
        self.elapsed = 0.0

    def __call__(self) -> float:
        return self.elapsed


class G005CollectorTests(unittest.TestCase):
    def test_unseen_page_one_then_known_only_page_two_stops(self):
        source = PageSource({"owner/repo": [[release(4), release(3)],
                                           [release(2), release(1)], [release(99)]]})
        result = collect_releases(["owner/repo"], source,
                                  seen_event_ids={"github:release:1", "github:release:2"},
                                  initialized_repos={"owner/repo"},
                                  config={"known_only_pages_to_stop": 1})
        self.assertEqual([item.release_id for item in result.events], [4, 3, 2, 1])
        self.assertEqual([page for _, page, _ in source.calls], [1, 2])

    def test_three_unseen_events_across_two_pages_are_all_collected(self):
        source = PageSource({"owner/repo": [[release(3), release(2)], [release(1)]]})
        result = collect_releases(["owner/repo"], source,
                                  initialized_repos={"owner/repo"})
        self.assertEqual([item.release_id for item in result.events], [3, 2, 1])
        self.assertEqual(result.pages_fetched, 2)

    def test_page_four_is_not_marked_seen_by_three_page_fast_path(self):
        source = PageSource({"owner/repo": [[release(1)], [release(2)],
                                           [release(3)], [release(4)]]})
        result = collect_releases(["owner/repo"], source,
                                  initialized_repos={"owner/repo"},
                                  config={"max_incremental_pages_per_repo": 3,
                                          "known_only_pages_to_stop": 4})
        self.assertNotIn("github:release:4", {item.event_id for item in result.events})
        self.assertEqual([page for _, page, _ in source.calls], [1, 2, 3])
        self.assertEqual(result.repositories_deferred, 0)  # page cap is not a failed scan

    def test_duplicate_and_reordered_ids_are_idempotent(self):
        source = PageSource({"owner/repo": [[release(3), release(2)],
                                           [release(2), release(1)], [release(3)]]})
        result = collect_releases(["owner/repo"], source,
                                  initialized_repos={"owner/repo"},
                                  config={"max_incremental_pages_per_repo": 3,
                                          "known_only_pages_to_stop": 3})
        self.assertEqual({item.event_id for item in result.events},
                         {"github:release:1", "github:release:2", "github:release:3"})
        self.assertEqual(len(result.events), 3)

    def test_special_project_receives_larger_fast_path_cap(self):
        pages = [[release(n)] for n in range(1, 6)]
        normal = PageSource({"owner/repo": pages})
        special = PageSource({"owner/repo": pages})
        settings = {"max_incremental_pages_per_repo": 3,
                    "max_incremental_pages_special_project": 5,
                    "known_only_pages_to_stop": 6}
        normal_result = collect_releases(["owner/repo"], normal,
                                         initialized_repos={"owner/repo"}, config=settings)
        special_result = collect_releases(["owner/repo"], special,
                                          initialized_repos={"owner/repo"},
                                          special_projects={"owner/repo"}, config=settings)
        self.assertEqual(len(normal_result.events), 3)
        self.assertEqual(len(special_result.events), 5)
        self.assertEqual(special_result.pages_fetched, 5)

    def test_bootstrap_thousand_releases_reads_only_recent_configured_pages(self):
        pages = [[release(n) for n in range(start, start + 100)]
                 for start in range(1, 1001, 100)]
        source = PageSource({"owner/repo": pages})
        result = collect_releases(["owner/repo"], source,
                                  config={"bootstrap_pages": 2, "per_page": 100})
        self.assertEqual([page for _, page, _ in source.calls], [1, 2])
        self.assertEqual(len(result.events), 200)
        self.assertEqual(result.pages_fetched, 2)
        self.assertEqual(result.repositories_completed, 1)

    def test_fixture_source_exposes_page_contract_without_iterating_full_history(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "fixture.json"
            path.write_text(json.dumps({"owner/repo": {"pages": [
                [release(1)], [release(2)], [release(3)]]}}))
            source = FixtureReleaseSource(path)
            first = source.fetch_page("owner/repo", 1, 100)
            self.assertEqual([item["id"] for item in first.releases], [1])
            self.assertTrue(first.has_next)
            self.assertFalse(source.fetch_page("owner/repo", 3, 100).has_next)

    def test_global_budget_preserves_completed_repo_and_defers_rest(self):
        clock = FakeClock()
        repos = [f"owner/repo{n}" for n in range(4)]
        source = PageSource({repo: [[release(index)]] for index, repo in enumerate(repos, 1)},
                            clock=clock, seconds_per_page=4)
        result = collect_releases(repos, source,
                                  config={"global_budget_seconds": 7}, clock=clock)
        self.assertTrue(result.collection_budget_exhausted)
        self.assertLess(result.repositories_started, result.repositories_total)
        self.assertGreaterEqual(result.repositories_deferred, 1)
        self.assertIn(1, [item.release_id for item in result.events])
        self.assertLess(result.elapsed_seconds, 30 * 60)

    def test_per_repo_budget_defers_slow_repo_but_scans_next(self):
        clock = FakeClock()
        source = PageSource({"owner/slow": [[release(1)], [release(2)], [release(3)]],
                             "owner/good": [[release(4)]]}, clock=clock, seconds_per_page=3)
        result = collect_releases(["owner/slow", "owner/good"], source,
                                  initialized_repos={"owner/slow", "owner/good"},
                                  config={"per_repo_budget_seconds": 5,
                                          "global_budget_seconds": 30,
                                          "known_only_pages_to_stop": 4}, clock=clock)
        self.assertIn("github:release:4", {item.event_id for item in result.events})
        self.assertGreaterEqual(result.repositories_deferred, 1)
        self.assertFalse(result.collection_budget_exhausted)

    def test_progress_uses_ordinal_and_nonreversible_reference(self):
        private = "private-owner/private-repository"
        source = PageSource({private: [[release(1)]]})
        progress = []
        collect_releases([private], source, progress=progress.append)
        self.assertTrue(progress)
        serialized = json.dumps(progress)
        self.assertNotIn(private, serialized)
        self.assertNotIn("private-owner", serialized)
        self.assertIn("ordinal", serialized)

    def test_rotating_start_index_gives_deferred_repos_first_turn_next_run(self):
        repos = [f"owner/repo{n}" for n in range(4)]
        first_clock = FakeClock()
        first = collect_releases(repos, PageSource({repo: [[release(i + 1)]]
            for i, repo in enumerate(repos)}, clock=first_clock, seconds_per_page=4),
            config={"global_budget_seconds": 7}, clock=first_clock, start_index=0)
        next_clock = FakeClock()
        next_source = PageSource({repo: [[release(i + 1)]]
            for i, repo in enumerate(repos)}, clock=next_clock, seconds_per_page=4)
        collect_releases(repos, next_source, config={"global_budget_seconds": 7},
                         clock=next_clock, start_index=first.next_start_index)
        self.assertTrue(next_source.calls)
        self.assertEqual(next_source.calls[0][0], repos[first.next_start_index])
        self.assertNotEqual(next_source.calls[0][0], repos[0])

    def test_failed_second_fixture_page_reports_safe_status_and_keeps_first_page(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "fixture.json"
            secret = "PRIVATE_BODY_TOKEN"
            path.write_text(json.dumps({"owner/repo": {"pages": [
                [release(1)], {"error": {"status": 500, "message": secret}}]}}))
            result = collect_releases(["owner/repo"], FixtureReleaseSource(path),
                                      initialized_repos={"owner/repo"},
                                      config={"known_only_pages_to_stop": 2})
            self.assertEqual([event.event_id for event in result.events], ["github:release:1"])
            self.assertEqual([error.message for error in result.errors], ["http_500"])
            self.assertNotIn(secret, repr(result.errors))

    def test_reconciliation_cap_of_one_is_rejected_to_preserve_overlap_progress(self):
        source = PageSource({"owner/repo": [[release(1)]]})
        with self.assertRaises(ValueError):
            collect_releases(["owner/repo"], source,
                             config={"reconciliation_pages_per_repo": 1})

    def test_deep_reconciliation_repository_cap_reports_deferred_work(self):
        source = PageSource({
            "owner/one": [[release(1)], [release(2)]],
            "owner/two": [[release(3)], [release(4)]],
        })
        result = collect_releases(["owner/one", "owner/two"], source,
                                  initialized_repos={"owner/one", "owner/two"},
                                  seen_event_ids={"github:release:1", "github:release:3"},
                                  config={"reconciliation_shards": 1,
                                          "max_reconciliation_repositories_per_run": 1})
        self.assertEqual(result.reconciliation_deferred, 1)
        self.assertEqual(result.repositories_completed, 2)
        self.assertEqual(result.repositories_deferred, 0)

    def test_budget_exhausted_is_reported_even_when_last_repo_uses_remainder(self):
        clock = FakeClock()
        source = PageSource({"owner/one": [[release(1)]],
                             "owner/two": [[release(2)]]},
                            clock=clock, seconds_per_page=4)
        result = collect_releases(["owner/one", "owner/two"], source,
                                  config={"global_budget_seconds": 7}, clock=clock)
        self.assertTrue(result.collection_budget_exhausted)
        self.assertEqual(result.repositories_completed, 2)
        self.assertEqual(result.repositories_deferred, 0)


class G005PipelineTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.db = self.root / "events.sqlite3"
        self.legacy = self.root / "legacy.json"
        self.config = {
            "special_projects": [],
            "notification": {"min_release_count": 1000, "first_run_notify": False,
                             "special_project_always_notify": False},
            "collector": {"per_page": 1, "bootstrap_pages": 1,
                          "max_incremental_pages_per_repo": 1,
                          "known_only_pages_to_stop": 1,
                          "reconciliation_pages_per_repo": 2,
                          "reconciliation_shards": 1,
                          "special_reconciliation_shards": 1},
        }

    def scan(self, source, *, mode="commit", clock=None, send_slack=False, transport=None):
        kwargs = {"state_path": self.db, "legacy_path": self.legacy,
                  "repos": ["owner/repo"], "source": source,
                  "config": self.config, "mode": mode,
                  "send_slack": send_slack, "transport": transport}
        if clock is not None:
            kwargs["clock"] = clock
        return run_pipeline(**kwargs)

    def test_bootstrap_suppresses_recent_page_and_initializes_repo(self):
        source = PageSource({"owner/repo": [[release(1)], [release(2)]]})
        first = self.scan(source)
        self.assertEqual(first.collected.pages_fetched, 1)
        self.assertEqual(first.pending_after_delivery_count, 0)
        self.assertEqual(first.chunks, [])
        store = EventStore.open(self.db, preview=True)
        try:
            self.assertIsNotNone(store.get_meta("repo_initialized:owner/repo"))
            self.assertEqual(store.event_ids(), {"github:release:1"})
        finally:
            store.close()

    def test_reconciliation_eventually_discovers_backdated_page_four(self):
        pages = [[release(1)], [release(2)], [release(3)],
                 [release(4, published_at="2020-01-01T00:00:00Z")]]
        self.scan(PageSource({"owner/repo": pages}))  # baseline page one only
        discoveries = []
        for _ in range(4):
            result = self.scan(PageSource({"owner/repo": pages}))
            discoveries.extend(event["event_id"] for event in result.new_events)
        self.assertIn("github:release:4", discoveries)
        self.assertEqual(discoveries.count("github:release:4"), 1)

    def test_reconciliation_overlap_revisits_shifted_boundary(self):
        initial = [[release(1)], [release(2)], [release(3)], [release(4)]]
        self.scan(PageSource({"owner/repo": initial}))
        self.scan(PageSource({"owner/repo": initial}))  # advance deep cursor with overlap
        shifted = [[release(1)], [release(9)], [release(2)], [release(3)], [release(4)]]
        result = self.scan(PageSource({"owner/repo": shifted}))
        self.assertIn("github:release:9", {event["event_id"] for event in result.new_events})

    def test_failed_reconciliation_page_does_not_advance_cursor(self):
        class FailingPage(PageSource):
            def fetch_page(self, repository, page_number, per_page):
                if page_number == 2:
                    raise ConnectionError("do not log secret response body")
                return super().fetch_page(repository, page_number, per_page)

        pages = [[release(1)], [release(2)], [release(3)]]
        self.scan(PageSource({"owner/repo": pages}))
        failed = self.scan(FailingPage({"owner/repo": pages}))
        self.assertTrue(failed.collected.errors)
        store = EventStore.open(self.db, preview=True)
        try:
            self.assertIn(store.get_meta("reconciliation_cursor:owner/repo"), (None, "1"))
        finally:
            store.close()
        retry = self.scan(PageSource({"owner/repo": pages}))
        self.assertIn("github:release:2", {event["event_id"] for event in retry.new_events})

    def test_known_release_can_refresh_draft_to_published(self):
        self.scan(PageSource({"owner/repo": [[release(1, draft=True)]]}))
        result = self.scan(PageSource({"owner/repo": [[release(1, draft=False)]]}))
        self.assertEqual(result.new_events, [])
        store = EventStore.open(self.db, preview=True)
        try:
            pending = store.pending()
            self.assertEqual([item["event_id"] for item in pending], ["github:release:1"])
            self.assertFalse(pending[0]["draft"])
        finally:
            store.close()

    def test_deep_history_after_bootstrap_is_not_bulk_notified(self):
        # Existing history normally has lower IDs behind the newest first page.
        pages = [[release(3)], [release(2)], [release(1)]]
        self.scan(PageSource({"owner/repo": pages}))
        for _ in range(3):
            self.scan(PageSource({"owner/repo": pages}))
        store = EventStore.open(self.db, preview=True)
        try:
            self.assertEqual(store.event_ids(), {"github:release:1", "github:release:2",
                                                 "github:release:3"})
            self.assertEqual(store.pending(), [])
        finally:
            store.close()

    def test_malformed_reconciliation_cursor_fails_closed(self):
        self.scan(PageSource({"owner/repo": [[release(1)]]}))
        for invalid in ("not-an-integer", "0", "-1"):
            with self.subTest(value=invalid):
                store = EventStore.open(self.db)
                try:
                    with store.transaction():
                        store.set_meta("reconciliation_cursor:owner/repo", invalid)
                finally:
                    store.close()
                before = self.db.read_bytes()
                source = PageSource({"owner/repo": [[release(1)]]})
                with self.assertRaises(ValueError):
                    self.scan(source)
                self.assertEqual(source.calls, [])
                self.assertEqual(self.db.read_bytes(), before)

    def test_legacy_boundary_remains_read_only_with_bounded_scan(self):
        self.legacy.write_text(json.dumps({"owner/repo": {"tag": "v1",
            "published": "2026-10-01T00:00:00Z"}}))
        before = self.legacy.read_bytes()
        result = self.scan(PageSource({"owner/repo": [[release(1)], [release(2)]]}))
        self.assertTrue(result.baseline_created)
        self.assertEqual(self.legacy.read_bytes(), before)

    def test_legacy_cutover_date_survives_cache_disappearance_for_deep_pages(self):
        self.config["notification"]["cutover_pending_policy"] = "preserve_pending"
        cutoff = "2026-10-01T00:00:00Z"
        self.legacy.write_text(json.dumps({"owner/repo": {"tag": "v90",
            "published": cutoff}}))
        pages = [[release(100, published_at="2026-10-03T00:00:00Z")],
                 [release(99, published_at="2026-10-02T00:00:00Z")],
                 [release(50, published_at=cutoff)]]
        self.scan(PageSource({"owner/repo": pages}))
        self.legacy.unlink()  # durable boundary must not depend on cache restore
        for _ in range(3):
            self.scan(PageSource({"owner/repo": pages}))
        store = EventStore.open(self.db, preview=True)
        try:
            self.assertEqual(store.event_ids(), {"github:release:100", "github:release:99",
                                                 "github:release:50"})
            self.assertEqual({item["event_id"] for item in store.pending()},
                             {"github:release:100", "github:release:99"})
        finally:
            store.close()

    def test_delayed_repo_bootstrap_preserves_legacy_boundary_after_global_migration(self):
        self.config["notification"]["cutover_pending_policy"] = "preserve_pending"
        cutoff = "2026-10-01T00:00:00Z"
        self.legacy.write_text(json.dumps({"owner/repo": {"tag": "v90",
            "published": cutoff}}))

        class FirstScanSource(PageSource):
            def fetch_page(self, repository, page_number, per_page):
                if repository == "owner/repo":
                    raise FixtureSourceError(404, "private response details")
                return super().fetch_page(repository, page_number, per_page)

        first = run_pipeline(
            state_path=self.db, legacy_path=self.legacy,
            repos=["owner/repo", "good/repo"],
            source=FirstScanSource({"good/repo": [[]]}),
            config=self.config, mode="commit",
        )
        self.assertEqual([error.status for error in first.collected.errors], [404])
        self.assertEqual(first.collected.repositories_completed, 1)
        self.legacy.unlink()  # cache can disappear before this repo first succeeds
        pages = [[release(100, published_at="2026-10-03T00:00:00Z")],
                 [release(99, published_at="2026-10-02T00:00:00Z")],
                 [release(50, published_at=cutoff)]]
        self.scan(PageSource({"owner/repo": pages}))  # first successful repo bootstrap
        for _ in range(2):
            self.scan(PageSource({"owner/repo": pages}))  # deep pages after cache disappears
        store = EventStore.open(self.db, preview=True)
        try:
            self.assertEqual(store.get_meta("legacy_cutover_published_at:owner/repo"), cutoff)
            self.assertEqual(store.event_ids(), {"github:release:100", "github:release:99",
                                                 "github:release:50"})
            self.assertEqual({item["event_id"] for item in store.pending()},
                             {"github:release:100", "github:release:99"})
        finally:
            store.close()

    def test_explicit_first_run_notify_keeps_later_history_eligible(self):
        self.config["notification"]["first_run_notify"] = True
        pages = [[release(100)], [release(50)]]
        self.scan(PageSource({"owner/repo": pages}))
        self.scan(PageSource({"owner/repo": pages}))
        store = EventStore.open(self.db, preview=True)
        try:
            self.assertEqual({item["event_id"] for item in store.pending()},
                             {"github:release:100", "github:release:50"})
        finally:
            store.close()

    def test_malformed_bootstrap_highwater_rolls_back_without_mutating_state(self):
        self.scan(PageSource({"owner/repo": [[release(100)]]}))
        for invalid in ("not-a-number", "0", "-1"):
            with self.subTest(value=invalid):
                store = EventStore.open(self.db)
                try:
                    with store.transaction():
                        store.set_meta("bootstrap_max_release_id:owner/repo", invalid)
                finally:
                    store.close()
                before = self.db.read_bytes()
                source = PageSource({"owner/repo": [[release(99)]]})
                with self.assertRaises(ValueError):
                    self.scan(source)
                self.assertEqual(source.calls, [])
                self.assertEqual(self.db.read_bytes(), before)


class G005CliTelemetryTests(unittest.TestCase):
    def test_budget_deferred_status_reaches_feed_and_github_outputs(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repos = root / "repos.txt"
            repos.write_text("owner/one\nowner/two\nowner/three\n")
            fixture = root / "fixture.json"
            fixture.write_text(json.dumps({
                "owner/one": [release(1)], "owner/two": [release(2)],
                "owner/three": [release(3)],
            }))
            config = root / "config.yaml"
            config.write_text("collector:\n  global_budget_seconds: 7\n")
            feed_path = root / "feed.json"
            output_path = root / "output.txt"
            args = script.build_arg_parser().parse_args([
                "--repos-file", str(repos), "--fixture-releases", str(fixture),
                "--state-db", str(root / "state.sqlite3"),
                "--cache-path", str(root / "legacy.json"),
                "--config", str(config), "--feed-path", str(feed_path),
                "--github-output", str(output_path), "--mode", "preview",
            ])
            clock = FakeClock()
            real_run_pipeline = run_pipeline

            def budgeted_pipeline(**kwargs):
                source = kwargs["source"]
                original_fetch = source.fetch_page

                def delayed_fetch(repository, page_number, per_page):
                    clock.elapsed += 4
                    return original_fetch(repository, page_number, per_page)

                source.fetch_page = delayed_fetch
                return real_run_pipeline(**kwargs, clock=clock)

            with mock.patch.object(script, "run_pipeline", side_effect=budgeted_pipeline):
                code = script.run(args)
            self.assertEqual(code, 0)
            feed = json.loads(feed_path.read_text())
            self.assertTrue(feed["collection_budget_exhausted"])
            self.assertGreaterEqual(feed["repositories_deferred"], 1)
            self.assertEqual(feed["repositories_completed"], 2)
            self.assertEqual(feed["collection_success_count"], 2)
            self.assertFalse((root / "state.sqlite3").exists())  # preview is immutable
            outputs = output_path.read_text()
            self.assertIn("collection_budget_exhausted=true", outputs)
            self.assertIn("repositories_deferred=1", outputs)

    def test_feed_and_outputs_report_bounded_progress_without_private_log_names(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            private = "private-owner/private-repository"
            marker = "SECRET_FIXTURE_RESPONSE_BODY"
            repos = root / "repos.txt"
            repos.write_text(private + "\nbad/repo\n", encoding="utf-8")
            fixture = root / "releases.json"
            fixture.write_text(json.dumps({private: [release(1)],
                "bad/repo": {"error": {"status": 404, "message": marker}}}))
            feed_path = root / "feed.json"
            output_path = root / "output.txt"
            args = script.build_arg_parser().parse_args([
                "--repos-file", str(repos), "--fixture-releases", str(fixture),
                "--state-db", str(root / "state.sqlite3"),
                "--cache-path", str(root / "legacy.json"),
                "--config", str(root / "missing.yaml"),
                "--feed-path", str(feed_path), "--github-output", str(output_path),
                "--mode", "preview",
            ])
            public_log = io.StringIO()
            with redirect_stdout(public_log):
                code = script.run(args)
            self.assertEqual(code, 0)
            self.assertNotIn(private, public_log.getvalue())
            self.assertNotIn(marker, public_log.getvalue())
            feed = json.loads(feed_path.read_text())
            self.assertEqual(feed["repositories_total"], 2)
            self.assertEqual(feed["repositories_started"], 2)
            self.assertEqual(feed["repositories_completed"], 1)
            self.assertEqual(feed["collection_error_count"], 1)
            self.assertEqual(feed["collector_errors_by_type"], {"http_404": 1})
            self.assertGreaterEqual(feed["pages_fetched"], 1)
            self.assertEqual(feed["releases_observed"], 1)
            self.assertFalse(feed["collection_budget_exhausted"])
            outputs = output_path.read_text()
            for key in ("repositories_total=2", "repositories_started=2",
                        "repositories_completed=1", "collection_error_count=1",
                        "collection_budget_exhausted=false"):
                self.assertIn(key, outputs)
            self.assertNotIn(private, outputs)
            self.assertNotIn(marker, outputs)

    def test_workflow_step_summary_includes_deferred_and_budget_metrics(self):
        workflow = (ROOT / ".github/workflows/notify-starred-releases.yml").read_text()
        self.assertIn("repositories_deferred", workflow)
        self.assertIn("collection_budget_exhausted", workflow)
        self.assertIn("pages_fetched", workflow)

if __name__ == "__main__":
    unittest.main()
