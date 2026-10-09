"""Token-free merge-gate regressions for feed identity and collector health."""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

from starwatch.event_store import EventStore
from starwatch.notifier import SlackResult

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / '.github/scripts/check_release.py'
spec = importlib.util.spec_from_file_location('closeout_check_release', SCRIPT)
script = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = script
spec.loader.exec_module(script)


def release(number: int) -> dict:
    return {'id': number, 'tag_name': f'v{number}', 'published_at': '2026-10-01T00:00:00Z',
            'html_url': f'https://github.com/owner/repo/releases/tag/v{number}'}


class Transport:
    def __init__(self, *statuses: int):
        self.statuses = list(statuses)
        self.sent = []

    def send(self, payload):
        self.sent.append(payload)
        return SlackResult(self.statuses.pop(0))


class CloseoutTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.repos = self.root / 'repos.txt'
        self.repos.write_text('owner/repo\n')
        self.fixture = self.root / 'fixture.json'
        self.config = self.root / 'config.yaml'
        self.config.write_text('notification:\n  min_release_count: 5\n  first_run_notify: false\n')
        self.db = self.root / 'events.sqlite3'
        self.feed = self.root / 'feed.json'
        self.output = self.root / 'output.txt'

    def run_fixture(self, fixture, *, send=False, transport=None, mode='commit'):
        self.fixture.write_text(json.dumps(fixture))
        args = script.build_arg_parser().parse_args([
            '--repos-file', str(self.repos), '--fixture-releases', str(self.fixture),
            '--state-db', str(self.db), '--cache-path', str(self.root / 'legacy.json'),
            '--config', str(self.config), '--feed-path', str(self.feed),
            '--github-output', str(self.output), '--mode', mode,
            *(['--send-slack'] if send else []),
        ])
        code = script.run(args, transport=transport)
        return code, json.loads(self.feed.read_text())

    def test_cli_four_pending_then_fifth_notification_feed_matches_slack(self):
        self.run_fixture({'owner/repo': []})
        code, four = self.run_fixture({'owner/repo': [release(n) for n in range(1, 5)]})
        self.assertEqual(code, 0)
        self.assertEqual(four['pending_release_count'], 4)
        self.assertEqual(four['notification_batch_count'], 0)
        transport = Transport(200)
        code, five = self.run_fixture({'owner/repo': [release(n) for n in range(1, 6)]},
                                      send=True, transport=transport)
        self.assertEqual(code, 0)
        self.assertEqual(five['new_release_count'], 1)
        self.assertEqual(five['pending_release_count'], 5)
        self.assertEqual(five['notification_batch_count'], 5)
        self.assertEqual(five['releases'], five['new_releases'])
        expected = [item['event_id'] for item in five['notification_batch']]
        self.assertEqual([eid for chunk in five['slack_chunks'] for eid in chunk['event_ids']], expected)
        self.assertEqual(len(expected), len(set(expected)))
        self.assertEqual([chunk['payload'] for chunk in five['slack_chunks']], transport.sent)
        self.assertIn('notification_batch[]', five['llm_contract']['input_guidance'])
        self.assertEqual(five['pending_count'], 0)

    def test_partial_multichunk_failure_preserves_batch_mapping(self):
        self.config.write_text('notification:\n  min_release_count: 1\n  first_run_notify: true\n  max_slack_text_length: 1000\n')
        # Long names force two or more chunks within the 1000-character floor.
        items = [dict(release(n), name='x' * 700) for n in range(1, 4)]
        transport = Transport(200, 500, 200)
        code, feed = self.run_fixture({'owner/repo': items}, send=True, transport=transport)
        self.assertEqual(code, 1)
        self.assertGreaterEqual(len(feed['slack_chunks']), 2)
        ids = [eid for chunk in feed['slack_chunks'] for eid in chunk['event_ids']]
        self.assertEqual(ids, [item['event_id'] for item in feed['notification_batch']])
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(feed['pending_count'], len(feed['slack_chunks'][1]['event_ids']))

    def test_isolated_repository_errors_degrade_but_retain_good_events(self):
        self.repos.write_text('owner/repo\nbad/repo\n')
        for status in (404, 500):
            with self.subTest(status=status):
                self.db.unlink(missing_ok=True)
                code, feed = self.run_fixture({'owner/repo': [release(1)], 'bad/repo': {
                    'error': {'status': status, 'message': 'secret-body token=https://example.invalid/private'}}})
                self.assertEqual(code, 0)
                self.assertTrue(feed['collection_degraded'])
                self.assertEqual(feed['collection_error_count'], 1)
                self.assertEqual(feed['collector_errors_by_type'], {f'http_{status}': 1})
                self.assertEqual(feed['collection_success_count'], 1)
                self.assertNotIn('secret-body', self.feed.read_text())
                self.assertNotIn('example.invalid', self.output.read_text())
                store = EventStore.open(self.db, preview=True)
                try:
                    self.assertIn('github:release:1', store.event_ids())
                finally:
                    store.close()

    def test_majority_failure_is_fatal_and_never_sends_slack(self):
        self.repos.write_text('owner/repo\nbad/one\nbad/two\n')
        self.config.write_text('notification:\n  min_release_count: 1\n  first_run_notify: true\n')
        transport = Transport(200)
        code, feed = self.run_fixture({'owner/repo': [release(1)],
            'bad/one': {'error': {'status': 404, 'message': 'secret'}},
            'bad/two': {'error': {'status': 500, 'message': 'secret'}}},
            send=True, transport=transport)
        self.assertEqual(code, 1)
        self.assertFalse(feed['collection_degraded'])
        self.assertEqual(feed['collection_success_count'], 1)
        self.assertEqual(transport.sent, [])
        store = EventStore.open(self.db, preview=True)
        try:
            self.assertIn('github:release:1', store.event_ids())
        finally:
            store.close()

    def test_all_failed_and_systemic_auth_are_fatal(self):
        self.repos.write_text('owner/repo\nbad/repo\n')
        for statuses in ((404, 500), (401, None), (403, None)):
            with self.subTest(statuses=statuses):
                fixture = {'owner/repo': {'error': {'status': statuses[0], 'message': 'secret'}}}
                fixture['bad/repo'] = ({'error': {'status': statuses[1], 'message': 'secret'}}
                                       if statuses[1] else [release(2)])
                code, feed = self.run_fixture(fixture)
                self.assertEqual(code, 1)
                self.assertFalse(feed['collection_degraded'])

    def test_degraded_collector_slack_failure_still_fatal(self):
        self.repos.write_text('owner/repo\nbad/repo\n')
        self.config.write_text('notification:\n  min_release_count: 1\n  first_run_notify: true\n')
        code, feed = self.run_fixture({'owner/repo': [release(1)], 'bad/repo': {
            'error': {'status': 404, 'message': 'secret'}}}, send=True, transport=Transport(500))
        self.assertEqual(code, 1)
        self.assertTrue(feed['collection_degraded'])
        self.assertFalse(feed['delivery_succeeded'])

    def test_error_ratio_boundary_and_unknown_error(self):
        self.repos.write_text('good/one\ngood/two\nbad/one\nbad/two\n')
        data = {'good/one': [], 'good/two': [],
                'bad/one': {'error': {'status': 429, 'message': 'secret'}},
                'bad/two': {'error': {'status': 503, 'message': 'secret'}}}
        code, feed = self.run_fixture(data)
        self.assertEqual(code, 0)  # 50% is permitted.
        self.assertTrue(feed['collection_degraded'])
        self.assertEqual(feed['collector_errors_by_type'], {'http_429': 1, 'http_503': 1})
        data['bad/two']['error']['status'] = 408
        code, feed = self.run_fixture(data)
        self.assertEqual(code, 1)  # Unclassified status is not isolated.
        self.assertFalse(feed['collection_degraded'])

    def test_deprecated_sleep_flags_remain_explicitly_reported(self):
        import contextlib
        import io
        self.fixture.write_text(json.dumps({'owner/repo': []}))
        args = script.build_arg_parser().parse_args([
            '--repos-file', str(self.repos), '--fixture-releases', str(self.fixture),
            '--state-db', str(self.db), '--cache-path', str(self.root / 'legacy.json'),
            '--config', str(self.config), '--feed-path', str(self.feed), '--no-sleep',
        ])
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(script.run(args), 0)
        self.assertIn('deprecated and have no effect', stderr.getvalue())


if __name__ == '__main__':
    unittest.main()
