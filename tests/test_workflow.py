"""Token-free checks for the stateful Actions workflow contract."""

import re
import unittest
from pathlib import Path


WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/notify-starred-releases.yml"


class WorkflowContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = WORKFLOW.read_text(encoding="utf-8")

    def test_schedule_and_safe_dispatch_default(self):
        for cron in ("0 23 * * *", "0 5 * * *", "0 8 * * *"):
            self.assertIn(f"cron: '{cron}'", self.workflow)
        self.assertRegex(self.workflow, r"(?m)^      mode:\n(?:.*\n){0,6}        default: preview$")
        self.assertIn("          - preview", self.workflow)
        self.assertIn("          - commit", self.workflow)
        self.assertNotIn("send_slack:", self.workflow)

    def test_serialized_least_privilege_single_job(self):
        self.assertIn("permissions:\n  contents: read", self.workflow)
        self.assertIn("group: github-stars-intelligence-state", self.workflow)
        self.assertIn("cancel-in-progress: false", self.workflow)
        self.assertIn("timeout-minutes: 30", self.workflow)
        self.assertNotIn("strategy:\n", self.workflow)
        self.assertNotIn("  notify:\n", self.workflow)

    def test_preview_never_receives_slack_secret_or_saves_state(self):
        preview = self.workflow.split("      - name: Preview releases (read-only)", 1)[1].split(
            "      - name: Collect and notify releases", 1
        )[0]
        self.assertIn("inputs.mode == 'preview'", preview)
        self.assertIn("--mode preview --state-db .cache/events.sqlite3", preview)
        self.assertNotIn("--send-slack", preview)
        self.assertNotIn("SLACK_WEBHOOK_URL", preview)
        self.assertIn("github.event_name == 'schedule' || inputs.mode == 'commit'", self.workflow)
        self.assertIn("--mode commit --state-db .cache/events.sqlite3 --send-slack", self.workflow)

    def test_default_branch_guard_precedes_state_and_slack(self):
        guard = self.workflow.index("- name: Reject commit mode outside the default branch")
        restore = self.workflow.index("- name: Restore legacy release cache")
        send = self.workflow.index("- name: Collect and notify releases")
        self.assertLess(guard, restore)
        self.assertLess(guard, send)
        self.assertIn('"$RUN_MODE" == commit && "$REF_NAME" != "$DEFAULT_BRANCH"', self.workflow)

    def test_dedicated_db_cache_and_failed_send_persistence(self):
        legacy = self.workflow.split("      - name: Restore legacy release cache", 1)[1].split(
            "      - name: Restore event DB", 1
        )[0]
        event = self.workflow.split("      - name: Restore event DB", 1)[1].split(
            "      - name: List starred repos", 1
        )[0]
        save = self.workflow.split("      - name: Save event DB", 1)[1].split(
            "      # Do not upload", 1
        )[0]
        self.assertIn("actions/cache/restore@0057852bfaa89a56745cba8c7296529d2fc39830", legacy)
        # Cache version hashes the saved path; old archives used the directory.
        self.assertIn("path: .cache\n", legacy)
        self.assertIn("releases-cache-", legacy)
        self.assertIn("actions/cache/restore@0057852bfaa89a56745cba8c7296529d2fc39830", event)
        self.assertIn("path: .cache/events.sqlite3", event)
        self.assertIn("actions/cache/save@0057852bfaa89a56745cba8c7296529d2fc39830", save)
        self.assertIn("path: .cache/events.sqlite3", save)
        self.assertIn("github.run_id }}-${{ github.run_attempt", event)
        self.assertIn("github.run_id }}-${{ github.run_attempt", save)
        self.assertIn("if: always()", save)
        self.assertIn("steps.validate_state.outcome == 'success'", save)
        self.assertIn("EventStore.open(path, preview=True).close()", self.workflow)
        self.assertIn("steps.detect_commit.outcome != 'skipped'", self.workflow)
        self.assertIn("github.ref_name == github.event.repository.default_branch", save)

    def test_private_metadata_not_uploaded_or_interpolated_in_shell(self):
        self.assertNotIn("actions/upload-artifact", self.workflow)
        self.assertNotIn("fromJSON(", self.workflow)
        self.assertNotIn("${{ steps.detect.outputs.notify_reason }}", self.workflow)
        run_blocks = re.findall(r"(?m)^        run: \|\n((?:          .*\n|\n)*)", self.workflow)
        for block in run_blocks:
            self.assertNotIn("${{", block)


if __name__ == "__main__":
    unittest.main()
