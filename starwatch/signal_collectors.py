"""Page-atomic read-only collector contract and bounded GitHub HTTP transport."""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Protocol

from .release_collector import BudgetExpired, _live_page_deadline

API_VERSION = '2022-11-28'


class SourceError(Exception):
    def __init__(self, category: str):
        self.category = category
        super().__init__(category)


@dataclass(frozen=True)
class CollectedPage:
    source_key: str
    events: tuple[dict, ...]
    proposed_cursor: dict
    terminal: bool
    metrics: dict = field(default_factory=dict)


class Collector(Protocol):
    source_key: str
    def fetch_page(self, state: dict, *, deadline: float) -> CollectedPage: ...


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class GitHubReader:
    """Only API GET and fixed read-only GraphQL query operations are exposed."""
    def __init__(self, token: str | None = None, *, opener=None, clock=time.monotonic):
        self._token = token
        self._opener = opener or urllib.request.build_opener(NoRedirect)
        self.clock = clock

    def get(self, path: str, *, deadline: float):
        if not path.startswith('/') or path.startswith('//'):
            raise SourceError('invalid_api_path')
        return self._request(path, deadline=deadline)

    def graphql(self, query: str, variables: dict, *, deadline: float):
        if not query.lstrip().startswith('query ') or re.search(r'\bmutation\b', query):
            raise SourceError('write_forbidden')
        result, headers = self._request('/graphql', deadline=deadline,
                                       payload={'query': query, 'variables': variables})
        if not isinstance(result, dict) or result.get('errors'):
            raise SourceError('graphql_unavailable')
        return result.get('data'), headers

    def _request(self, path, *, deadline, payload=None):
        remaining = deadline - self.clock()
        if remaining <= 0:
            raise SourceError('budget_exhausted')
        headers = {'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': API_VERSION,
                   'User-Agent': 'starwatch-intelligence'}
        if self._token:
            headers['Authorization'] = 'Bearer ' + self._token
        if payload is not None:
            headers['Content-Type'] = 'application/json'
        request = urllib.request.Request('https://api.github.com' + path, headers=headers,
                                         data=json.dumps(payload).encode() if payload is not None else None)
        try:
            # socket timeout bounds idle I/O, while the alarm bounds the
            # complete request+body read against the caller's wall budget.
            with _live_page_deadline(remaining):
                with self._opener.open(request, timeout=min(15, remaining)) as response:
                    raw = response.read(8_000_001)
                    if len(raw) > 8_000_000:
                        raise SourceError('response_too_large')
                    data = json.loads(raw)
                    return data, {k.lower(): v for k, v in response.headers.items()}
        except BudgetExpired:
            raise SourceError('budget_exhausted') from None
        except RuntimeError:
            raise SourceError('budget_unavailable') from None
        except urllib.error.HTTPError as exc:
            try:
                if exc.code == 429 or (exc.code == 403 and
                        (exc.headers.get('Retry-After') or exc.headers.get('X-RateLimit-Remaining') == '0')):
                    raise SourceError('rate_limited') from None
                raise SourceError(f'http_{exc.code}') from None
            finally:
                exc.close()
        except SourceError:
            raise
        except (OSError, ValueError):
            raise SourceError('transport_error') from None


def next_api_path(headers: dict, expected_path: str) -> str | None:
    """Never forward credentials to a Link origin or endpoint supplied by a source."""
    for part in headers.get('link', '').split(','):
        match = re.search(r'<([^>]+)>;\s*rel="next"', part)
        if match:
            parsed = urllib.parse.urlsplit(match.group(1))
            if (parsed.scheme != 'https' or parsed.netloc != 'api.github.com'
                    or parsed.path != expected_path or parsed.fragment):
                raise SourceError('unsafe_pagination')
            return parsed.path + ('?' + parsed.query if parsed.query else '')
    return None
