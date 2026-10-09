"""Token-free GHSA API, mapping, cursor and revision contracts."""
from __future__ import annotations

import io
import json
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from starwatch.advisories import AdvisoryCollector, github_repository, map_project, normalize_advisory
from starwatch.event_store import EventStore
from starwatch.signal_collectors import GitHubReader, SourceError


NOW = datetime(2026, 10, 9, 0, 0, tzinfo=timezone.utc)


class Registry:
    def __init__(self):
        self.projects = {
            'kubernetes/kubernetes': SimpleNamespace(canonical_name='kubernetes/kubernetes',
                                                      packages=[{'ecosystem': 'go', 'name': 'k8s.io/kubernetes'}]),
            'argoproj/argo-cd': SimpleNamespace(canonical_name='argoproj/argo-cd',
                                               packages=[{'ecosystem': 'go', 'name': 'github.com/argoproj/argo-cd'}]),
        }
        self.aliases = {'old/k8s': 'kubernetes/kubernetes'}

    def resolve(self, name):
        return self.projects.get(self.aliases.get(name, name))


def advisory(*, ghsa='GHSA-aaaa-bbbb-cccc', updated='2026-10-08T20:00:00Z', **extra):
    return {'ghsa_id': ghsa, 'state': 'published', 'published_at': '2026-10-08T00:00:00Z',
            'updated_at': updated, 'severity': 'critical', 'summary': 'Critical fix',
            'description': 'Remote code execution',
            'repository_advisory_url': 'https://api.github.com/repos/kubernetes/kubernetes/security-advisories/' + ghsa,
            'vulnerabilities': [{'package': {'ecosystem': 'go', 'name': 'k8s.io/kubernetes'},
                                 'vulnerable_version_range': '<1.31.3',
                                 'first_patched_version': '1.31.3', 'vulnerable_functions': ['Run']}],
            'cvss_severities': {'cvss_v3': {'score': 9.8}, 'cvss_v4': {'score': 9.3}},
            'epss': {'percentage': 0.5}, 'cwes': [{'cwe_id': 'CWE-79'}],
            'references': ['https://github.com/kubernetes/kubernetes/issues/1'],
            'identifiers': [{'type': 'CVE', 'value': 'CVE-2026-1234'}],
            'credits': [{'user': {'login': 'helper', 'private': 'SECRET_FIXTURE'}, 'type': 'reporter'}],
            'collaborating_users': [{'login': 'SECRET_FIXTURE'}], **extra}


class FakeReader:
    def __init__(self, pages):
        self.pages = list(pages)
        self.paths = []

    def get(self, path, *, deadline):
        self.paths.append(path)
        result = self.pages.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class AdvisoryTests(unittest.TestCase):
    def setUp(self):
        self.registry = Registry()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name) / 'events.sqlite3'

    def collector(self, reader, **kwargs):
        return AdvisoryCollector(reader, self.registry, utcnow=lambda: NOW, **kwargs)

    def test_malformed_severity_is_safe_error(self):
        for severity in ([], {}, None, 5):
            with self.subTest(severity=severity), self.assertRaises(SourceError):
                normalize_advisory(advisory(severity=severity), self.registry,
                                   source_key='global')

    def test_malformed_vulnerability_page_is_safe_error(self):
        for malformed in (['bad'], [None], [{'package': 'bad'}], 'bad'):
            with self.subTest(malformed=malformed), self.assertRaises(SourceError):
                normalize_advisory(advisory(vulnerabilities=malformed), self.registry,
                                   source_key='global_advisories')

    def test_normalized_critical_slack_includes_affected_and_patched_versions(self):
        from starwatch.routing import decide, build_routed_chunks
        item = normalize_advisory(advisory(cve_id='CVE-2026-1234'), self.registry,
                                  source_key='global_advisories')
        item['routing'] = decide(item, {}, {})
        text = build_routed_chunks([item], 35000, {})[0].payload['text']
        self.assertIn('&lt;1.31.3', text)
        self.assertIn('patched: 1.31.3', text)
        self.assertIn('CVE-2026-1234', text)

    def test_official_api_repository_url_mapping_precedes_weak_conflict(self):
        item = advisory()
        item['references'] = ['https://github.com/argoproj/argo-cd/issues/1']
        self.assertEqual(map_project(item, self.registry), {
            'status': 'mapped', 'project': 'kubernetes/kubernetes',
            'reason': 'repository_advisory_url', 'confidence': 'deterministic'})
        self.assertEqual(github_repository(item['repository_advisory_url']), 'kubernetes/kubernetes')
        self.assertEqual(github_repository('https://github.com/old/k8s/tree/main'), 'old/k8s')
        self.assertIsNone(github_repository('https://github.com:bad/owner/repo'))

    def test_package_alias_reference_and_unmapped_evidence(self):
        item = advisory()
        item.pop('repository_advisory_url')
        item['source_code_location'] = 'https://github.com/old/k8s'
        self.assertEqual(map_project(item, self.registry)['reason'], 'source_code_location')
        item.pop('source_code_location')
        self.assertEqual(map_project(item, self.registry)['reason'], 'package_map')
        item['vulnerabilities'] = []
        self.assertEqual(map_project(item, self.registry)['reason'], 'reference_url')
        item['references'] = []
        self.assertEqual(map_project(item, self.registry)['status'], 'unmapped')

    def test_normalized_fields_private_metadata_excluded_and_visibility_explicit(self):
        event = normalize_advisory(advisory(), self.registry,
                                   source_key='repository_advisories:kubernetes/kubernetes',
                                   source_visibility='unknown')
        self.assertEqual(event['event_id'], 'github:ghsa:GHSA-AAAA-BBBB-CCCC')
        self.assertEqual(event['visibility'], 'unknown')
        self.assertEqual(event['severity'], 'critical')
        self.assertEqual(event['vulnerabilities'][0]['first_patched_version'], '1.31.3')
        self.assertEqual(event['cvss_severities']['cvss_v4']['score'], 9.3)
        self.assertEqual(event['cwes'][0]['cwe_id'], 'CWE-79')
        self.assertEqual(event['identifiers'][0]['value'], 'CVE-2026-1234')
        self.assertNotIn('SECRET_FIXTURE', json.dumps(event))
        self.assertEqual(event['metadata']['credits'], [{'login': 'helper', 'type': 'reporter'}])

    def test_global_bootstrap_window_and_new_after_first_complete_scan(self):
        reader = FakeReader([([advisory()], {}), ([advisory(ghsa='GHSA-dddd-eeee-ffff',
                                                         updated='2026-10-09T01:00:00Z')], {})])
        collector = self.collector(reader)
        first = collector.fetch_page({}, deadline=100)
        self.assertTrue(first.terminal)
        self.assertTrue(first.proposed_cursor['initialized'])
        self.assertIn('modified=', reader.paths[0])
        self.assertEqual(len(first.events), 1)
        store = EventStore.open(self.db)
        with store.transaction():
            for event in first.events:
                store.upsert_raw(event, suppress=True, suppression_reason='source_bootstrap')
        self.assertEqual(store.pending(), [])
        collector.utcnow = lambda: datetime(2026, 10, 9, 2, 0, tzinfo=timezone.utc)
        second = collector.fetch_page(first.proposed_cursor, deadline=100)
        with store.transaction():
            for event in second.events:
                store.upsert_raw(event)
        self.assertEqual([event['event_id'] for event in store.pending()],
                         ['github:ghsa:GHSA-DDDD-EEEE-FFFF'])
        store.close()

    def test_global_cursor_page_failure_does_not_advance(self):
        first_reader = FakeReader([])
        collector = self.collector(first_reader)
        path = collector.path + '?' + urllib.parse.urlencode({
            'per_page': collector.per_page, 'sort': 'updated', 'direction': 'asc',
            'modified': '2026-10-02T00:00:00Z..2026-10-09T00:00:00Z', 'after': 'opaque'})
        first_reader.pages = [([advisory()], {'link': f'<https://api.github.com{path}>; rel="next"'}),
                              SourceError('http_500')]
        page = collector.fetch_page({}, deadline=100)
        self.assertFalse(page.terminal)
        before = dict(page.proposed_cursor)
        with self.assertRaisesRegex(SourceError, 'http_500'):
            collector.fetch_page(before, deadline=100)
        self.assertEqual(before, page.proposed_cursor)
        self.assertNotIn('watermark', before)

    def test_overlap_and_withdrawn_scan_are_separate_sources(self):
        regular = self.collector(FakeReader([([], {})]))
        withdrawn_reader = FakeReader([([advisory(withdrawn_at='2026-10-08T21:00:00Z')], {})])
        withdrawn = self.collector(withdrawn_reader, withdrawn=True)
        self.assertNotEqual(regular.source_key, withdrawn.source_key)
        state = {'initialized': True, 'watermark': '2026-10-08T23:00:00Z'}
        page = withdrawn.fetch_page(state, deadline=100)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(withdrawn_reader.paths[0]).query)
        self.assertEqual(query['modified'], ['2026-10-08T22:00:00Z..2026-10-09T00:00:00Z'])
        self.assertEqual(query['is_withdrawn'], ['true'])
        self.assertEqual(page.events[0]['withdrawn_at'], '2026-10-08T21:00:00Z')

    def test_repository_visibility_unknown_and_capability_403_isolated(self):
        global_collector = self.collector(FakeReader([([advisory()], {})]))
        private_reader = FakeReader([SourceError('http_403')])
        optional = self.collector(private_reader, repository='kubernetes/kubernetes')
        with self.assertRaisesRegex(SourceError, 'http_403'):
            optional.fetch_page({}, deadline=100)
        self.assertEqual(global_collector.fetch_page({}, deadline=100).events[0]['visibility'], 'public')
        public_repo = self.collector(FakeReader([([advisory()], {})]), repository='kubernetes/kubernetes',
                                     repository_visibility='public')
        self.assertEqual(public_repo.fetch_page({}, deadline=100).events[0]['visibility'], 'public')
        unknown_repo = self.collector(FakeReader([([advisory()], {})]), repository='kubernetes/kubernetes')
        self.assertEqual(unknown_repo.fetch_page({}, deadline=100).events[0]['visibility'], 'unknown')

    def test_repository_descending_overlap_boundary_and_cursor(self):
        reader = FakeReader([])
        collector = self.collector(reader, repository='kubernetes/kubernetes')
        lower, upper = '2026-10-08T22:00:00Z', '2026-10-09T00:00:00Z'
        path = collector.path + '?' + urllib.parse.urlencode({
            'per_page': collector.per_page, 'sort': 'updated', 'direction': 'desc',
            'state': 'published', 'after': 'opaque'})
        reader.pages = [([advisory(updated='2026-10-08T23:00:00Z')],
                         {'link': f'<https://api.github.com{path}>; rel="next"'}),
                        ([advisory(ghsa='GHSA-dddd-eeee-ffff', updated='2026-10-08T21:00:00Z')], {})]
        first = collector.fetch_page({'watermark': '2026-10-08T23:00:00Z',
                                      'bootstrap_cutoff': '2026-10-08T00:00:00Z'}, deadline=100)
        self.assertEqual(first.proposed_cursor['lower'], lower)
        self.assertEqual(first.proposed_cursor['upper'], upper)
        self.assertFalse(first.terminal)
        self.assertEqual(len(first.events), 1)
        second = collector.fetch_page(first.proposed_cursor, deadline=100)
        self.assertTrue(second.terminal)
        self.assertEqual(second.events, ())
        self.assertEqual(second.proposed_cursor['watermark'], upper)
        self.assertIn('state=published', reader.paths[0])

    def test_invalid_continuation_filter_fails_before_request(self):
        reader = FakeReader([])
        collector = self.collector(reader)
        state = {'lower': '2026-10-02T00:00:00Z', 'upper': '2026-10-09T00:00:00Z',
                 'continuation': '/advisories?per_page=30&sort=updated&direction=asc&modified=wrong&after=x'}
        with self.assertRaisesRegex(SourceError, 'invalid_cursor'):
            collector.fetch_page(state, deadline=100)
        self.assertEqual(reader.paths, [])

    def test_external_link_origin_rejected_without_following(self):
        reader = FakeReader([([advisory()], {'link':
            '<https://private-attacker.example/advisories?after=x>; rel="next"'})])
        collector = self.collector(reader)
        with self.assertRaisesRegex(SourceError, 'unsafe_pagination'):
            collector.fetch_page({}, deadline=100)
        self.assertEqual(len(reader.paths), 1)

    def test_meaningful_update_requeues_but_epss_only_does_not(self):
        first = normalize_advisory(advisory(), self.registry, source_key='global_advisories')
        raw = advisory()
        raw['epss'] = {'percentage': 0.9}
        raw['updated_at'] = '2026-10-08T22:00:00Z'
        second = normalize_advisory(raw, self.registry, source_key='global_advisories')
        self.assertNotEqual(first['content_hash'], second['content_hash'])
        self.assertEqual(first['material_hash'], second['material_hash'])
        store = EventStore.open(self.db)
        with store.transaction():
            store.upsert_raw(first)
        self.assertTrue(store.claim([first['event_id']]))
        store.acknowledge([first['event_id']])
        with store.transaction():
            store.upsert_raw(second)
        self.assertEqual(store.pending(), [])
        changed_raw = advisory(severity='high', updated_at='2026-10-08T23:00:00Z')
        changed = normalize_advisory(changed_raw, self.registry, source_key='global_advisories')
        with store.transaction():
            store.upsert_raw(changed)
        self.assertEqual(len(store.pending()), 1)
        self.assertEqual(len(store.revisions(first['event_id'])), 3)
        store.close()

    def test_global_repository_patch_field_equivalence_is_not_material(self):
        global_event = normalize_advisory(advisory(), self.registry, source_key='global_advisories')
        repo_raw = advisory()
        vuln = repo_raw['vulnerabilities'][0]
        vuln['patched_versions'] = vuln.pop('first_patched_version')
        repo_event = normalize_advisory(repo_raw, self.registry,
                                        source_key='repository_advisories:kubernetes/kubernetes',
                                        source_visibility='public')
        self.assertEqual(global_event['material_hash'], repo_event['material_hash'])

    def test_global_primary_wins_optional_repo_observation_without_resend(self):
        global_event = normalize_advisory(advisory(), self.registry, source_key='global_advisories')
        repo_raw = advisory(severity='high')
        repo_event = normalize_advisory(repo_raw, self.registry,
                                        source_key='repository_advisories:kubernetes/kubernetes',
                                        source_visibility='unknown')
        store = EventStore.open(self.db)
        with store.transaction():
            store.upsert_raw(global_event)
        self.assertTrue(store.claim([global_event['event_id']]))
        store.acknowledge([global_event['event_id']])
        with store.transaction():
            store.upsert_raw(repo_event, suppress=True, suppression_reason='visibility_not_public')
        current = json.loads(store.db.execute('SELECT payload_json FROM events').fetchone()[0])
        self.assertEqual(current['severity'], 'critical')
        self.assertEqual(current['visibility'], 'public')
        self.assertEqual(current['provenance']['collector'], 'global_advisories')
        self.assertEqual(store.db.execute('SELECT notification_state FROM notification_outbox').fetchone()[0],
                         'DELIVERED')
        self.assertEqual(len(store.revisions(global_event['event_id'])), 2)
        store.close()

    def test_repo_first_global_replaces_primary_without_format_only_requeue(self):
        raw = advisory()
        global_event = normalize_advisory(raw, self.registry, source_key='global_advisories')
        repo_raw = advisory()
        repo_raw['vulnerabilities'][0]['patched_versions'] = repo_raw['vulnerabilities'][0].pop('first_patched_version')
        repo_event = normalize_advisory(repo_raw, self.registry,
                                        source_key='repository_advisories:kubernetes/kubernetes',
                                        source_visibility='unknown')
        self.assertEqual(repo_event['material_hash'], global_event['material_hash'])
        store = EventStore.open(self.db)
        with store.transaction():
            store.upsert_raw(repo_event, suppress=True, suppression_reason='visibility_not_public')
            store.upsert_raw(global_event)
        current = json.loads(store.db.execute('SELECT payload_json FROM events').fetchone()[0])
        self.assertEqual(current['visibility'], 'public')
        self.assertEqual(current['provenance']['collector'], 'global_advisories')
        self.assertEqual(store.pending(), [])
        self.assertEqual(len(store.revisions(global_event['event_id'])), 2)
        store.close()

    def test_transport_http_error_redacts_body_and_deadline(self):
        class Opener:
            def open(self, request, timeout):
                self.request = request
                raise urllib.error.HTTPError(request.full_url, 403, 'Forbidden', {},
                                             io.BytesIO(b'GH_TOKEN=SECRET_FIXTURE'))
        opener = Opener()
        reader = GitHubReader('SECRET_FIXTURE', opener=opener)
        with self.assertRaises(SourceError) as result:
            reader.get('/advisories', deadline=time.monotonic() + 1)
        self.assertEqual(result.exception.category, 'http_403')
        self.assertNotIn('SECRET_FIXTURE', str(result.exception))
        self.assertEqual(opener.request.get_method(), 'GET')
        with self.assertRaisesRegex(SourceError, 'budget_exhausted'):
            reader.get('/advisories', deadline=time.monotonic() - 1)

    def test_transport_wall_deadline_interrupts_slow_response(self):
        class SlowOpener:
            def open(self, request, timeout):
                time.sleep(0.5)
                raise AssertionError('wall deadline failed to interrupt transport')
        reader = GitHubReader(opener=SlowOpener())
        started = time.monotonic()
        with self.assertRaisesRegex(SourceError, 'budget_exhausted'):
            reader.get('/advisories', deadline=started + 0.05)
        self.assertLess(time.monotonic() - started, 0.4)


if __name__ == '__main__':
    unittest.main()
