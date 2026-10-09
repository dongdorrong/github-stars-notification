"""Opt-in official announcement collectors with page-atomic cursor proposals.

All source text is untrusted. These collectors only return normalized facts and
proposed cursors; callers own transaction commit and delivery policy.
"""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import re
import socket
import ssl
import time
import urllib.parse
import xml.etree.ElementTree as ET
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from typing import Any

from .advisories import instant, stamp
from .release_collector import BudgetExpired, _live_page_deadline
from .signal_collectors import CollectedPage, SourceError, next_api_path

MAX_FEED_BYTES = 1_000_000
_MAINTAINERS = frozenset({'OWNER', 'MEMBER', 'COLLABORATOR'})
_DTD = re.compile(br'<!\s*(?:DOCTYPE|ENTITY)\b', re.IGNORECASE)


def _hash(value: Any) -> str:
    return 'sha256:' + hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                                  separators=(',', ':')).encode()).hexdigest()


def _event(source_id: str, kind: str, policy, *, title: str, body: str,
           url: str, created: str, updated: str, trust: int, reasons: list[str],
           metadata: dict, candidate: bool = True, author: str | None = None,
           association: str | None = None, labels: list[str] | None = None,
           category: str | None = None, visibility: str = 'public') -> dict:
    payload = {'title': title, 'body': body, 'url': url, 'updated_at': updated,
               'metadata': metadata}
    return {
        'schema_version': 'k8s-intelligence-event/v1', 'event_id': source_id,
        'source_id': source_id, 'event_type': kind, 'repository': policy.canonical_name,
        'published_at': created, 'updated_at': updated, 'visibility': visibility,
        'source_trust': trust, 'title': title, 'body': body, 'html_url': url,
        'candidate': candidate, 'author': author, 'author_association': association,
        'labels': labels or [], 'discussion_category': category,
        'metadata': metadata,
        'provenance': {'collector': kind, 'source_url': url,
                       'project': policy.canonical_name, 'trust_reasons': reasons},
        'content_hash': _hash(payload),
    }


def _window(state: dict, now: datetime, overlap_seconds: int) -> tuple[dict, datetime]:
    state = dict(state)
    state.setdefault('bootstrap_cutoff', stamp(now))
    instant(state['bootstrap_cutoff'])
    if state.get('watermark'):
        lower = instant(state['watermark']) - timedelta(seconds=overlap_seconds)
    else:
        lower = now - timedelta(days=7)
    if not state.get('continuation'):
        state['upper'] = stamp(now)
        state['lower'] = stamp(lower)
    else:
        lower = instant(state['lower'])
        instant(state['upper'])
    return state, lower


def _finish(state: dict, continuation: Any, terminal: bool) -> dict:
    state['continuation'] = None if terminal else continuation
    if terminal:
        state['watermark'] = state['upper']
        state['initialized'] = True
    return state


_DISCUSSION_QUERY = '''query AnnouncementDiscussions($owner:String!,$name:String!,$first:Int!,$after:String) {
  repository(owner:$owner,name:$name) {
    discussions(first:$first,after:$after,orderBy:{field:UPDATED_AT,direction:DESC}) {
      edges { cursor node { id databaseId title body url createdAt updatedAt authorAssociation
        author { login } category { name } } }
      pageInfo { hasNextPage endCursor }
    }
  }
}'''


class DiscussionCollector:
    def __init__(self, reader, policy, *, repository_visibility: str = 'unknown', per_page: int = 50,
                 overlap_seconds: int = 3600, utcnow=None):
        if not 1 <= per_page <= 100 or overlap_seconds < 0:
            raise ValueError('invalid discussion collector limits')
        if repository_visibility not in {'public', 'private', 'internal', 'unknown'}:
            raise ValueError('invalid source visibility')
        self.reader, self.policy = reader, policy
        self.visibility = repository_visibility
        self.per_page, self.overlap = per_page, overlap_seconds
        self.utcnow = utcnow or (lambda: datetime.now(timezone.utc))
        self.source_key = 'discussions:' + policy.canonical_name

    def fetch_page(self, state: dict, *, deadline: float) -> CollectedPage:
        if not (self.policy.enabled and self.policy.signals['announcement'] and self.policy.signals['discussions']):
            return CollectedPage(self.source_key, (), dict(state), True, {'pages': 0, 'events': 0})
        categories = set(self.policy.sources.get('discussions', {}).get('categories', []))
        if not categories:
            raise SourceError('missing_discussion_categories')
        state, lower = _window(state, self.utcnow(), self.overlap)
        cursor = state.get('continuation')
        if cursor is not None and (not isinstance(cursor, str) or not cursor or len(cursor) > 500):
            raise SourceError('invalid_cursor')
        owner, name = self.policy.canonical_name.split('/', 1)
        data, _ = self.reader.graphql(_DISCUSSION_QUERY,
                                      {'owner': owner, 'name': name, 'first': self.per_page, 'after': cursor},
                                      deadline=deadline)
        try:
            connection = data['repository']['discussions']
            edges, page_info = connection['edges'], connection['pageInfo']
            if not isinstance(edges, list) or not isinstance(page_info, dict):
                raise TypeError
            events = []
            boundary = False
            for edge in edges:
                node = edge['node']
                updated = instant(node['updatedAt'])
                if updated < lower:
                    boundary = True
                    continue
                category = node['category']['name']
                if category not in categories:
                    continue
                stable = node.get('id') or node.get('databaseId')
                if not isinstance(stable, (str, int)) or not stable:
                    raise TypeError
                source_id = 'github:discussion:' + str(stable)
                events.append(_event(source_id, 'github_discussion', self.policy,
                                     title=str(node.get('title') or ''), body=str(node.get('body') or ''),
                                     url=str(node.get('url') or ''), created=stamp(instant(node['createdAt'])),
                                     updated=stamp(updated), trust=90,
                                     reasons=['explicit_official_discussion_category'],
                                     metadata={'discussion_id': str(stable), 'category': category},
                                     author=(node.get('author') or {}).get('login'),
                                     association=node.get('authorAssociation'), category=category,
                                     visibility=self.visibility))
            has_next = page_info['hasNextPage']
            if not isinstance(has_next, bool):
                raise TypeError
            continuation = page_info.get('endCursor') if has_next else None
            if has_next and (not isinstance(continuation, str) or not continuation or
                             len(continuation) > 500 or continuation == cursor):
                raise TypeError
            terminal = not continuation or boundary
            return CollectedPage(self.source_key, tuple(events), _finish(state, continuation, terminal),
                                 terminal, {'pages': 1, 'events': len(events), 'observed': len(edges)})
        except (KeyError, TypeError, ValueError, AttributeError):
            raise SourceError('invalid_discussion_page') from None


def _validate_issue_cursor(path: Any, expected_path: str, lower: str, per_page: int) -> None:
    """A Link cursor may change only its positive page number, never filters."""
    if not isinstance(path, str) or len(path) > 2000:
        raise SourceError('invalid_cursor')
    try:
        parsed = urllib.parse.urlsplit(path)
    except ValueError:
        raise SourceError('invalid_cursor') from None
    if parsed.scheme or parsed.netloc or parsed.fragment or parsed.path != expected_path:
        raise SourceError('invalid_cursor')
    try:
        pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        raise SourceError('invalid_cursor') from None
    params = dict(pairs)
    expected = {'since': lower, 'sort': 'updated', 'direction': 'desc',
                'per_page': str(per_page), 'state': 'all'}
    if len(pairs) != len(params) or set(params) != set(expected) | {'page'}:
        raise SourceError('invalid_cursor')
    if any(params[key] != value for key, value in expected.items()):
        raise SourceError('invalid_cursor')
    page = params['page']
    if not page.isascii() or not page.isdecimal() or len(page) > 9 or int(page) < 2:
        raise SourceError('invalid_cursor')


class IssueCollector:
    def __init__(self, reader, policy, repository_id: int, *, per_page: int = 100,
                 overlap_seconds: int = 3600, repository_visibility: str = 'unknown', utcnow=None):
        if not isinstance(repository_id, int) or repository_id <= 0 or not 1 <= per_page <= 100 or overlap_seconds < 0:
            raise ValueError('invalid issue collector limits')
        if repository_visibility not in {'public', 'private', 'internal', 'unknown'}:
            raise ValueError('invalid source visibility')
        self.reader, self.policy, self.repository_id = reader, policy, repository_id
        self.visibility = repository_visibility
        self.per_page, self.overlap = per_page, overlap_seconds
        self.utcnow = utcnow or (lambda: datetime.now(timezone.utc))
        self.source_key = 'issues:' + policy.canonical_name
        self.path = '/repos/' + policy.canonical_name + '/issues'

    def fetch_page(self, state: dict, *, deadline: float) -> CollectedPage:
        if not (self.policy.enabled and self.policy.signals['announcement'] and self.policy.signals['issues']):
            return CollectedPage(self.source_key, (), dict(state), True, {'pages': 0, 'events': 0})
        source = self.policy.sources.get('issues', {})
        allow = {v.lower() for v in source.get('allow_labels', [])}
        deny = {v.lower() for v in source.get('deny_labels', [])}
        if not allow:
            raise SourceError('missing_issue_allowlist')
        state, lower = _window(state, self.utcnow(), self.overlap)
        if state.get('continuation') is not None:
            path = state['continuation']
            _validate_issue_cursor(path, self.path, state['lower'], self.per_page)
        else:
            params = {'since': state['lower'], 'sort': 'updated', 'direction': 'desc',
                      'per_page': str(self.per_page), 'state': 'all'}
            path = self.path + '?' + urllib.parse.urlencode(params)
        raw, headers = self.reader.get(path, deadline=deadline)
        if not isinstance(raw, list):
            raise SourceError('invalid_issue_page')
        try:
            events = []
            boundary = False
            for issue in raw:
                if not isinstance(issue, dict):
                    raise TypeError
                if 'pull_request' in issue:
                    continue
                updated = instant(issue['updated_at'])
                if updated < lower:
                    boundary = True
                    continue
                labels = [str(v['name']).lower() for v in issue.get('labels', [])]
                if deny.intersection(labels) or not allow.intersection(labels):
                    continue
                number = issue['number']
                if not isinstance(number, int) or number <= 0:
                    raise TypeError
                association = str(issue.get('author_association') or '').upper()
                trusted = association in _MAINTAINERS
                source_id = f'github:issue:{self.repository_id}:{number}'
                events.append(_event(source_id, 'github_issue', self.policy,
                                     title=str(issue.get('title') or ''), body=str(issue.get('body') or ''),
                                     url=str(issue.get('html_url') or ''),
                                     created=stamp(instant(issue['created_at'])), updated=stamp(updated),
                                     trust=85 if trusted else 30,
                                     reasons=['explicit_label', 'maintainer_association'] if trusted else
                                             ['explicit_label', 'unverified_author'],
                                     candidate=trusted,
                                     metadata={'repository_id': self.repository_id, 'issue_number': number,
                                               'labels': labels},
                                     author=(issue.get('user') or {}).get('login'),
                                     association=association, labels=labels,
                                     visibility=self.visibility))
            continuation = next_api_path(headers, self.path)
            if continuation:
                try:
                    _validate_issue_cursor(continuation, self.path, state['lower'], self.per_page)
                except SourceError:
                    raise SourceError('invalid_issue_page') from None
            terminal = not continuation or boundary
            return CollectedPage(self.source_key, tuple(events), _finish(state, continuation, terminal),
                                 terminal, {'pages': 1, 'events': len(events), 'observed': len(raw)})
        except (KeyError, TypeError, ValueError, AttributeError):
            raise SourceError('invalid_issue_page') from None


def _safe_url(url: str, resolver) -> tuple[str, str, int, str]:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.fragment or parsed.query:
        raise SourceError('unsafe_feed_url')
    try:
        port = parsed.port or 443
    except ValueError:
        raise SourceError('unsafe_feed_url') from None
    if port != 443 or not re.fullmatch(r'[A-Za-z0-9.-]+', parsed.hostname):
        raise SourceError('unsafe_feed_url')
    try:
        addresses = resolver(parsed.hostname, port, type=socket.SOCK_STREAM)
        ips = [address[4][0] for address in addresses]
        if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
            raise SourceError('unsafe_feed_address')
    except (OSError, ValueError, IndexError, TypeError):
        raise SourceError('unsafe_feed_address') from None
    return parsed.hostname, ips[0], port, parsed.path or '/'


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host: str, ip: str, *, timeout: float):
        super().__init__(host, timeout=timeout, context=ssl.create_default_context())
        self._ip = ip

    def connect(self):
        sock = socket.create_connection((self._ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class SafeRSSReader:
    """Resolve/validate each hop, pin the resolved IP, and never forward secrets."""
    def __init__(self, *, resolver=socket.getaddrinfo, transport=None, clock=time.monotonic):
        self.resolver, self.transport, self.clock = resolver, transport, clock

    def fetch(self, url: str, *, deadline: float, etag: str | None = None,
              last_modified: str | None = None, allowed_hosts: frozenset[str] = frozenset()) -> tuple[int, dict, bytes, str]:
        current = url
        for _ in range(4):
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise SourceError('budget_exhausted')
            try:
                # Live DNS resolution, connect, TLS handshake and body read share
                # one wall deadline. Injected fixture transports retain their
                # deterministic clock and do not replace a process alarm.
                guard = _live_page_deadline(remaining) if self.transport is None else nullcontext()
                with guard:
                    host, ip, _port, path = _safe_url(current, self.resolver)
                    if host not in allowed_hosts:
                        raise SourceError('rss_redirect_not_allowlisted')
                    remaining = deadline - self.clock()
                    if remaining <= 0:
                        raise SourceError('budget_exhausted')
                    headers = {'User-Agent': 'starwatch-intelligence', 'Accept': 'application/rss+xml, application/atom+xml, application/xml, text/xml'}
                    if etag:
                        headers['If-None-Match'] = etag
                    if last_modified:
                        headers['If-Modified-Since'] = last_modified
                    if self.transport is not None:
                        status, response_headers, body = self.transport(current, ip, headers, min(10, remaining))
                    else:
                        connection = _PinnedHTTPS(host, ip, timeout=min(10, remaining))
                        try:
                            connection.request('GET', path, headers=headers)
                            response = connection.getresponse()
                            status = response.status
                            response_headers = dict(response.getheaders())
                            body = response.read(MAX_FEED_BYTES + 1)
                        finally:
                            connection.close()
            except BudgetExpired:
                raise SourceError('budget_exhausted') from None
            except RuntimeError:
                raise SourceError('budget_unavailable') from None
            except (OSError, TimeoutError, ssl.SSLError, http.client.HTTPException):
                raise SourceError('rss_transport_error') from None
            response_headers = {str(k).lower(): str(v) for k, v in response_headers.items()}
            if status in (301, 302, 303, 307, 308):
                location = response_headers.get('location')
                if not location:
                    raise SourceError('rss_invalid_redirect')
                current = urllib.parse.urljoin(current, location)
                continue
            if len(body) > MAX_FEED_BYTES:
                raise SourceError('rss_too_large')
            return status, response_headers, body, current
        raise SourceError('rss_redirect_limit')


def _text(node, path: str, namespaces: dict | None = None) -> str:
    found = node.find(path, namespaces or {})
    return ''.join(found.itertext()).strip() if found is not None else ''


class _NoDTDBuilder(ET.TreeBuilder):
    def doctype(self, name, pubid, system):
        # Byte-pattern checks do not catch UTF-16/UTF-32 declarations. Expat
        # invokes this callback after decoding any supported XML encoding.
        raise SourceError('rss_unsafe_xml')


def _parse_feed(raw: bytes, feed_url: str) -> list[dict]:
    if _DTD.search(raw):
        raise SourceError('rss_unsafe_xml')
    try:
        root = ET.fromstring(raw, parser=ET.XMLParser(target=_NoDTDBuilder()))
    except ET.ParseError:
        raise SourceError('rss_invalid_xml') from None
    entries = []
    if root.tag.lower() == 'rss' or root.tag.lower().endswith('}rss'):
        nodes = root.findall('./channel/item')
        for item in nodes:
            guid = _text(item, 'guid') or _text(item, 'link')
            if not guid:
                continue
            entries.append({'guid': guid, 'title': _text(item, 'title'),
                            'body': _text(item, 'description'), 'url': _text(item, 'link') or feed_url,
                            'published': _text(item, 'pubDate')})
    elif root.tag.lower().endswith('feed'):
        ns = {'a': 'http://www.w3.org/2005/Atom'}
        for item in root.findall('a:entry', ns):
            guid = _text(item, 'a:id', ns)
            links = item.findall('a:link', ns)
            url = next((link.get('href') for link in links if link.get('rel', 'alternate') == 'alternate'), '')
            guid = guid or url
            if not guid:
                continue
            entries.append({'guid': guid, 'title': _text(item, 'a:title', ns),
                            'body': _text(item, 'a:summary', ns) or _text(item, 'a:content', ns),
                            'url': url or feed_url,
                            'published': _text(item, 'a:published', ns) or _text(item, 'a:updated', ns)})
    else:
        raise SourceError('rss_invalid_xml')
    return entries


def _rss_timestamp(value: str, now: datetime) -> str:
    if not value:
        return stamp(now)
    try:
        return stamp(instant(value))
    except SourceError:
        try:
            from email.utils import parsedate_to_datetime
            parsed = parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                raise ValueError
            return stamp(parsed)
        except (TypeError, ValueError):
            raise SourceError('rss_invalid_timestamp') from None


def _official_item_url(value: str, feed_url: str, allowed_hosts: frozenset[str]) -> str:
    """An item cannot smuggle an arbitrary link into a trusted message."""
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme != 'https' or parsed.hostname not in allowed_hosts or
            parsed.username or parsed.password or parsed.fragment):
        return feed_url
    try:
        if parsed.port not in (None, 443):
            return feed_url
    except ValueError:
        return feed_url
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, '', ''))


class RSSCollector:
    def __init__(self, policy, *, reader: SafeRSSReader | None = None,
                 per_page: int = 50, public_only: bool = False, utcnow=None):
        if per_page < 1 or per_page > 100:
            raise ValueError('invalid RSS page size')
        self.policy, self.reader, self.per_page = policy, reader or SafeRSSReader(), per_page
        self.public_only = public_only
        self.utcnow = utcnow or (lambda: datetime.now(timezone.utc))
        self.source_key = 'rss:' + policy.canonical_name

    def fetch_page(self, state: dict, *, deadline: float) -> CollectedPage:
        if not (self.policy.enabled and self.policy.signals['announcement'] and self.policy.signals['rss']):
            return CollectedPage(self.source_key, (), dict(state), True, {'pages': 0, 'events': 0})
        configured = self.policy.sources.get('rss', [])
        if not configured:
            raise SourceError('missing_rss_sources')
        state = dict(state)
        state.setdefault('bootstrap_cutoff', stamp(self.utcnow()))
        instant(state['bootstrap_cutoff'])
        index = state.get('feed_index', 0)
        if not isinstance(index, int) or not 0 <= index < len(configured):
            raise SourceError('invalid_cursor')
        if self.public_only:
            # Keep persisted indices in the original registry order. Filtering
            # the list in CLI reinterprets feed_index and can skip or fetch a
            # different (possibly private) source in preview.
            public_indices = [i for i, item in enumerate(configured)
                              if item.get('visibility') == 'public']
            if not public_indices:
                return CollectedPage(self.source_key, (), state, True,
                                     {'pages': 0, 'events': 0, 'observed': 0})
            selected = next((i for i in public_indices if i >= index), public_indices[0])
            if selected != index:
                state['item_index'] = 0
            index = selected
        source = configured[index]
        if source.get('official') is not True:
            raise SourceError('untrusted_rss_source')
        url = source['url']
        host = urllib.parse.urlsplit(url).hostname
        allowed = frozenset({host, *(source.get('redirect_hosts') or [])})
        cache = dict(state.get('http_cache') or {})
        prior = cache.get(str(index), {})
        offset = state.get('item_index', 0)
        if not isinstance(offset, int) or offset < 0:
            raise SourceError('invalid_cursor')
        status, headers, raw, resolved = self.reader.fetch(
            url, deadline=deadline, etag=prior.get('etag') if offset == 0 else None,
            last_modified=prior.get('last_modified') if offset == 0 else None,
            allowed_hosts=allowed)
        terminal = True
        if status == 304:
            events = []
        elif status == 200:
            media_type = headers.get('content-type', '').lower()
            if not any(value in media_type for value in ('xml', 'rss', 'atom')):
                raise SourceError('rss_invalid_content_type')
            entries = _parse_feed(raw, url)
            events = []
            for entry in entries[offset:offset + self.per_page]:
                source_id = 'rss:' + hashlib.sha256((url + '\x00' + entry['guid']).encode()).hexdigest()
                published = _rss_timestamp(entry['published'], self.utcnow())
                events.append(_event(source_id, 'official_rss', self.policy,
                                     title=entry['title'], body=entry['body'],
                                     url=_official_item_url(entry['url'], url, allowed),
                                     created=published, updated=published, trust=95,
                                     reasons=['explicit_official_https_rss'],
                                     metadata={'feed_url': url, 'guid': entry['guid'],
                                               'resolved_feed_url': resolved},
                                     visibility=source.get('visibility', 'unknown')))
            terminal = offset + self.per_page >= len(entries)
            if terminal:
                cache[str(index)] = {'etag': headers.get('etag'), 'last_modified': headers.get('last-modified')}
        else:
            raise SourceError('rss_http_status')
        state['http_cache'] = cache
        if terminal:
            state['feed_index'] = (index + 1) % len(configured)
            state['item_index'] = 0
            state['initialized'] = True
        else:
            # Re-read one overlapping item: shifting feed boundaries cannot
            # skip the first item of the next page; stable IDs deduplicate it.
            state['item_index'] = offset + self.per_page - (1 if self.per_page > 1 else 0)
        return CollectedPage(self.source_key, tuple(events), state, terminal,
                             {'pages': 1, 'events': len(events), 'observed': len(events)})
