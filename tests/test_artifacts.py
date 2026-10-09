"""Public artifact opt-in, visibility and structured-redaction fixtures."""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from starwatch.artifacts import export_artifacts


def release(number: int, repo: str = 'public/project', scope: str = 'public',
            body: str = 'Full official release body') -> dict:
    return {'event_id': f'github:release:{number}', 'event_type': 'github_release',
            'repository': repo, 'visibility': scope, 'published_at': '2026-10-09T00:00:00Z',
            'content_hash': 'sha256:' + hashlib.sha256(body.encode()).hexdigest(),
            'tag_name': f'v{number}', 'release_name': f'Release v{number}', 'body': body,
            'html_url': f'https://github.com/{repo}/releases/tag/v{number}',
            'raw_metadata': {'Authorization': 'Bearer SECRET_FIXTURE'}}


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / 'artifacts'
        self.inventory = [
            {'full_name': 'public/project', 'id': 1, 'visibility': 'public',
             'description': 'GH_TOKEN=SECRET_FIXTURE'},
            {'full_name': 'secret/private-project', 'id': 2, 'visibility': 'private'},
            {'full_name': 'unknown/project', 'id': 3, 'visibility': 'unknown'},
        ]
        self.feed = {'schema_version': 'github-stars-release-feed/v1',
                     'releases': [release(1), release(2, 'secret/private-project', 'public'),
                                  release(3, 'unknown/project', 'unknown')],
                     'pending_releases': [release(1)],
                     'notification_batch': [release(1), release(2, 'secret/private-project', 'public')],
                     'slack_chunks': [{'text': 'SECRET_FIXTURE'}]}
        self.config = {'artifacts': {
            'inventory': {'enabled': True, 'public_only': True},
            'release_feed': {'enabled': True, 'public_only': True},
            'knowledge_export': {'enabled': True, 'public_only': True}}}

    def test_default_disabled_creates_no_directory_or_files(self):
        self.assertIsNone(export_artifacts({}, self.inventory, self.feed, self.root))
        self.assertFalse(self.root.exists())

    def test_public_products_are_allowlisted_and_current_inventory_wins(self):
        manifest = export_artifacts(self.config, self.inventory, self.feed, self.root)
        self.assertEqual(manifest['public_repository_count'], 1)
        self.assertEqual(manifest['public_new_release_count'], 1)
        self.assertEqual(manifest['excluded_repository_count'], 2)
        self.assertEqual(set(manifest['files']), {
            'inventory-public.json', 'release-feed-public.json', 'knowledge-public.jsonl'})
        public_feed = json.loads((self.root / 'release-feed-public.json').read_text())
        self.assertEqual(public_feed['new_release_count'], 1)
        self.assertEqual(public_feed['notification_batch_count'], 1)
        self.assertEqual(public_feed['notification_batch'][0]['event_id'], 'github:release:1')
        knowledge = [json.loads(line) for line in (self.root / 'knowledge-public.jsonl').read_text().splitlines()]
        self.assertEqual([item['document_id'] for item in knowledge], ['event:github:release:1'])
        self.assertEqual(knowledge[0]['body'], 'Release v1\nFull official release body')
        self.assertEqual(knowledge[0]['metadata']['provenance']['collector'], 'github-releases')
        output = '\n'.join(path.read_text() for path in self.root.iterdir())
        self.assertNotIn('secret/private-project', output)
        self.assertNotIn('unknown/project', output)
        self.assertNotIn('SECRET_FIXTURE', output)
        self.assertNotIn('slack_chunks', output)
        self.assertNotIn('raw_metadata', output)

    def test_public_notification_batch_preserves_ghsa_type(self):
        advisory = release(9)
        advisory['event_id'] = 'github:ghsa:GHSA-aaaa-bbbb-cccc'
        advisory['event_type'] = 'github_security_advisory'
        self.feed['notification_batch'].append(advisory)
        export_artifacts(self.config, self.inventory, self.feed, self.root)
        public_feed = json.loads((self.root / 'release-feed-public.json').read_text())
        self.assertEqual(public_feed['notification_batch_count'], 2)
        self.assertEqual(public_feed['notification_batch'][1]['event_type'],
                         'github_security_advisory')
        self.assertEqual(public_feed['new_release_count'], 1)

    def test_secret_value_in_public_release_body_is_redacted(self):
        self.feed['releases'] = [release(1, body=(
            'GH_TOKEN=SECRET_FIXTURE Authorization: Bearer SECRET_FIXTURE '
            'linked secret/private-project'))]
        self.feed['releases'][0]['html_url'] += '?token=SECRET_FIXTURE'
        with (patch('socket.create_connection', side_effect=AssertionError('network forbidden')),
              patch('sqlite3.connect', side_effect=AssertionError('database forbidden'))):
            export_artifacts(self.config, self.inventory, self.feed, self.root)
        output = '\n'.join(path.read_text() for path in self.root.iterdir())
        self.assertNotIn('SECRET_FIXTURE', output)
        self.assertNotIn('secret/private-project', output)
        self.assertIn('[REDACTED]', output)

    def test_enabled_private_policy_or_malformed_feed_fails_before_files(self):
        bad = {'artifacts': {'release_feed': {'enabled': True, 'public_only': False}}}
        with self.assertRaisesRegex(ValueError, 'public_only'):
            export_artifacts(bad, self.inventory, self.feed, self.root)
        self.assertFalse(self.root.exists())
        malformed = {**self.feed, 'new_releases': []}
        with self.assertRaisesRegex(ValueError, 'alias mismatch'):
            export_artifacts(self.config, self.inventory, malformed, self.root)
        self.assertFalse(self.root.exists())
        bad_event = release(1)
        bad_event['event_id'] = 'github:release:SECRET_FIXTURE'
        with self.assertRaisesRegex(ValueError, 'event identity'):
            export_artifacts(self.config, self.inventory,
                             {**self.feed, 'releases': [bad_event]}, self.root)
        self.assertFalse(self.root.exists())

    def test_repeat_identical_and_existing_different_or_unknown_file_rejected(self):
        export_artifacts(self.config, self.inventory, self.feed, self.root)
        first = {path.name: path.read_bytes() for path in self.root.iterdir()}
        export_artifacts(self.config, self.inventory, self.feed, self.root)
        self.assertEqual(first, {path.name: path.read_bytes() for path in self.root.iterdir()})
        changed = {**self.feed, 'releases': [release(4)]}
        with self.assertRaisesRegex(ValueError, 'different content'):
            export_artifacts(self.config, self.inventory, changed, self.root)
        self.assertEqual(first, {path.name: path.read_bytes() for path in self.root.iterdir()})
        (self.root / 'events.sqlite3').write_bytes(b'unsafe-state')
        with self.assertRaisesRegex(ValueError, 'unknown files'):
            export_artifacts(self.config, self.inventory, self.feed, self.root)


if __name__ == '__main__':
    unittest.main()
