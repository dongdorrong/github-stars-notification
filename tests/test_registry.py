from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from starwatch.registry import load_registry, validate_registry

ROOT = Path(__file__).resolve().parents[1]


class RegistryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.raw = json.loads((ROOT / "config/projects.yaml").read_text(encoding="utf-8"))

    def load(self, raw: dict, legacy: tuple[str, ...] = ()):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "projects.yaml"
            path.write_text(json.dumps(raw), encoding="utf-8")
            return load_registry(path, legacy)

    def test_initial_registry_is_versioned_and_has_required_projects(self) -> None:
        registry = self.load(self.raw)
        self.assertEqual(len(registry.projects), 10)
        self.assertTrue(registry.resolve("Kubernetes / Kubernetes").immediate_release)
        self.assertFalse(registry.resolve("prometheus/prometheus").immediate_release)

    def test_explicit_policy_wins_and_ignore_wins(self) -> None:
        policy = self.raw["projects"]["grafana/grafana"]
        policy["classification"]["result"] = "NOT_KUBERNETES"
        registry = self.load(self.raw)
        item = {"full_name": "grafana/grafana", "topics": ["kubernetes"]}
        self.assertEqual(registry.classify(item).result, "NOT_KUBERNETES")
        self.assertEqual(registry.classify(item).source, "explicit")
        policy["tier"] = "ignore"
        self.assertTrue(self.load(self.raw).classify(item).ignored)

    def test_owner_topic_name_description_negative_and_ambiguous(self) -> None:
        registry = self.load(self.raw)
        self.assertEqual(registry.classify({"full_name": "kubernetes/new-repo"}).source, "owner")
        self.assertEqual(registry.classify({"full_name": "kubernetes-sigs/new-repo"}).source, "owner")
        self.assertEqual(registry.classify({"full_name": "vendor/project", "topics": ["cncf"]}).source, "topic")
        self.assertEqual(registry.classify({"full_name": "vendor/k8s-operator"}).source, "name")
        self.assertEqual(registry.classify({"full_name": "vendor/project", "description": "Kubernetes controller"}).source, "description")
        self.assertEqual(registry.classify({"full_name": "vendor/notkubernetes", "description": "A web theme"}).result, "NOT_KUBERNETES")
        self.assertEqual(registry.classify({"full_name": "vendor/project"}).result, "AMBIGUOUS")

    def test_alias_maps_one_policy_and_rejects_collision(self) -> None:
        self.raw["projects"]["grafana/grafana"]["aliases"] = ["old/grafana"]
        registry = self.load(self.raw)
        self.assertIs(registry.resolve("old/grafana"), registry.resolve("grafana/grafana"))
        self.assertEqual(registry.classify("old/grafana").project, "grafana/grafana")
        self.raw["projects"]["prometheus/prometheus"]["aliases"] = ["old/grafana"]
        with self.assertRaisesRegex(ValueError, "alias collides"):
            self.load(self.raw)

    def test_legacy_special_translation_and_conflict(self) -> None:
        registry = self.load(self.raw, ("GRAFANA / GRAFANA", "legacy/old"))
        self.assertTrue(registry.resolve("legacy/old").legacy_special)
        self.assertTrue(registry.resolve("legacy/old").immediate_release)
        self.assertFalse(registry.resolve("legacy/old").signals["advisory"])
        self.raw["projects"]["grafana/grafana"]["routing"]["release_floor"] = "digest"
        with self.assertRaisesRegex(ValueError, "legacy special project conflicts"):
            self.load(self.raw, ("grafana/grafana",))

    def test_schema_rejects_invalid_tier_category_signal_visibility(self) -> None:
        for key, value in (
            ("tier", "urgent"), ("categories", ["unknown"]),
            ("signals", {"release": "true"}), ("visibility", "secret"),
        ):
            with self.subTest(key=key):
                candidate = copy.deepcopy(self.raw)
                candidate["projects"]["grafana/grafana"][key] = value
                with self.assertRaises(ValueError):
                    validate_registry(candidate)

    def test_schema_rejects_unknown_fields_and_noncanonical_keys(self) -> None:
        self.raw["projects"]["grafana/grafana"]["unsafe"] = True
        with self.assertRaises(ValueError):
            validate_registry(self.raw)
        self.raw["projects"]["grafana/grafana"].pop("unsafe")
        self.raw["projects"]["Grafana/Grafana"] = self.raw["projects"].pop("grafana/grafana")
        with self.assertRaises(ValueError):
            validate_registry(self.raw)

    def test_report_is_aggregate_and_private_names_do_not_escape(self) -> None:
        registry = self.load(self.raw)
        rows = [
            {"full_name": "private-owner/secret-repo", "visibility": "private", "description": "internal finance"},
            {"full_name": "kubernetes/kubernetes", "visibility": "public"},
            {"full_name": "vendor/thing", "visibility": "internal"},
        ]
        report = registry.classification_report(rows)
        self.assertEqual(report["total_starred_repositories"], 3)
        self.assertEqual(report["visibility_counts"]["private"], 1)
        self.assertEqual(report["explicit_registry_count"], 1)
        self.assertNotIn("private-owner", json.dumps(report))
        self.assertNotIn("secret-repo", json.dumps(report))

    def test_private_key_does_not_escape_validation_error(self) -> None:
        self.raw["projects"]["private-owner/secret-repo"] = copy.deepcopy(self.raw["projects"]["grafana/grafana"])
        self.raw["projects"]["private-owner/secret-repo"]["tier"] = "invalid"
        with self.assertRaises(ValueError) as failure:
            validate_registry(self.raw)
        self.assertNotIn("private-owner", str(failure.exception))


if __name__ == "__main__":
    unittest.main()
