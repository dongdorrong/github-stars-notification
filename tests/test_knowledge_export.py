from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "export_knowledge_jsonl.py"
spec = importlib.util.spec_from_file_location("export_knowledge_jsonl", SCRIPT)
export_knowledge_jsonl = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = export_knowledge_jsonl
spec.loader.exec_module(export_knowledge_jsonl)


class GithubStarsKnowledgeExportTests(unittest.TestCase):
    def test_exports_release_feed_as_knowledge_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            feed_path = Path(tmp_dir) / "release-feed.json"
            feed_path.write_text(
                json.dumps(
                    {
                        "schema_version": "github-stars-release-feed/v1",
                        "generated_at": "2026-06-27T00:00:00Z",
                        "notify_reason": "special_project_release",
                        "releases": [
                            {
                                "repo": "ray-project/llmperf",
                                "tag": "v0.2.0",
                                "name": "Release v0.2.0",
                                "published": "2026-06-20 12:00:00",
                                "html_url": "https://github.com/ray-project/llmperf/releases/tag/v0.2.0",
                                "is_special": True,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            docs = export_knowledge_jsonl.export_documents(feed_path)

            self.assertEqual(len(docs), 1)
            payload = json.loads(docs[0].to_json())
            self.assertEqual(payload["source_id"], "github-stars")
            self.assertEqual(payload["document_id"], "releases/ray-project/llmperf/v0.2.0")
            self.assertEqual(payload["visibility"], "public")
            self.assertTrue(payload["content_hash"].startswith("sha256:"))


if __name__ == "__main__":
    unittest.main()
