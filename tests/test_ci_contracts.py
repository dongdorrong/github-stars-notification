"""No-token PR CI gates must reject drift and malformed contract fixtures."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from scripts.check_contracts import (
    validate_locks,
    validate_no_embedded_secrets,
    validate_python_version,
    validate_schemas,
)


ROOT = Path(__file__).resolve().parents[1]


class CIContractsTests(unittest.TestCase):
    def test_real_registry_schemas_and_hashed_locks(self) -> None:
        validate_schemas()
        validate_locks()

    def test_invalid_registry_schema_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "schemas").mkdir()
            (root / "config").mkdir()
            for name in ("project-registry.schema.json", "ai-analysis-v1.json"):
                shutil.copy(ROOT / "schemas" / name, root / "schemas" / name)
            raw = json.loads(
                (ROOT / "config/projects.yaml").read_text(encoding="utf-8")
            )
            raw["projects"]["kubernetes/kubernetes"]["tier"] = "untrusted"
            (root / "config/projects.yaml").write_text(
                json.dumps(raw), encoding="utf-8"
            )
            with self.assertRaises(Exception):
                validate_schemas(root)

    def test_unpinned_or_unhashed_lock_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = root / ".github/scripts"
            dest.mkdir(parents=True)
            for name in (
                "requirements.in",
                "requirements-dev.in",
                "requirements.txt",
                "requirements-dev.txt",
            ):
                shutil.copy(ROOT / ".github/scripts" / name, dest / name)
            (dest / "requirements.in").write_text("PyGithub>=2.2.0\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "exactly pinned"):
                validate_locks(root)
            shutil.copy(
                ROOT / ".github/scripts/requirements.in", dest / "requirements.in"
            )
            (dest / "requirements.txt").write_text(
                "PyGithub==2.2.0\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "unhashed"):
                validate_locks(root)

    def test_secret_shape_scan_reports_no_values(self) -> None:
        validate_no_embedded_secrets()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "starwatch").mkdir()
            (root / "starwatch/sample.py").write_text(
                "TOKEN = 'ghp_" + "x" * 36 + "'\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(
                ValueError, "embedded secret-shaped value"
            ) as error:
                validate_no_embedded_secrets(root)
            self.assertNotIn("ghp_", str(error.exception))
            (root / "starwatch/sample.py").write_text(
                "URL = 'https://user:secret@example.org/feed'\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "embedded secret-shaped value"):
                validate_no_embedded_secrets(root)

    def test_least_privilege_pr_ci_and_no_production_secret_bindings(self) -> None:
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        self.assertIn("  pull_request:", workflow)
        self.assertIn("permissions:\n  contents: read", workflow)
        self.assertIn("persist-credentials: false", workflow)
        self.assertIn("python-version: '3.12'", workflow)
        self.assertIn("--require-hashes", workflow)
        self.assertIn("pip-audit", workflow)
        self.assertNotIn("secrets.", workflow)
        self.assertNotIn("--send-slack", workflow)
        self.assertNotIn("--mode commit", workflow)
        self.assertNotIn("${{", workflow)

    def test_python_minor_agrees_across_workflows(self) -> None:
        validate_python_version()


if __name__ == "__main__":
    unittest.main()
