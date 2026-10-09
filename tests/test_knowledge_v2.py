"""Visibility-safe, side-effect-free Knowledge export regression tests."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from starwatch.analysis import AnalysisConfig, AnalysisService
from starwatch.event_store import EventStore, SCHEMA_V1

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("knowledge_v2", ROOT / "scripts/export_knowledge_jsonl.py")
exporter = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = exporter
SPEC.loader.exec_module(exporter)


def signal(event_id: str, *, scope: str = "public", body: str = "official body",
           advisory: bool = False) -> dict:
    result = {
        "schema_version": "k8s-intelligence-event/v1",
        "event_id": event_id, "event_type": "github_security_advisory" if advisory else "github_issue",
        "source_id": event_id, "repository": "public/project" if scope == "public" else "secret/private-project",
        "published_at": "2026-10-09T00:00:00Z", "updated_at": "2026-10-09T01:00:00Z",
        "visibility": scope, "title": "Official signal", "body": body,
        "html_url": "https://github.com/public/project/security/advisories/GHSA-aaaa-bbbb-cccc",
        "provenance": {"collector": "fixture", "source_url": "https://github.com/public/project"},
        "content_hash": "sha256:" + hashlib.sha256(body.encode()).hexdigest(),
    }
    if advisory:
        result.update({"ghsa_id": "GHSA-aaaa-bbbb-cccc", "severity": "critical",
                       "project_mapping": {"status": "mapped", "project": "public/project",
                                           "reason": "package_map", "confidence": "deterministic"},
                       "metadata": {"vulnerabilities": [{"package": {"ecosystem": "pip", "name": "example"},
                                                         "vulnerable_version_range": "<2", "first_patched_version": "2.0"}],
                                    "cwes": [{"cwe_id": "CWE-79"}],
                                    "references": ["https://github.com/public/project"]}})
    return result


class KnowledgeV2Tests(unittest.TestCase):
    def _db(self, directory: Path) -> Path:
        path = directory / "events.sqlite3"
        store = EventStore.open(path)
        with store.transaction():
            store.upsert_raw(signal("github:ghsa:GHSA-aaaa-bbbb-cccc", advisory=True), suppress=True)
            store.upsert_raw(signal("github:issue:11:2", scope="private", body="Authorization: Bearer PRIVATESECRET"), suppress=True)
            store.upsert_raw(signal("github:issue:11:3", scope="unknown"), suppress=True)
            store.set_meta("project_visibility:public/project", "public")
        store.close()
        return path

    def test_public_export_raw_ai_revision_and_full_advisory_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(Path(tmp))
            store = EventStore.open(db)
            updated = signal("github:ghsa:GHSA-aaaa-bbbb-cccc", advisory=True, body="updated advisory full body")
            with store.transaction():
                store.upsert_raw(updated, suppress=True)
                AnalysisService(AnalysisConfig()).analyze(updated, "CRITICAL", store=store)
            store.close()
            documents = exporter.export_database(db)
            self.assertEqual([doc.document_type for doc in documents].count("raw_event"), 1)
            self.assertEqual([doc.document_type for doc in documents].count("revision"), 2)
            self.assertEqual([doc.document_type for doc in documents].count("ai_analysis"), 1)
            raw = next(doc for doc in documents if doc.document_type == "raw_event")
            self.assertEqual(raw.document_id, "event:github:ghsa:GHSA-aaaa-bbbb-cccc")
            self.assertIn("updated advisory full body", raw.body)
            self.assertEqual(raw.metadata["advisory"]["severity"], "critical")
            self.assertEqual(raw.metadata["advisory"]["vulnerabilities"][0]["first_patched_version"], "2.0")
            self.assertEqual(raw.metadata["provenance"]["collector"], "fixture")
            analysis = next(doc for doc in documents if doc.document_type == "ai_analysis")
            self.assertEqual(analysis.related_document_ids, [raw.document_id])
            self.assertEqual(analysis.metadata["analysis"]["provider"], "deterministic")

    def test_private_unknown_default_exclusion_and_explicit_private_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(Path(tmp))
            public = exporter.export_database(db)
            self.assertTrue(all(doc.visibility == "public" for doc in public))
            self.assertNotIn("secret/private-project", "\n".join(doc.to_json() for doc in public))
            with self.assertRaises(ValueError):
                exporter.export_database(db, include_private=True)
            private = exporter.export_database(db, include_private=True, destination="private")
            self.assertEqual({doc.visibility for doc in private}, {"public", "private"})
            self.assertNotIn("PRIVATESECRET", "\n".join(doc.to_json() for doc in private))
            self.assertNotIn("unknown", {doc.visibility for doc in private})
            output = Path(tmp) / "private.jsonl"
            exporter.write_jsonl(private, output)
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)

    def test_export_is_readonly_network_free_and_byte_stable(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(Path(tmp))
            before = db.read_bytes()
            with patch("socket.create_connection", side_effect=AssertionError("network forbidden")):
                first = [doc.to_json() for doc in exporter.export_database(db)]
                second = [doc.to_json() for doc in exporter.export_database(db)]
            self.assertEqual(first, second)
            self.assertEqual(db.read_bytes(), before)
            self.assertFalse(db.with_name(db.name + "-wal").exists())
            self.assertFalse(db.with_name(db.name + "-shm").exists())
            self.assertEqual(len({json.loads(line)["document_id"] for line in first}), len(first))

    def test_feed_schema_and_visibility_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "feed.json"
            base = {"schema_version": "github-stars-release-feed/v1", "releases": [
                {"event_id": "github:release:1", "repo": "public/project", "tag": "v1",
                 "visibility": "public", "body": "full release notes"},
                {"event_id": "github:release:2", "repo": "secret/private-project", "tag": "v2",
                 "visibility": "private"},
                {"event_id": "github:release:3", "repo": "unknown/project", "tag": "v3"}]}
            path.write_text(json.dumps(base), encoding="utf-8")
            docs = exporter.export_documents(path)
            self.assertEqual([doc.event_id for doc in docs], ["github:release:1"])
            self.assertIn("full release notes", docs[0].body)
            for malformed in ({"schema_version": "bad", "releases": []},
                              {"schema_version": "github-stars-release-feed/v1", "releases": "bad"},
                              {**base, "new_releases": []},
                              {**base, "releases": ["not an object"]}):
                path.write_text(json.dumps(malformed), encoding="utf-8")
                with self.assertRaises(ValueError):
                    exporter.export_documents(path)

    def test_malformed_database_and_output_alias_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.sqlite3"
            path.write_bytes(b"not sqlite")
            with self.assertRaises(ValueError):
                exporter.export_database(path)
            feed = Path(tmp) / "feed.json"
            feed.write_text(json.dumps({"schema_version": "github-stars-release-feed/v1", "releases": []}))
            with patch.object(sys, "argv", ["export", "--feed", str(feed), "--output", str(feed)]):
                with self.assertRaises(ValueError):
                    exporter.main()

    def test_v1_database_export_does_not_migrate_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "legacy-v1.sqlite3"
            value = signal("github:issue:11:1")
            with sqlite3.connect(path) as db:
                db.executescript(SCHEMA_V1)
                db.execute("PRAGMA user_version=1")
                db.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)",
                           (value["event_id"], value["event_type"], value["repository"],
                            value["source_id"], value["published_at"], value["published_at"],
                            value["updated_at"], value["content_hash"], json.dumps(value)))
            before = path.read_bytes()
            docs = exporter.export_database(path)
            self.assertEqual([doc.document_id for doc in docs], ["event:github:issue:11:1"])
            self.assertEqual(path.read_bytes(), before)
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)

    def test_current_inventory_privacy_revokes_historical_public_documents(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(Path(tmp))
            store = EventStore.open(db)
            original = signal("github:issue:99:1", body="former public body")
            revised = signal("github:issue:99:1", body="new public body")
            with store.transaction():
                store.upsert_raw(original, suppress=True)
                store.upsert_raw(revised, suppress=True)
                AnalysisService(AnalysisConfig()).analyze(revised, "DIGEST", store=store)
            before = exporter.export_database(db)
            self.assertTrue(any(doc.event_id == original["event_id"] for doc in before))
            with store.transaction():
                store.set_meta("repository_visibility:public/project", "private")
            store.close()
            after = exporter.export_database(db)
            self.assertFalse(any(doc.event_id == original["event_id"] for doc in after))
            self.assertNotIn("former public body", "\n".join(doc.to_json() for doc in after))
            self.assertNotIn("new public body", "\n".join(doc.to_json() for doc in after))
            # Either current visibility marker can tighten an advisory's
            # derived project scope; public source does not leak the name.
            self.assertFalse(any(doc.event_id == "github:ghsa:GHSA-aaaa-bbbb-cccc" for doc in after))

    def test_private_mapped_ghsa_excludes_raw_revision_and_ai_but_unmapped_remains(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(Path(tmp))
            store = EventStore.open(db)
            mapped = signal("github:ghsa:GHSA-aaaa-bbbb-cccc", advisory=True,
                            body="mapped private package")
            unmapped = signal("github:ghsa:GHSA-1111-2222-3333", advisory=True,
                              body="public unmatched advisory")
            unmapped["project_mapping"] = {"status": "unmapped", "project": None,
                                            "reason": "no_registry_match", "confidence": "ambiguous"}
            unmapped["repository"] = ""
            with store.transaction():
                store.upsert_raw(mapped, suppress=True)
                AnalysisService(AnalysisConfig()).analyze(mapped, "CRITICAL", store=store)
                store.upsert_raw(unmapped, suppress=True)
                store.set_meta("project_visibility:public/project", "private")
            store.close()
            docs = exporter.export_database(db)
            self.assertFalse(any(doc.event_id == mapped["event_id"] for doc in docs))
            self.assertFalse(any("mapped private package" in doc.to_json() for doc in docs))
            self.assertTrue(any(doc.event_id == unmapped["event_id"] for doc in docs))


if __name__ == "__main__":
    unittest.main()
