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


if __name__ == "__main__":
    unittest.main()
