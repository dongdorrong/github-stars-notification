"""State-safe collection, baseline migration, and delivery orchestration."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
import time

from .event_store import EventStore, now
from .notifier import SlackTransport, deliver
from .policy import Decision, select
from .release_collector import CollectionResult, ReleaseSource, assess_collection, collect_releases
from .slack_payload import SlackChunk, build_chunks


@dataclass(frozen=True)
class PipelineResult:
    collected: CollectionResult
    new_events: list[dict]
    pending: list[dict]
    decision: Decision
    chunks: list[SlackChunk]
    first_run: bool
    delivery_succeeded: bool | None
    baseline_created: bool
    pending_before_delivery_count: int
    pending_after_delivery_count: int
    cutover_policy_applied: bool
    cutover_backlog_suppressed_count: int


def load_legacy_cache(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("legacy release cache is malformed") from exc
    if not isinstance(data, dict):
        raise ValueError("legacy release cache must be a mapping")
    for repo, value in data.items():
        if not isinstance(repo, str) or not isinstance(value, dict):
            raise ValueError("legacy release cache has an invalid repository entry")
        if not isinstance(value.get("tag"), str) or not isinstance(value.get("published"), str):
            raise ValueError("legacy release cache entry lacks tag/published baseline")
        _instant(value["published"])
    return data


def _instant(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("legacy release cache has an invalid timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _metadata_int(store: EventStore, key: str, default: int = 0, *, minimum: int = 0) -> int:
    value = store.get_meta(key)
    if value is None:
        return default
    if not value.isascii() or not value.isdecimal() or int(value) < minimum:
        raise ValueError("collector state metadata is malformed")
    return int(value)


def _prebootstrap_suppress(event, cutoff: int | None, created_cutoff: str | None) -> bool:
    """Avoid a delayed historical bootstrap flood when deep pages are first seen.

    Numeric Release IDs are an ordering *heuristic*, not a GitHub guarantee.
    Stable IDs above the observed bootstrap high-water remain notification-eligible
    even when a release publication date was backdated.
    """
    if cutoff is not None and isinstance(event.release_id, int) and event.release_id <= cutoff:
        return True
    if cutoff is not None and isinstance(event.release_id, int):
        return False
    return bool(created_cutoff and event.created_at and
                _instant(event.created_at) <= _instant(created_cutoff))


def run_pipeline(
    *, state_path: Path, legacy_path: Path, repos: list[str], source: ReleaseSource,
    config: dict, mode: str = "preview", send_slack: bool = False,
    transport: SlackTransport | None = None,
    clock: Callable[[], float] = time.monotonic,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> PipelineResult:
    if mode not in {"preview", "commit"}:
        raise ValueError("mode must be preview or commit")
    if send_slack and mode != "commit":
        raise ValueError("Slack is forbidden in preview mode")
    if send_slack and transport is None:
        raise ValueError("Slack transport is required")
    cutover_pending_policy = config["notification"].get("cutover_pending_policy", "suppress_existing")
    if cutover_pending_policy not in {"suppress_existing", "preserve_pending"}:
        raise ValueError("notification.cutover_pending_policy must be suppress_existing or preserve_pending")
    legacy = load_legacy_cache(legacy_path)  # fail before touching DB
    existed = state_path.exists()
    store = EventStore.open(state_path, preview=mode == "preview")
    try:
        first_run = not existed
        migration = store.get_meta("legacy_migrated") is None
        legacy_cutover = migration and legacy_path.exists()
        old_ids = store.event_ids()
        # A missing migration marker in an already populated DB must not turn
        # existing retryable/delivered rows into a new cutover cohort.
        suppress_cutover_cohort = (
            legacy_cutover and not old_ids and
            cutover_pending_policy == "suppress_existing"
        )
        initialized = {repo for repo in repos if store.get_meta("repo_initialized:" + repo) is not None}
        cursors = {repo: _metadata_int(store, "reconciliation_cursor:" + repo, 1, minimum=1)
                   for repo in initialized}
        visits = {repo: _metadata_int(store, "reconcile_visit:" + repo)
                  for repo in initialized}
        start_index = _metadata_int(store, "collector_next_repo_index")
        for repo in initialized:
            if store.get_meta("bootstrap_max_release_id:" + repo) is not None:
                _metadata_int(store, "bootstrap_max_release_id:" + repo, minimum=1)
            for key in ("bootstrap_created_at:", "legacy_cutover_published_at:"):
                value = store.get_meta(key + repo)
                if value is not None:
                    _instant(value)
        collected = collect_releases(
            repos, source, set(config["special_projects"]), old_ids,
            config=config.get("collector"), initialized_repos=initialized,
            reconciliation_cursors=cursors, reconciliation_visits=visits,
            start_index=start_index, clock=clock, progress=progress,
        )
        successful_bootstraps = {update.repository for update in collected.repo_updates if update.initialized}
        bootstrapped_ids: dict[str, int] = {}
        bootstrapped_created: dict[str, str] = {}
        for event in collected.events:
            if event.repository in successful_bootstraps and isinstance(event.release_id, int):
                bootstrapped_ids[event.repository] = max(bootstrapped_ids.get(event.repository, 0), event.release_id)
            if event.repository in successful_bootstraps and event.created_at:
                previous = bootstrapped_created.get(event.repository)
                if previous is None or _instant(event.created_at) > _instant(previous):
                    bootstrapped_created[event.repository] = event.created_at
        new_events: list[dict] = []
        baseline_created = False
        cutover_backlog_suppressed_count = 0
        with store.transaction():
            if migration:
                store.set_meta("legacy_migrated", "legacy_cache" if legacy_path.exists() else "no_legacy_cache")
                if legacy_cutover and not old_ids:
                    store.set_meta("legacy_cutover_pending_policy", cutover_pending_policy)
                # Store the read-only cutover hint even for repositories whose
                # initial scan fails or is deferred. They may bootstrap after
                # the legacy file is no longer present.
                for repo, entry in legacy.items():
                    store.set_meta("legacy_cutover_published_at:" + repo, entry["published"])
            else:
                # Older P0 databases may have marked global migration before a
                # repository's first successful scan. Keep its legacy boundary
                # when the read-only file is still available.
                for repo, entry in legacy.items():
                    if (store.get_meta("repo_initialized:" + repo) is None and
                            store.get_meta("legacy_cutover_published_at:" + repo) is None):
                        store.set_meta("legacy_cutover_published_at:" + repo, entry["published"])
            for event in collected.events:
                if event.event_id not in old_ids:
                    initialized = store.get_meta("repo_initialized:" + event.repository) is not None
                    if not initialized:
                        legacy_date = store.get_meta("legacy_cutover_published_at:" + event.repository)
                        if legacy_date is not None:
                            suppress = _instant(event.published_at) <= _instant(legacy_date)
                        else:
                            suppress = not config["notification"]["first_run_notify"]
                        baseline_created = baseline_created or suppress
                    else:
                        legacy_date = store.get_meta("legacy_cutover_published_at:" + event.repository)
                        if legacy_date is not None:
                            suppress = _instant(event.published_at) <= _instant(legacy_date)
                        else:
                            cutoff = store.get_meta("bootstrap_max_release_id:" + event.repository)
                            created_cutoff = store.get_meta("bootstrap_created_at:" + event.repository)
                            suppress = _prebootstrap_suppress(event, int(cutoff) if cutoff is not None else None,
                                                             created_cutoff)
                        baseline_created = baseline_created or suppress
                    if suppress_cutover_cohort and not event.draft:
                        cutover_backlog_suppressed_count += 1
                    suppress = suppress or suppress_cutover_cohort
                    baseline_created = baseline_created or suppress
                    store.upsert(event, suppress=suppress or event.draft)
                    new_events.append(event.as_dict())
                else:
                    store.upsert(event)
            for update in collected.repo_updates:
                if update.initialized:
                    store.set_meta("repo_initialized:" + update.repository, now())
                    has_legacy_boundary = store.get_meta(
                        "legacy_cutover_published_at:" + update.repository) is not None
                    if not has_legacy_boundary and not config["notification"]["first_run_notify"] and update.repository in bootstrapped_ids:
                        store.set_meta("bootstrap_max_release_id:" + update.repository,
                                       str(bootstrapped_ids[update.repository]))
                    if not has_legacy_boundary and (
                        not config["notification"]["first_run_notify"] and
                        update.repository in bootstrapped_created
                    ):
                        store.set_meta("bootstrap_created_at:" + update.repository,
                                       bootstrapped_created[update.repository])
                if update.reconciliation_cursor is not None:
                    store.set_meta("reconciliation_cursor:" + update.repository,
                                   str(update.reconciliation_cursor))
                if update.reconciliation_visit is not None:
                    store.set_meta("reconcile_visit:" + update.repository,
                                   str(update.reconciliation_visit))
            store.set_meta("collector_next_repo_index", str(collected.next_start_index))
            # Release IDs and timestamps are diagnostic cursors, not pagination stop points.
            latest_by_repo = {}
            for event in collected.events:
                if (
                    event.repository not in latest_by_repo or
                    (_instant(event.published_at), str(event.release_id)) >
                    (_instant(latest_by_repo[event.repository].published_at), str(latest_by_repo[event.repository].release_id))
                ):
                    latest_by_repo[event.repository] = event
            for event in latest_by_repo.values():
                store.set_meta("last_release:" + event.repository, json.dumps({"id": event.release_id, "published_at": event.published_at}))
        store.recover_expired_leases()
        pending = store.pending()
        pending_before_delivery_count = len(store.pending(include_delayed=True))
        decision = select(pending, config)
        chunks = build_chunks(pending, config["notification"].get("max_slack_text_length", 35000)) if decision.should_notify else []
        delivered: bool | None = None
        # The cutover invocation itself never sends, including when the caller
        # explicitly enabled Slack. A subsequent run can send post-baseline new events.
        if send_slack and not assess_collection(collected).fatal and not legacy_cutover and chunks:
            delivered = deliver(store, chunks, transport)
        pending_after_delivery_count = len(store.pending(include_delayed=True))
        return PipelineResult(collected, new_events, pending, decision, chunks,
                              first_run, delivered, baseline_created,
                              pending_before_delivery_count, pending_after_delivery_count,
                              suppress_cutover_cohort, cutover_backlog_suppressed_count)
    finally:
        store.close()
