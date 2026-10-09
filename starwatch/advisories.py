"""Bounded published GitHub advisory collection with durable window continuation."""
from __future__ import annotations

import hashlib
import json
import re
import urllib.parse
from datetime import datetime, timedelta, timezone

from .signal_collectors import CollectedPage, SourceError, next_api_path


def instant(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if result.tzinfo is None:
            raise ValueError
        return result.astimezone(timezone.utc)
    except (ValueError, AttributeError):
        raise SourceError('invalid_timestamp') from None


def stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def github_repository(url: object) -> str | None:
    if not isinstance(url, str):
        return None
    try:
        parsed = urllib.parse.urlsplit(url)
        parts = parsed.path.strip('/').split('/')
        safe_host = (parsed.scheme == 'https' and not parsed.username and not parsed.password
                     and not parsed.port and parsed.hostname in {'github.com', 'api.github.com'})
    except ValueError:
        return None
    if not safe_host:
        return None
    if parsed.hostname == 'api.github.com':
        if len(parts) < 4 or parts[0] != 'repos':
            return None
        parts = parts[1:]
    if len(parts) < 2:
        return None
    name = '/'.join(parts[:2]).removesuffix('.git').lower()
    return name if re.fullmatch(r'[a-z0-9_.-]+/[a-z0-9_.-]+', name) else None


def map_project(raw: dict, registry) -> dict:
    # Stronger official source evidence wins over weaker package/reference
    # matches; a conflicting weak reference must not suppress a certain URL.
    for field in ('repository_advisory_url', 'source_code_location'):
        name = github_repository(raw.get(field))
        policy = registry.resolve(name) if name else None
        if policy:
            return {'status': 'mapped', 'project': policy.canonical_name,
                    'reason': field, 'confidence': 'deterministic'}
    candidates = {}
    for vuln in raw.get('vulnerabilities') or []:
        if not isinstance(vuln, dict):
            continue
        package = vuln.get('package') or {}
        if not isinstance(package, dict):
            continue
        for name, policy in registry.projects.items():
            if any(p['ecosystem'].lower() == str(package.get('ecosystem', '')).lower()
                   and p['name'].lower() == str(package.get('name', '')).lower() for p in policy.packages):
                candidates.setdefault(name, 'package_map')
    if len(candidates) == 1:
        name = next(iter(candidates))
        return {'status': 'mapped', 'project': name, 'reason': 'package_map', 'confidence': 'deterministic'}
    if len(candidates) > 1:
        return {'status': 'ambiguous', 'project': None, 'reason': 'conflicting_package_maps',
                'confidence': 'ambiguous'}
    for reference in raw.get('references') or []:
        name = github_repository(reference.get('url') if isinstance(reference, dict) else reference)
        policy = registry.resolve(name) if name else None
        if policy:
            candidates[policy.canonical_name] = 'reference_url'
    if len(candidates) == 1:
        name, reason = next(iter(candidates.items()))
        return {'status': 'mapped', 'project': name, 'reason': reason, 'confidence': 'deterministic'}
    return {'status': 'ambiguous' if candidates else 'unmapped', 'project': None,
            'reason': 'conflicting_evidence' if candidates else 'no_registry_match', 'confidence': 'ambiguous'}


def normalize_advisory(raw: dict, registry, *, source_key: str,
                       source_visibility: str = 'public') -> dict:
    if not isinstance(raw, dict):
        raise SourceError('invalid_advisory_page')
    vulnerabilities = raw.get('vulnerabilities') or []
    if (not isinstance(vulnerabilities, list)
            or any(not isinstance(v, dict) or not isinstance(v.get('package') or {}, dict)
                   for v in vulnerabilities)):
        raise SourceError('invalid_advisory_page')
    severity = raw.get('severity', 'unknown')
    if not isinstance(severity, str) or severity not in {'critical', 'high', 'medium', 'low', 'unknown'}:
        raise SourceError('invalid_advisory_page')
    ghsa = raw.get('ghsa_id')
    if not isinstance(ghsa, str) or not re.fullmatch(r'GHSA-[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4}', ghsa, re.I):
        raise SourceError('invalid_advisory_id')
    published = stamp(instant(raw.get('published_at')))
    updated = stamp(instant(raw.get('updated_at') or published))
    mapping = map_project(raw, registry)
    # Repository advisory responses may contain private collaborators and
    # fork metadata. Keep only source fields needed for audit/routing/export.
    fields = ('ghsa_id', 'cve_id', 'identifiers', 'type', 'severity', 'cvss',
              'cvss_severities', 'epss', 'cwes', 'summary', 'description',
              'vulnerabilities', 'source_code_location', 'repository_advisory_url',
              'references', 'published_at', 'updated_at', 'withdrawn_at',
              'state', 'html_url')
    payload = {key: raw[key] for key in fields if key in raw}
    payload['credits'] = [
        {'login': credit.get('login') or (credit.get('user') or {}).get('login'),
         'type': credit.get('type')}
        for credit in raw.get('credits') or []
        if isinstance(credit, dict) and isinstance(credit.get('user') or {}, dict)
    ]
    event_id = 'github:ghsa:' + ghsa.upper()
    def tidy(value):
        return ' '.join(str(value or '').split())

    def unordered(value):
        if not isinstance(value, list):
            return value
        return sorted(value, key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False))

    def affected(value):
        if not isinstance(value, list):
            return []
        result = []
        for item in value:
            if not isinstance(item, dict):
                continue
            package = item.get('package') if isinstance(item.get('package'), dict) else {}
            result.append({
                'ecosystem': str(package.get('ecosystem') or '').lower(),
                'package': str(package.get('name') or '').lower(),
                'range': tidy(item.get('vulnerable_version_range')),
                'patched': tidy(item.get('first_patched_version') or item.get('patched_versions')),
                'functions': unordered(item.get('vulnerable_functions')) or [],
            })
        return unordered(result)

    meaningful = {
        'severity': str(raw.get('severity') or '').lower(),
        'summary': tidy(raw.get('summary')),
        'description': tidy(raw.get('description')),
        'identifiers': unordered(raw.get('identifiers')),
        'cve_id': raw.get('cve_id'),
        'cvss': raw.get('cvss'),
        'cvss_severities': raw.get('cvss_severities'),
        'cwes': unordered(raw.get('cwes')),
        'vulnerabilities': affected(raw.get('vulnerabilities')),
        'withdrawn_at': raw.get('withdrawn_at'),
    }
    material_hash = 'sha256:' + hashlib.sha256(json.dumps(
        meaningful, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    return {'schema_version': 'k8s-intelligence-event/v1', 'event_id': event_id,
            'event_type': 'github_security_advisory', 'source_id': event_id,
            'repository': mapping['project'] or '', 'published_at': published,
            'updated_at': updated, 'visibility': source_visibility, 'source_trust': 100,
            'title': str(raw.get('summary') or ghsa), 'body': str(raw.get('description') or ''),
            'html_url': raw.get('html_url') or raw.get('repository_advisory_url') or '',
            'severity': raw.get('severity', 'unknown'), 'ghsa_id': ghsa.upper(),
            'affected_versions': [str(v.get('vulnerable_version_range') or '') for v in raw.get('vulnerabilities') or []],
            'patched_versions': [str(v.get('first_patched_version') or v.get('patched_versions') or '') for v in raw.get('vulnerabilities') or []],
            'withdrawn_at': raw.get('withdrawn_at'), 'project_mapping': mapping,
            'material_hash': material_hash,
            'cve_id': raw.get('cve_id'), 'identifiers': raw.get('identifiers') or [],
            'advisory_type': raw.get('type', 'reviewed'), 'cvss': raw.get('cvss'),
            'cvss_severities': raw.get('cvss_severities'), 'epss': raw.get('epss'),
            'cwes': raw.get('cwes') or [], 'vulnerabilities': raw.get('vulnerabilities') or [],
            'references': raw.get('references') or [],
            'metadata': payload, 'provenance': {'collector': source_key, 'api_resource_id': ghsa,
                                               'trust_reasons': ['published_official_advisory']},
            'content_hash': 'sha256:' + hashlib.sha256(json.dumps(payload, sort_keys=True,
                                      ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()}


class AdvisoryCollector:
    """One complete page per call. Caller commits events and cursor atomically.

    Global scans use frozen modified windows with overlap and server Link cursors.
    Repository scans have no server since filter, so bounded DESC scans stop at
    the overlap boundary and continue across invocations until reached.
    """
    def __init__(self, reader, registry, *, repository: str | None = None,
                 repository_visibility: str = 'unknown', withdrawn: bool = False,
                 per_page: int = 30, overlap_seconds: int = 3600,
                 bootstrap_days: int = 7, utcnow=None):
        if not 1 <= per_page <= 100 or overlap_seconds < 0 or bootstrap_days < 1:
            raise ValueError('invalid advisory collector limits')
        if repository is not None and not re.fullmatch(r'[a-z0-9_.-]+/[a-z0-9_.-]+', repository):
            raise ValueError('invalid advisory repository')
        if repository_visibility not in {'public', 'private', 'internal', 'unknown'}:
            raise ValueError('invalid repository visibility')
        if withdrawn and repository:
            raise ValueError('withdrawn scan is global-only')
        self.reader, self.registry, self.repository = reader, registry, repository
        self.visibility, self.withdrawn = repository_visibility if repository else 'public', withdrawn
        self.per_page, self.overlap, self.bootstrap_days = per_page, overlap_seconds, bootstrap_days
        self.utcnow = utcnow or (lambda: datetime.now(timezone.utc))
        self.path = f'/repos/{repository}/security-advisories' if repository else '/advisories'
        self.source_key = ('repository_advisories:' + repository if repository else
                           'global_advisories_withdrawn' if withdrawn else 'global_advisories')

    def fetch_page(self, state: dict, *, deadline: float) -> CollectedPage:
        state = dict(state)
        now = stamp(self.utcnow())
        if 'bootstrap_cutoff' not in state:
            state['bootstrap_cutoff'] = now
        for key in ('bootstrap_cutoff', 'watermark', 'lower', 'upper'):
            if key in state:
                instant(state[key])
        if not state.get('continuation'):
            state['upper'] = now
            state['lower'] = stamp(instant(state['watermark']) - timedelta(seconds=self.overlap)
                                  if state.get('watermark') else
                                  instant(now) - timedelta(days=self.bootstrap_days))
            params = {'per_page': self.per_page, 'sort': 'updated',
                      'direction': 'desc' if self.repository else 'asc'}
            if self.repository:
                params['state'] = 'published'
            else:
                params['modified'] = state['lower'] + '..' + state['upper']
                if self.withdrawn:
                    params['is_withdrawn'] = 'true'
            path = self.path + '?' + urllib.parse.urlencode(params)
        else:
            path = state['continuation']
            parsed = urllib.parse.urlsplit(path)
            if parsed.scheme or parsed.netloc or parsed.path != self.path or 'lower' not in state or 'upper' not in state:
                raise SourceError('invalid_cursor')
            try:
                params = urllib.parse.parse_qs(parsed.query, strict_parsing=True)
            except ValueError:
                raise SourceError('invalid_cursor') from None
            expected = {'per_page': str(self.per_page), 'sort': 'updated',
                        'direction': 'desc' if self.repository else 'asc',
                        'state': 'published'} if self.repository else {
                            'per_page': str(self.per_page), 'sort': 'updated', 'direction': 'asc',
                            'modified': state['lower'] + '..' + state['upper']}
            if self.withdrawn:
                expected['is_withdrawn'] = 'true'
            if (any(params.get(key) != [value] for key, value in expected.items())
                    or set(params) - set(expected) not in ({'after'}, {'before'})
                    or len(params) != len(expected) + 1):
                raise SourceError('invalid_cursor')
        raw, headers = self.reader.get(path, deadline=deadline)
        if not isinstance(raw, list):
            raise SourceError('invalid_advisory_page')
        events = []
        reached_boundary = False
        for item in raw:
            if not isinstance(item, dict):
                raise SourceError('invalid_advisory_page')
            if item.get('state', 'published') != 'published' or not item.get('published_at'):
                continue
            event = normalize_advisory(item, self.registry, source_key=self.source_key,
                                       source_visibility=self.visibility)
            if self.repository and instant(event['updated_at']) < instant(state['lower']):
                reached_boundary = True
                continue
            events.append(event)
        continuation = next_api_path(headers, self.path)
        terminal = not continuation or reached_boundary
        state['continuation'] = None if terminal else continuation
        if terminal:
            state['watermark'] = state['upper']
            state['initialized'] = True
        return CollectedPage(self.source_key, tuple(events), state, terminal,
                             {'pages': 1, 'events': len(events), 'observed': len(raw)})
