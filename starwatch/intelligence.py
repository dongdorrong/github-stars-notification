"""Explicit shadow/canary/full orchestration; raw state remains authoritative."""
from __future__ import annotations

import json
import time
from collections import Counter
from dataclasses import asdict, replace
from datetime import datetime, timezone

from .advisories import instant
from .analysis import AnalysisConfig, AnalysisService
from .routing import decide, select, build_routed_chunks
from .security import redact, visibility
from .signal_collectors import SourceError


def validate_config(config: dict) -> None:
    policy = config.get('intelligence', {})
    if not isinstance(policy, dict) or policy.get('mode', 'shadow') not in {'shadow', 'canary', 'full'}:
        raise ValueError('invalid intelligence rollout mode')
    for name, default, maximum in (('max_pages_per_source', 3, 20), ('budget_seconds', 120, 300), ('max_analyses_per_run', 200, 1000)):
        value = policy.get(name, default)
        if type(value) is not int or not 1 <= value <= maximum:
            raise ValueError('invalid intelligence collector limit')
    if not isinstance(policy.get('canary_projects', []), list):
        raise ValueError('invalid canary project list')
    if config.get('routing', {}).get('destination_visibility', 'private') not in {'public', 'private'}:
        raise ValueError('invalid Slack destination visibility')
    AnalysisConfig.from_mapping(config.get('analysis'))
    for field in ('advisories_enabled', 'repository_advisories_enabled'):
        if field in policy and type(policy[field]) is not bool:
            raise ValueError('invalid source enable switch')
    routing = config.get('routing', {})
    if not isinstance(routing, dict) or routing.get('policy_version', 'v1') != 'v1':
        raise ValueError('unsupported routing policy')
    for field in ('high', 'digest'):
        section = routing.get(field, {})
        if not isinstance(section, dict):
            raise ValueError('invalid routing section')
        if type(section.get('max_batch_size', 50)) is not int or not 1 <= section.get('max_batch_size', 50) <= 100:
            raise ValueError('invalid routing batch size')
    digest = routing.get('digest', {})
    if type(digest.get('min_count', 5)) is not int or digest.get('min_count', 5) < 1:
        raise ValueError('invalid digest count')
    if type(digest.get('max_age_hours', 24)) not in (int, float) or not 0 < digest.get('max_age_hours', 24) <= 168:
        raise ValueError('invalid digest max age')
    hours = digest.get('send_hours_kst', [17])
    if not isinstance(hours, list) or any(type(h) is not int or not 0 <= h < 24 for h in hours):
        raise ValueError('invalid digest hours')
    from .artifacts import _enabled
    _enabled(config)  # Validate opt-in outputs before any state or transport.


class Intelligence:
    def __init__(self, config, registry, inventory, collectors=(), *, mode='preview',
                 analyzer=None, clock=time.monotonic):
        validate_config(config)
        self.config, self.registry, self.mode = config, registry, mode
        self.inventory = {r['full_name'].lower(): r for r in inventory}
        self.collectors, self.clock = collectors, clock
        analysis_config = AnalysisConfig.from_mapping(config.get('analysis'))
        # Preview is fallback-only even if configuration enables an endpoint.
        self.analyzer = analyzer or AnalysisService(replace(analysis_config, enabled=False)
                                                   if mode == 'preview' else analysis_config)
        self.metrics = {'classification': registry.classification_report(inventory),
                        'advisory_pages': 0, 'advisory_events': 0, 'advisory_errors': 0,
                        'announcement_pages': 0, 'announcement_events': 0, 'announcement_errors': 0,
                        'ai_success': 0, 'ai_fallback': 0, 'ai_cache': 0,
                        'visibility_exclusions': 0, 'signal_budget_exhausted': False,
                        'signal_deferred': 0, 'signal_errors_by_type': {}, 'route_counts': {}}
        self.new_events = []
        self.decisions = {}
        self.analyses = {}

    def release_visibility(self, repository):
        return visibility(self.inventory.get(repository.lower(), {}))

    def enrich(self, event):
        event = dict(event)
        metadata = self.inventory.get(event['repository'].lower(), {})
        if event['event_type'] == 'github_release':
            event['visibility'] = visibility(metadata)
        else:
            event.setdefault('visibility', 'unknown')
        if event.get('repository'):
            event['project_visibility'] = visibility(metadata)
        event.setdefault('source_trust', 100 if event['event_type'] == 'github_release' else 0)
        event.setdefault('title', event.get('release_name') or event.get('tag_name') or '')
        event.setdefault('provenance', {'collector': 'github-releases',
                                       'api_resource_id': event.get('release_id')})
        return event

    def project(self, event):
        policy = self.registry.resolve(event['repository']) if event['repository'] else None
        return asdict(policy) if policy else {'enabled': True, 'tier': 'standard', 'signals': {}}

    def process(self, store, observed_releases, *, deadline):
        policy = self.config['intelligence']
        with store.transaction():
            tracked = {row[0].removeprefix('repository_visibility:') for row in store.db.execute(
                "SELECT key FROM state_metadata WHERE key LIKE 'repository_visibility:%'")}
            for repository in tracked | self.inventory.keys():
                store.set_meta('repository_visibility:' + repository, self.release_visibility(repository))
        deadline = min(deadline, self.clock() + policy.get('budget_seconds', 120))
        errors = Counter()
        observed = {event['event_id']: self.enrich(event) for event in observed_releases}
        for index, collector in enumerate(self.collectors):
            group = 'advisory' if 'advisories' in collector.source_key else 'announcement'
            key = 'signal_cursor:' + collector.source_key
            raw_state = store.get_meta(key)
            try:
                state = json.loads(raw_state) if raw_state is not None else {}
                if not isinstance(state, dict):
                    raise ValueError
                if raw_state is not None:
                    if not state or not isinstance(state.get('bootstrap_cutoff'), str):
                        raise ValueError
                    for field in ('bootstrap_cutoff', 'watermark', 'lower', 'upper'):
                        if field in state:
                            instant(state[field])
                    continuation = state.get('continuation')
                    if continuation is not None and (not isinstance(continuation, str)
                            or not continuation.strip() or len(continuation) > 8192):
                        raise ValueError
                    if 'initialized' in state and type(state['initialized']) is not bool:
                        raise ValueError
            except (ValueError, TypeError, SourceError):
                raise ValueError('invalid signal cursor state') from None
            terminal = False
            for _ in range(policy.get('max_pages_per_source', 3)):
                if self.clock() >= deadline:
                    self.metrics['signal_budget_exhausted'] = True
                    break
                try:
                    page = collector.fetch_page(state, deadline=deadline)
                except SourceError as exc:
                    if raw_state is not None and exc.category == 'invalid_cursor':
                        raise ValueError('invalid durable source cursor') from None
                    self.metrics[group + '_errors'] += 1
                    errors[exc.category] += 1
                    if exc.category == 'budget_exhausted':
                        self.metrics['signal_budget_exhausted'] = True
                    break
                # Only a fully normalized page can commit its continuation.
                with store.transaction():
                    for event in page.events:
                        event = self.enrich(event)
                        baseline = bool(page.proposed_cursor.get('bootstrap_cutoff') and
                                        instant(event['updated_at']) <= instant(page.proposed_cursor['bootstrap_cutoff']))
                        exists = store.db.execute('SELECT 1 FROM events WHERE event_id=?',
                                                  (event['event_id'],)).fetchone()
                        reason = 'source_bootstrap' if baseline and exists is None else self.rollout_reason(event)
                        initial = decide(event, self.project(event), self.config)
                        reason = reason or initial['suppression_reason']
                        if store.upsert_raw(event, suppress=bool(reason), suppression_reason=reason):
                            self.new_events.append(event)
                        stored = store.db.execute('SELECT payload_json FROM events WHERE event_id=?',
                                                  (event['event_id'],)).fetchone()
                        observed[event['event_id']] = self.enrich(json.loads(stored[0]))
                    store.set_meta(key, json.dumps(page.proposed_cursor, sort_keys=True))
                state, terminal = page.proposed_cursor, page.terminal
                self.metrics[group + '_pages'] += 1
                self.metrics[group + '_events'] += len(page.events)
                if terminal:
                    break
            if not terminal:
                self.metrics['signal_deferred'] += 1
        self.metrics['signal_errors_by_type'] = dict(sorted(errors.items()))
        # Evaluate pending from previous runs even when a source is unavailable.
        observed.update({e['event_id']: self.enrich(e) for e in store.pending(include_delayed=True)})
        routes = Counter()
        self.metrics['ai_deferred'] = 0
        max_analyses = policy.get('max_analyses_per_run', 200)
        analyzed = 0
        pending_ids = {e['event_id'] for e in store.pending()}
        ordered = sorted(observed.values(), key=lambda e: (e['event_id'] not in pending_ids,
                         e['event_type'] == 'github_release', e['event_id']))
        for event in ordered:
            project = self.project(event)
            initial = decide(event, project, self.config)
            decision = initial
            if analyzed < max_analyses and self.clock() < deadline:
                # AI network calls are outside the SQLite write transaction.
                # Cache writes are staged, then committed with the route audit.
                staged = []
                class AnalysisCache:
                    get_analysis = store.get_analysis
                    def save_analysis(self, analysis):
                        staged.append(analysis)
                service = self.analyzer
                required = service.config.timeout_seconds * (service.config.max_retries + 1)
                if service.config.enabled and deadline - self.clock() < required:
                    service = AnalysisService(replace(service.config, enabled=False))
                result = service.analyze(redact(event), initial['deterministic_floor'], store=AnalysisCache())
                metric = {'ai': 'ai_success', 'fallback': 'ai_fallback', 'cache': 'ai_cache',
                          'fallback_cache': 'ai_cache'}[result.source]
                self.metrics[metric] += 1
                decision = decide(event, project, self.config, result.analysis)
                self.analyses[event['event_id']] = result.analysis
                analyzed += result.source not in {"cache", "fallback_cache"}
            else:
                staged = []
                self.metrics['ai_deferred'] += 1
            row = store.db.execute('SELECT notification_state,suppression_reason FROM notification_outbox WHERE event_id=?',
                                   (event['event_id'],)).fetchone()
            reason = decision['suppression_reason'] or self.rollout_reason(event)
            if row and row[0] == 'SUPPRESSED':
                reason = reason or row[1] or 'release_baseline'
            if reason:
                decision = dict(decision, route='SUPPRESSED', effective_priority='SUPPRESSED',
                                suppression_reason=reason,
                                deterministic_reasons=decision['deterministic_reasons'] + [reason])
            self.decisions[event['event_id']] = decision
            routes[decision['route']] += 1
            with store.transaction():
                for analysis in staged:
                    store.save_analysis(analysis)
                store.save_routing(event['event_id'], decision)
                if reason:
                    self.metrics['visibility_exclusions'] += reason == 'visibility_not_public'
                    store.db.execute("""UPDATE notification_outbox SET notification_state='SUPPRESSED',
                        suppression_reason=? WHERE event_id=? AND notification_state IN
                        ('PENDING_NOTIFICATION','DELIVERY_FAILED')""", (reason, event['event_id']))
        self.metrics['route_counts'] = dict(sorted(routes.items()))

    def rollout_reason(self, event):
        if event['event_type'] == 'github_release':
            return None
        policy = self.config['intelligence']
        if policy.get('mode', 'shadow') == 'shadow':
            return 'rollout_shadow'
        if policy.get('mode') == 'canary' and event['repository'] not in policy.get('canary_projects', []):
            return 'rollout_canary_excluded'
        return None

    def select(self, pending):
        candidates = []
        full = self.config['intelligence'].get('mode') == 'full'
        for event in pending:
            if event['event_type'] == 'github_release' and not full:
                continue
            enriched = self.enrich(event)
            enriched['routing'] = self.decisions.get(event['event_id']) or decide(enriched, self.project(enriched), self.config)
            enriched['analysis'] = self.analyses.get(event['event_id'], {})
            enriched.update({k: v for k, v in enriched['analysis'].items()
                             if k in {'summary_ko', 'recommended_actions'}})
            candidates.append(enriched)
        selected = select(candidates, self.config, datetime.now(timezone.utc))
        return build_routed_chunks(selected, self.config['notification']['max_slack_text_length'], self.config)
