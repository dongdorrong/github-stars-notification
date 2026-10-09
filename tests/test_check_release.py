from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github" / "scripts" / "check_release.py"
spec = importlib.util.spec_from_file_location("check_release", SCRIPT)
check_release = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = check_release
spec.loader.exec_module(check_release)


class CheckReleaseTest(unittest.TestCase):
    def test_normalize_repo_name_accepts_human_spacing(self) -> None:
        self.assertEqual(check_release.normalize_repo_name(" kubernetes / kubernetes "), "kubernetes/kubernetes")
        self.assertEqual(check_release.normalize_repo_name("grafana/grafana"), "grafana/grafana")
        self.assertEqual(check_release.normalize_repo_name("aws /amazon-vpc-cni-k8s"), "aws/amazon-vpc-cni-k8s")

    def test_detect_releases_uses_cache_for_duplicate_prevention(self) -> None:
        repos = ["owner/repo"]
        release = {
            "tag_name": "v1.0.0",
            "name": "Release v1.0.0",
            "published_at": "2026-06-20 10:00:00",
            "html_url": "https://github.com/owner/repo/releases/tag/v1.0.0",
        }

        first = check_release.detect_releases(
            repos,
            lambda repo: release,
            previous_cache={},
            special_projects=set(),
            first_run=True,
            sleep_seconds=0,
        )
        self.assertEqual(len(first.releases), 1)

        second = check_release.detect_releases(
            repos,
            lambda repo: release,
            previous_cache=first.current_cache,
            special_projects=set(),
            first_run=False,
            sleep_seconds=0,
        )
        self.assertEqual(second.releases, [])

    def test_legacy_below_threshold_event_is_cached_before_delivery(self) -> None:
        """Characterize the loss mode fixed by the durable outbox."""
        config = check_release.normalize_config({"notification": {"min_release_count": 5}})
        raw = {"tag_name": "v1", "published_at": "2026-06-20 10:00:00"}
        first = check_release.detect_releases(
            ["owner/repo"], lambda _: raw, {}, set(), True, 0
        )
        self.assertFalse(check_release.decide_notification(first.releases, True, config).should_notify)
        self.assertEqual(first.current_cache["owner/repo"]["tag"], "v1")
        second = check_release.detect_releases(
            ["owner/repo"], lambda _: raw, first.current_cache, set(), False, 0
        )
        self.assertEqual(second.releases, [])

    def test_first_run_default_is_fail_safe(self) -> None:
        """The characterization commit captured the old notify-on-bootstrap default."""
        config = check_release.normalize_config({})
        releases = [
            check_release.Release(f"owner/repo{i}", "v1", "", "2026-06-20", "")
            for i in range(5)
        ]
        self.assertFalse(config["notification"]["first_run_notify"])
        self.assertFalse(check_release.decide_notification(releases, True, config).should_notify)

    def test_policy_notifies_special_project_below_threshold(self) -> None:
        config = check_release.normalize_config(
            {
                "special_projects": ["grafana/grafana"],
                "notification": {
                    "min_release_count": 5,
                    "special_project_always_notify": True,
                },
            }
        )
        release = check_release.Release(
            repo="grafana/grafana",
            tag="v12.0.0",
            name="v12.0.0",
            published="2026-06-20 10:00:00",
            html_url="https://github.com/grafana/grafana/releases/tag/v12.0.0",
            is_special=True,
        )
        decision = check_release.decide_notification([release], first_run=False, config=config)
        self.assertTrue(decision.should_notify)
        self.assertEqual(decision.reason, "special_project_release")

    def test_run_writes_feed_and_actions_outputs_with_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            repos_file = tmp / "repos.txt"
            repos_file.write_text("grafana / grafana\nother/repo\n", encoding="utf-8")
            fixture_file = tmp / "fixture.json"
            fixture_file.write_text(
                json.dumps(
                    {
                        "grafana/grafana": {
                            "tag_name": "v12.0.0",
                            "name": "Release v12.0.0",
                            "published_at": "2026-06-20 10:00:00",
                            "html_url": "https://github.com/grafana/grafana/releases/tag/v12.0.0",
                        },
                        "other/repo": {
                            "tag_name": "v1.2.3",
                            "name": "v1.2.3",
                            "published_at": "2026-06-20 09:00:00",
                            "html_url": "https://github.com/other/repo/releases/tag/v1.2.3",
                        },
                    }
                ),
                encoding="utf-8",
            )
            config_file = tmp / "config.yaml"
            config_file.write_text(
                """
special_projects:
  - grafana / grafana
notification:
  min_release_count: 5
  special_project_always_notify: true
  first_run_notify: true
feed:
  output_path: ignored-by-arg.json
""".strip()
                + "\n",
                encoding="utf-8",
            )
            feed_path = tmp / "feed.json"
            output_path = tmp / "github-output.txt"

            exit_code = check_release.run(
                Namespace(
                    repos_file=repos_file,
                    cache_path=tmp / "cache.json",
                    config=config_file,
                    feed_path=feed_path,
                    github_output=output_path,
                    fixture_releases=fixture_file,
                    sleep_seconds=0,
                    no_sleep=True,
                )
            )

            self.assertEqual(exit_code, 0)
            feed = json.loads(feed_path.read_text(encoding="utf-8"))
            self.assertEqual(feed["schema_version"], "github-stars-release-feed/v1")
            self.assertEqual(feed["release_count"], 2)
            self.assertEqual(feed["special_release_count"], 1)
            self.assertTrue(feed["notify"])
            self.assertIn("decide_new_vs_duplicate", feed["llm_contract"]["must_not_do"])

            output = output_path.read_text(encoding="utf-8")
            self.assertIn("has_new=true", output)
            self.assertIn("notify_reason=special_project_release", output)
            self.assertIn(f"feed_path={feed_path}", output)

    def test_fixture_errors_are_reported_by_safe_type_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            repos = tmp / "repos.txt"
            repos.write_text("bad/missing\nbad/server\nowner/good\n", encoding="utf-8")
            fixture = tmp / "fixture.json"
            fixture.write_text(json.dumps({
                "bad/missing": {"error": {"status": 404, "message": "private-token-marker"}},
                "bad/server": {"error": {"status": 500, "message": "private-token-marker"}},
                "owner/good": {"id": 1, "tag_name": "v1", "published_at": "2026-10-02T00:00:00Z"},
            }), encoding="utf-8")
            feed = tmp / "feed.json"
            code = check_release.run(Namespace(
                repos_file=repos, fixture_releases=fixture, state_db=tmp / "state.sqlite3",
                cache_path=tmp / "legacy.json", config=tmp / "missing-config.yaml",
                feed_path=feed, github_output=None, mode="preview", send_slack=False,
                no_sleep=True, sleep_seconds=0,
            ))
            self.assertEqual(code, 1)
            output = feed.read_text(encoding="utf-8")
            self.assertNotIn("private-token-marker", output)
            parsed = json.loads(output)
            self.assertEqual(parsed["collector_errors_by_type"], {"http_404": 1, "http_500": 1})
            self.assertEqual(parsed["release_count"], 1)

    def test_feed_pending_count_reflects_post_delivery_state(self) -> None:
        class FakeTransport:
            def __init__(self, status):
                self.status = status

            def send(self, payload):
                from starwatch.notifier import SlackResult
                return SlackResult(self.status)

        for status, expected in ((200, 0), (500, 1)):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp_dir:
                tmp = Path(tmp_dir)
                repos = tmp / "repos.txt"
                repos.write_text("owner/repo\n", encoding="utf-8")
                fixture = tmp / "fixture.json"
                fixture.write_text(json.dumps({"owner/repo": {
                    "id": 1, "tag_name": "v1", "published_at": "2026-10-02T00:00:00Z"
                }}), encoding="utf-8")
                config = tmp / "config.yaml"
                config.write_text("notification:\n  min_release_count: 1\n  first_run_notify: true\n")
                feed = tmp / "feed.json"
                code = check_release.run(Namespace(
                    repos_file=repos, fixture_releases=fixture, state_db=tmp / "state.sqlite3",
                    cache_path=tmp / "legacy.json", config=config, feed_path=feed,
                    github_output=None, mode="commit", send_slack=True,
                    no_sleep=True, sleep_seconds=0,
                ), transport=FakeTransport(status))
                self.assertEqual(code, 0 if status == 200 else 1)
                parsed = json.loads(feed.read_text(encoding="utf-8"))
                self.assertEqual(parsed["pending_before_delivery_count"], 1)
                self.assertEqual(parsed["pending_count"], expected)


if __name__ == "__main__":
    unittest.main()
