"""State-safe collection, baseline migration, and delivery orchestration."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

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


def _legacy_suppress(event, cache: dict[str, dict]) -> bool:
    baseline = cache.get(event.repository)
    if not baseline:
        return False
    # The old cache has no Release ID. Its tag and publication date are a
    # cutover boundary only, never a substitute durable identity.
    return _instant(event.published_at) <= _instant(baseline["published"])


def run_pipeline(
    *, state_path: Path, legacy_path: Path, repos: list[str], source: ReleaseSource,
    config: dict, mode: str = "preview", send_slack: bool = False,
    transport: SlackTransport | None = None,
) -> PipelineResult:
    if mode not in {"preview", "commit"}:
        raise ValueError("mode must be preview or commit")
    if send_slack and mode != "commit":
        raise ValueError("Slack is forbidden in preview mode")
    if send_slack and transport is None:
        raise ValueError("Slack transport is required")
    legacy = load_legacy_cache(legacy_path)  # fail before touching DB
    existed = state_path.exists()
    store = EventStore.open(state_path, preview=mode == "preview")
    try:
        first_run = not existed
        migration = store.get_meta("legacy_migrated") is None
        legacy_cutover = migration and legacy_path.exists()
        collected = collect_releases(repos, source, set(config["special_projects"]))
        failed_repos = {error.repository for error in collected.errors}
        old_ids = store.event_ids()
        new_events: list[dict] = []
        baseline_created = False
        with store.transaction():
            if migration:
                store.set_meta("legacy_migrated", "legacy_cache" if legacy_path.exists() else "no_legacy_cache")
            for event in collected.events:
                if event.repository in failed_repos:
                    continue
                if event.event_id not in old_ids:
                    initialized = store.get_meta("repo_initialized:" + event.repository) is not None
                    if not initialized:
                        if legacy_path.exists() and event.repository in legacy:
                            suppress = _legacy_suppress(event, legacy)
                        else:
                            suppress = not config["notification"]["first_run_notify"]
                        baseline_created = baseline_created or suppress
                    else:
                        suppress = False
                    store.upsert(event, suppress=suppress or event.draft)
                    new_events.append(event.as_dict())
                else:
                    store.upsert(event)
            for repo in repos:
                if repo not in failed_repos and store.get_meta("repo_initialized:" + repo) is None:
                    store.set_meta("repo_initialized:" + repo, now())
            # Release IDs and timestamps are diagnostic cursors, not pagination stop points.
            latest_by_repo = {}
            for event in collected.events:
                if event.repository not in failed_repos and (
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
                              pending_before_delivery_count, pending_after_delivery_count)
    finally:
        store.close()
