from __future__ import annotations

import json
import tempfile
import time
import unittest
import urllib.parse
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from starwatch.announcements import (
    DiscussionCollector,
    IssueCollector,
    RSSCollector,
    SafeRSSReader,
)
from starwatch.event_store import EventStore
from starwatch.registry import load_registry
from starwatch.release_collector import BudgetExpired
from starwatch.signal_collectors import SourceError

NOW = datetime(2026, 10, 9, 0, 0, tzinfo=timezone.utc)
URL = 'https://project.example.org/feed.xml'


def policy(*, discussion=False, issue=False, rss=False):
    base = load_registry('config/projects.yaml').resolve('argoproj/argo-cd')
    signals = dict(base.signals)
    signals.update(announcement=True, discussions=discussion, issues=issue, rss=rss)
    sources = {'discussions': {'categories': ['Announcements']},
               'issues': {'allow_labels': ['security', 'announcement'], 'deny_labels': ['question']},
               'rss': [{'url': URL, 'official': True, 'visibility': 'public'}]}
    return replace(base, signals=signals, sources=sources)


class FakeGitHub:
    def __init__(self, *, data=None, pages=None):
        self.data, self.pages, self.calls = data, pages or [], []

    def graphql(self, query, variables, *, deadline):
        self.calls.append(('graphql', query, variables))
        return self.data, {}

    def get(self, path, *, deadline):
        self.calls.append(('get', path))
        return self.pages.pop(0)


def discussion_node(**overrides):
    raw = {'id': 'D_123', 'databaseId': 123, 'title': 'Security announcement',
           'body': 'Important update', 'url': 'https://github.com/argoproj/argo-cd/discussions/123',
           'createdAt': '2026-10-08T00:00:00Z', 'updatedAt': '2026-10-08T01:00:00Z',
           'authorAssociation': 'MEMBER', 'author': {'login': 'maintainer'},
           'category': {'name': 'Announcements'}}
    raw.update(overrides)
    return raw


def issue_node(**overrides):
    raw = {'number': 42, 'title': 'Breaking upgrade', 'body': 'Review before upgrading',
           'html_url': 'https://github.com/argoproj/argo-cd/issues/42',
           'created_at': '2026-10-08T00:00:00Z', 'updated_at': '2026-10-08T01:00:00Z',
           'author_association': 'MEMBER', 'user': {'login': 'maintainer'},
           'labels': [{'name': 'announcement'}]}
    raw.update(overrides)
    return raw


class AnnouncementTest(unittest.TestCase):
    def test_discussion_official_category_and_provenance(self):
        reader = FakeGitHub(data={'repository': {'discussions': {
            'edges': [{'node': discussion_node()}, {'node': discussion_node(id='D_456', category={'name': 'Q&A'})}],
            'pageInfo': {'hasNextPage': False, 'endCursor': None}}}})
        page = DiscussionCollector(reader, policy(discussion=True), utcnow=lambda: NOW).fetch_page({}, deadline=999)
        self.assertTrue(page.terminal)
        self.assertEqual([e['event_id'] for e in page.events], ['github:discussion:D_123'])
        self.assertEqual(page.events[0]['source_trust'], 90)
        self.assertEqual(page.events[0]['visibility'], 'unknown')
        self.assertEqual(page.proposed_cursor['bootstrap_cutoff'], '2026-10-09T00:00:00Z')
        self.assertEqual(page.events[0]['author_association'], 'MEMBER')
        self.assertIn('source_url', page.events[0]['provenance'])
        self.assertIn('orderBy:{field:UPDATED_AT', reader.calls[0][1])

    def test_unconfigured_discussion_does_not_call_network(self):
        reader = FakeGitHub()
        page = DiscussionCollector(reader, policy(), utcnow=lambda: NOW).fetch_page({}, deadline=999)
        self.assertEqual(page.events, ())
        self.assertEqual(reader.calls, [])

    def test_discussion_failed_page_does_not_advance_cursor(self):
        reader = FakeGitHub(data={'repository': None})
        state = {'watermark': '2026-10-08T00:00:00Z'}
        with self.assertRaises(SourceError):
            DiscussionCollector(reader, policy(discussion=True), utcnow=lambda: NOW).fetch_page(state, deadline=999)
        self.assertEqual(state, {'watermark': '2026-10-08T00:00:00Z'})

    def test_discussion_page_info_requires_cursor_when_has_next(self):
        state = {'watermark': '2026-10-08T00:00:00Z'}
        for page_info in (
            {'hasNextPage': True, 'endCursor': None},
            {'hasNextPage': True, 'endCursor': ''},
            {'hasNextPage': 'true', 'endCursor': 'next'},
        ):
            with self.subTest(page_info=page_info):
                reader = FakeGitHub(data={'repository': {'discussions': {
                    'edges': [{'node': discussion_node()}], 'pageInfo': page_info}}})
                with self.assertRaisesRegex(SourceError, 'invalid_discussion_page'):
                    DiscussionCollector(reader, policy(discussion=True), utcnow=lambda: NOW).fetch_page(
                        state, deadline=999)
                self.assertEqual(state, {'watermark': '2026-10-08T00:00:00Z'})
        reader = FakeGitHub(data={'repository': {'discussions': {
            'edges': [{'node': discussion_node()}],
            'pageInfo': {'hasNextPage': False, 'endCursor': {'ignored': 'value'}}}}})
        page = DiscussionCollector(reader, policy(discussion=True), utcnow=lambda: NOW).fetch_page(
            state, deadline=999)
        self.assertTrue(page.terminal)

    def test_issue_labeled_maintainer_only_and_pr_excluded(self):
        raw = [issue_node(), issue_node(number=43, labels=[]),
               issue_node(number=44, labels=[{'name': 'question'}, {'name': 'announcement'}]),
               issue_node(number=45, pull_request={'url': 'untrusted'}),
               issue_node(number=46, author_association='NONE')]
        reader = FakeGitHub(pages=[(raw, {})])
        page = IssueCollector(reader, policy(issue=True), 120896210, utcnow=lambda: NOW).fetch_page({}, deadline=999)
        self.assertEqual([e['event_id'] for e in page.events],
                         ['github:issue:120896210:42', 'github:issue:120896210:46'])
        self.assertEqual([e['candidate'] for e in page.events], [True, False])
        self.assertEqual([e['source_trust'] for e in page.events], [85, 30])
        self.assertTrue(all(e['visibility'] == 'unknown' for e in page.events))
        self.assertEqual(page.proposed_cursor['bootstrap_cutoff'], '2026-10-09T00:00:00Z')
        self.assertIn('since=2026-10-02', reader.calls[0][1])

    def test_issue_disabled_does_not_call_network(self):
        reader = FakeGitHub()
        page = IssueCollector(reader, policy(), 1, utcnow=lambda: NOW).fetch_page({}, deadline=999)
        self.assertEqual(reader.calls, [])
        self.assertEqual(page.events, ())

    def test_issue_update_preserves_stable_id_and_changes_hash(self):
        reader = FakeGitHub(pages=[([issue_node(body='one')], {}), ([issue_node(body='two')], {})])
        collector = IssueCollector(reader, policy(issue=True), 1, utcnow=lambda: NOW)
        a = collector.fetch_page({}, deadline=999).events[0]
        b = collector.fetch_page({}, deadline=999).events[0]
        self.assertEqual(a['event_id'], b['event_id'])
        self.assertNotEqual(a['content_hash'], b['content_hash'])

    def test_issue_cursor_rejects_filter_changes_and_extra_query(self):
        lower = '2026-10-08T00:00:00Z'
        base = '/repos/argoproj/argo-cd/issues?'
        expected = {'since': lower, 'sort': 'updated', 'direction': 'desc',
                    'per_page': '100', 'state': 'all', 'page': '2'}
        valid = base + urllib.parse.urlencode(expected)
        state = {'lower': lower, 'upper': '2026-10-09T00:00:00Z',
                 'bootstrap_cutoff': '2026-10-08T00:00:00Z',
                 'continuation': valid, 'watermark': lower}
        variants = [
            {**expected, 'since': '2020-01-01T00:00:00Z'},
            {**expected, 'sort': 'created'},
            {**expected, 'direction': 'asc'},
            {**expected, 'state': 'closed'},
            {**expected, 'per_page': '1'},
            {**expected, 'page': '0'},
            {**expected, 'extra': 'x'},
        ]
        variants.append({key: value for key, value in expected.items() if key != 'since'})
        for params in variants:
            with self.subTest(params=params):
                reader = FakeGitHub()
                candidate = {**state, 'continuation': base + urllib.parse.urlencode(params)}
                with self.assertRaisesRegex(SourceError, 'invalid_cursor'):
                    IssueCollector(reader, policy(issue=True), 1, utcnow=lambda: NOW).fetch_page(
                        candidate, deadline=999)
                self.assertEqual(reader.calls, [])
                self.assertEqual(candidate['watermark'], lower)
        reader = FakeGitHub(pages=[([issue_node()], {})])
        page = IssueCollector(reader, policy(issue=True), 1, utcnow=lambda: NOW).fetch_page(
            state, deadline=999)
        self.assertEqual(len(page.events), 1)
        self.assertEqual(reader.calls[0][1], valid)

    def test_issue_remote_link_cannot_advance_with_changed_filters(self):
        malicious = 'https://api.github.com/repos/argoproj/argo-cd/issues?page=2&per_page=100&sort=created'
        reader = FakeGitHub(pages=[([issue_node()], {'link': f'<{malicious}>; rel="next"'})])
        state = {'watermark': '2026-10-08T00:00:00Z'}
        with self.assertRaisesRegex(SourceError, 'invalid_issue_page'):
            IssueCollector(reader, policy(issue=True), 1, utcnow=lambda: NOW).fetch_page(
                state, deadline=999)
        self.assertEqual(state, {'watermark': '2026-10-08T00:00:00Z'})

    def test_rss_official_normalizes_stable_id_and_etag(self):
        xml = b'<rss><channel><item><guid>stable-1</guid><title>Announcement</title><description>Release</description><link>https://project.example.org/news/1</link><pubDate>Thu, 08 Oct 2026 00:00:00 GMT</pubDate></item></channel></rss>'
        calls = []
        def transport(url, ip, headers, timeout):
            calls.append(headers)
            return 200, {'content-type': 'application/rss+xml', 'etag': '"abc"'}, xml
        reader = SafeRSSReader(resolver=lambda *a, **k: [(None, None, None, None, ('93.184.216.34', 443))],
                               transport=transport)
        collector = RSSCollector(policy(rss=True), reader=reader, utcnow=lambda: NOW)
        first = collector.fetch_page({}, deadline=time.monotonic()+10)
        second = collector.fetch_page(first.proposed_cursor, deadline=time.monotonic()+10)
        self.assertEqual(first.events[0]['event_id'], second.events[0]['event_id'])
        self.assertEqual(first.events[0]['source_trust'], 95)
        self.assertEqual(first.events[0]['visibility'], 'public')
        self.assertEqual(first.proposed_cursor['bootstrap_cutoff'], '2026-10-09T00:00:00Z')
        self.assertEqual(first.proposed_cursor['http_cache']['0']['etag'], '"abc"')
        self.assertEqual(calls[1]['If-None-Match'], '"abc"')

    def test_rss_ssrf_redirect_escape_size_and_xml_are_blocked(self):
        private = SafeRSSReader(resolver=lambda *a, **k: [(None, None, None, None, ('127.0.0.1', 443))],
                                transport=lambda *a: (200, {}, b''))
        with self.assertRaisesRegex(SourceError, 'unsafe_feed_address'):
            private.fetch(URL, deadline=time.monotonic()+10, allowed_hosts=frozenset({'project.example.org'}))
        def public(*args, **kwargs):
            return [(None, None, None, None, ('93.184.216.34', 443))]
        redirect = SafeRSSReader(resolver=public, transport=lambda *a: (302, {'location': 'https://evil.example.org/feed'}, b''))
        with self.assertRaisesRegex(SourceError, 'rss_redirect_not_allowlisted'):
            redirect.fetch(URL, deadline=time.monotonic()+10, allowed_hosts=frozenset({'project.example.org'}))
        oversized = SafeRSSReader(resolver=public, transport=lambda *a: (200, {'content-type': 'application/xml'}, b'x'*1_000_001))
        with self.assertRaisesRegex(SourceError, 'rss_too_large'):
            RSSCollector(policy(rss=True), reader=oversized, utcnow=lambda: NOW).fetch_page({}, deadline=time.monotonic()+10)
        unsafe_xml = SafeRSSReader(resolver=public, transport=lambda *a: (200, {'content-type': 'application/xml'}, b'<!DOCTYPE rss><rss/>'))
        with self.assertRaisesRegex(SourceError, 'rss_unsafe_xml'):
            RSSCollector(policy(rss=True), reader=unsafe_xml, utcnow=lambda: NOW).fetch_page({}, deadline=time.monotonic()+10)
        utf16 = '<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE rss [<!ENTITY x "EXPAND">]><rss><channel><item><guid>g</guid><title>&x;</title></item></channel></rss>'.encode('utf-16')
        encoded_xml = SafeRSSReader(resolver=public, transport=lambda *a: (200, {'content-type': 'application/xml'}, utf16))
        with self.assertRaisesRegex(SourceError, 'rss_unsafe_xml'):
            RSSCollector(policy(rss=True), reader=encoded_xml, utcnow=lambda: NOW).fetch_page({}, deadline=time.monotonic()+10)

    def test_live_rss_deadline_covers_dns_and_maps_budget_errors(self):
        class ExpiringGuard:
            def __enter__(self):
                raise BudgetExpired()
            def __exit__(self, *args):
                return False

        reader = SafeRSSReader(resolver=lambda *a, **k: self.fail('DNS should be under alarm'))
        with (
            patch('starwatch.announcements._live_page_deadline', return_value=ExpiringGuard()) as guard,
            self.assertRaisesRegex(SourceError, 'budget_exhausted'),
        ):
            reader.fetch(URL, deadline=time.monotonic()+10, allowed_hosts=frozenset({'project.example.org'}))
        guard.assert_called_once()
        with (
            patch('starwatch.announcements._live_page_deadline', side_effect=RuntimeError('active alarm')),
            self.assertRaisesRegex(SourceError, 'budget_unavailable'),
        ):
            reader.fetch(URL, deadline=time.monotonic()+10, allowed_hosts=frozenset({'project.example.org'}))

    def test_rss_disabled_does_not_call_network(self):
        reader = SafeRSSReader(transport=lambda *a: self.fail('network called'))
        page = RSSCollector(policy(), reader=reader).fetch_page({}, deadline=0)
        self.assertEqual(page.events, ())

    def test_rss_without_explicit_public_visibility_is_unknown(self):
        xml = b'<rss><channel><item><guid>stable</guid><title>News</title></item></channel></rss>'
        reader = SafeRSSReader(
            resolver=lambda *a, **k: [(None, None, None, None, ('93.184.216.34', 443))],
            transport=lambda *a: (200, {'content-type': 'application/rss+xml'}, xml))
        configured = policy(rss=True)
        configured.sources['rss'][0].pop('visibility')
        event = RSSCollector(configured, reader=reader, utcnow=lambda: NOW).fetch_page(
            {}, deadline=time.monotonic()+10).events[0]
        self.assertEqual(event['visibility'], 'unknown')

    def test_preview_public_only_preserves_original_feed_indices_and_state(self):
        private_url = 'https://private.example.org/feed.xml'
        configured = policy(rss=True)
        configured.sources['rss'].insert(0, {'url': private_url, 'official': True,
                                              'visibility': 'private'})
        xml = b'<rss><channel><item><guid>public-guid</guid><title>News</title></item></channel></rss>'
        requests = []

        def transport(url, ip, headers, timeout):
            requests.append(url)
            return 200, {'content-type': 'application/rss+xml'}, xml

        reader = SafeRSSReader(
            resolver=lambda *a, **k: [(None, None, None, None, ('93.184.216.34', 443))],
            transport=transport)
        collector = RSSCollector(configured, reader=reader, public_only=True, utcnow=lambda: NOW)
        saved = {'feed_index': 1, 'item_index': 0,
                 'http_cache': {'1': {'etag': None, 'last_modified': None}}}
        before = json.dumps(saved, sort_keys=True)
        page = collector.fetch_page(saved, deadline=time.monotonic()+10)
        self.assertEqual(requests, [URL])
        self.assertEqual(page.events[0]['visibility'], 'public')
        self.assertEqual(json.dumps(saved, sort_keys=True), before)
        self.assertEqual(page.proposed_cursor['feed_index'], 0)  # Original-index cycle, preview-only.

        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / 'events.sqlite3'
            store = EventStore.open(db_path)
            with store.transaction():
                store.set_meta('signal_cursor:' + collector.source_key, before)
            store.close()
            db_before = db_path.read_bytes()
            preview = EventStore.open(db_path, preview=True)
            cursor = json.loads(preview.get_meta('signal_cursor:' + collector.source_key))
            in_memory_page = collector.fetch_page(cursor, deadline=time.monotonic()+10)
            with preview.transaction():
                preview.set_meta('signal_cursor:' + collector.source_key,
                                 json.dumps(in_memory_page.proposed_cursor))
            preview.close()
            self.assertEqual(db_path.read_bytes(), db_before)

        requests.clear()
        saved = {'feed_index': 0, 'item_index': 7}
        before = json.dumps(saved, sort_keys=True)
        page = collector.fetch_page(saved, deadline=time.monotonic()+10)
        self.assertEqual(requests, [URL])
        self.assertEqual(len(page.events), 1)  # Private feed offset must not skip the public item.
        self.assertEqual(json.dumps(saved, sort_keys=True), before)

    def test_source_visibility_is_inventory_supplied_not_assumed_public(self):
        discussion_reader = FakeGitHub(data={'repository': {'discussions': {
            'edges': [{'node': discussion_node()}],
            'pageInfo': {'hasNextPage': False, 'endCursor': None}}}})
        event = DiscussionCollector(discussion_reader, policy(discussion=True),
                                    repository_visibility='private', utcnow=lambda: NOW).fetch_page({}, deadline=999).events[0]
        self.assertEqual(event['visibility'], 'private')
        issue_reader = FakeGitHub(pages=[([issue_node()], {})])
        event = IssueCollector(issue_reader, policy(issue=True), 1,
                               repository_visibility='internal', utcnow=lambda: NOW).fetch_page({}, deadline=999).events[0]
        self.assertEqual(event['visibility'], 'internal')


if __name__ == '__main__':
    unittest.main()
