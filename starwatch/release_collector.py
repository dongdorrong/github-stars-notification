"""Bounded page-based GitHub Release collection with stable IDs."""

from __future__ import annotations

import hashlib
import json
import secrets
import signal
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol

from .models import ReleaseEvent


class ReleaseSource(Protocol):
    def fetch_page(self, repository: str, page_number: int, per_page: int) -> "ReleasePage": ...


@dataclass(frozen=True)
class ReleasePage:
    releases: tuple[Mapping[str, Any], ...]
    page_number: int
    has_next: bool
    request_count: int = 1


DEFAULT_COLLECTOR: dict[str, int] = {
    "per_page": 100,
    "max_incremental_pages_per_repo": 3,
    "max_incremental_pages_special_project": 5,
    "bootstrap_pages": 1,
    "known_only_pages_to_stop": 1,
    "global_budget_seconds": 900,
    "per_repo_budget_seconds": 60,
    "reconciliation_pages_per_repo": 2,
    "reconciliation_pages_special_project": 4,
    "reconciliation_shards": 8,
    "special_reconciliation_shards": 2,
    "max_reconciliation_repositories_per_run": 10,
}


@dataclass(frozen=True)
class CollectorError:
    repository: str
    status: int | None
    message: str


@dataclass(frozen=True)
class CollectionResult:
    events: tuple[ReleaseEvent, ...]
    repositories_scanned: int
    repositories_with_release: int
    errors: tuple[CollectorError, ...]
    repositories_total: int = 0
    repositories_started: int = 0
    repositories_completed: int = 0
    repositories_deferred: int = 0
    pages_fetched: int = 0
    releases_observed: int = 0
    elapsed_seconds: float = 0.0
    collection_budget_exhausted: bool = False
    repo_updates: tuple["RepositoryUpdate", ...] = ()
    next_start_index: int = 0
    reconciliation_deferred: int = 0


@dataclass(frozen=True)
class RepositoryUpdate:
    repository: str
    initialized: bool = False
    reconciliation_cursor: int | None = None
    reconciliation_visit: int | None = None


@dataclass(frozen=True)
class CollectionHealth:
    success_count: int
    degraded: bool
    fatal: bool


def assess_collection(result: CollectionResult) -> CollectionHealth:
    """Fail closed for systemic/majority errors; tolerate isolated repo failures."""
    errors = len(result.errors)
    success = result.repositories_completed if result.repositories_total else result.repositories_scanned - errors
    isolated = all(error.status in (404, 429) or
                   (error.status is not None and 500 <= error.status <= 599)
                   for error in result.errors)
    fatal = success == 0 or (errors * 2 > result.repositories_started if result.repositories_started else
                             errors * 2 > result.repositories_scanned) or not isolated
    return CollectionHealth(success, bool(errors or result.repositories_deferred or
                                          result.reconciliation_deferred) and not fatal, fatal)


class FixtureSourceError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class BudgetExpired(BaseException):
    """The current page did not finish before its collector deadline."""


@contextmanager
def _live_page_deadline(seconds: float):
    """Hard wall-clock bound for a live request on Actions' POSIX main thread.

    Never steal a caller's existing alarm. Unsupported contexts fail closed.
    Fixtures use their injected monotonic clock instead of process signals.
    """
    if seconds <= 0:
        raise BudgetExpired()
    if threading.current_thread() is not threading.main_thread() or not hasattr(signal, "setitimer"):
        raise RuntimeError("bounded live collection requires POSIX main thread")
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    if previous_timer[0] > 0:
        raise RuntimeError("bounded live collection cannot replace an active alarm")
    previous_handler = signal.getsignal(signal.SIGALRM)
    if previous_handler != signal.SIG_DFL:
        raise RuntimeError("bounded live collection cannot replace a caller alarm handler")

    def expired(_signum, _frame):
        raise BudgetExpired()

    signal.signal(signal.SIGALRM, expired)
    try:
        signal.setitimer(signal.ITIMER_REAL, seconds)
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)


class FixtureReleaseSource:
    """Read test releases; accepts a legacy single object, list, or pages list.

    Missing Release IDs get a deterministic synthetic ID only in this adapter.
    """

    def __init__(self, path: Path):
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            grouped: dict[str, list[dict[str, Any]]] = {}
            for item in data:
                if not isinstance(item, dict) or "repo" not in item:
                    raise ValueError("fixture list entries must be objects with repo")
                repo = _normalize_repo(str(item["repo"]))
                grouped.setdefault(repo, []).append(item)
            data = grouped
        if not isinstance(data, dict):
            raise ValueError("fixture releases must be a mapping or list of objects with repo")
        self._releases = {_normalize_repo(str(repo)): value for repo, value in data.items()}

    def fetch_page(self, repository: str, page_number: int, per_page: int) -> ReleasePage:
        if page_number < 1 or per_page < 1:
            raise ValueError("invalid page request")
        value = self._releases.get(_normalize_repo(repository))
        if value is None or value is False:
            return ReleasePage((), page_number, False)
        if isinstance(value, dict) and "error" in value:
            error = value["error"]
            if not isinstance(error, dict):
                raise ValueError("fixture error must be an object")
            raise FixtureSourceError(int(error["status"]), str(error.get("message", "fixture error")))
        if isinstance(value, dict) and "pages" in value:
            pages = value["pages"]
            if not isinstance(pages, list) or any(
                not isinstance(page, list) and not (
                    isinstance(page, dict) and isinstance(page.get("error"), dict)
                ) for page in pages
            ):
                raise ValueError("fixture pages must be release lists or error objects")
            selected = pages[page_number - 1] if page_number <= len(pages) else []
            if isinstance(selected, dict) and "error" in selected:
                error = selected["error"]
                raise FixtureSourceError(int(error["status"]), str(error.get("message", "fixture error")))
            releases = selected
            has_next = page_number < len(pages)
        elif isinstance(value, list):
            offset = (page_number - 1) * per_page
            releases = value[offset:offset + per_page]
            has_next = offset + per_page < len(value)
        elif isinstance(value, dict):
            releases = [value] if page_number == 1 else []
            has_next = False
        else:
            raise ValueError(f"fixture releases for {repository} must be an object or list")
        if any(not isinstance(item, dict) for item in releases):
            raise ValueError(f"fixture releases for {repository} contain a non-object")
        return ReleasePage(tuple(releases), page_number, has_next)


class LiveReleaseSource:
    """PyGithub adapter; injected client permits token-free adapter tests."""

    def __init__(self, token: str | None = None, *, client: Any | None = None,
                 per_page: int = 100):
        if client is None:
            if not token:
                raise ValueError("GitHub token is required for live collection")
            try:
                from github import Github  # type: ignore
            except ModuleNotFoundError as exc:
                raise RuntimeError("PyGithub is required for live collection") from exc
            client = Github(token, per_page=per_page, timeout=15, retry=0)
        self._client = client
        self._per_page = per_page
        self._repositories: dict[str, Any] = {}

    def fetch_page(self, repository: str, page_number: int, per_page: int) -> ReleasePage:
        if page_number < 1 or per_page < 1:
            raise ValueError("invalid page request")
        if per_page != self._per_page:
            raise ValueError("source and collector page sizes differ")
        # PyGithub's get_page is zero-based and does not traverse later pages.
        github_repo = self._repositories.get(repository)
        if github_repo is None:
            github_repo = self._client.get_repo(repository, lazy=True)
            self._repositories[repository] = github_repo
        page = github_repo.get_releases().get_page(page_number - 1)
        releases = []
        for release in page:
            # PyGithub 2.2.0 `raw_data` completes a list object with one GET
            # per Release. Its list response is already in `_rawData`; reading
            # that pinned internal field avoids an otherwise unbounded N+1.
            raw = getattr(release, "_rawData", None)
            if not isinstance(raw, dict):
                raise ValueError("PyGithub release list item has no raw data")
            releases.append(raw)
        # A full page may be terminal; one bounded follow-up request then confirms it.
        return ReleasePage(tuple(releases), page_number, len(releases) >= per_page)


def _normalize_repo(repo: str) -> str:
    return "/".join(part.strip() for part in repo.strip().split("/"))


def _release_id(repository: str, raw: Mapping[str, Any], fixture: bool) -> int | str:
    value = raw.get("id")
    if value is None or value == "":
        if not fixture:
            raise ValueError(f"live GitHub release for {repository} has no Release ID")
        # Legacy fixture compatibility; never used for live event identity.
        key = json.dumps(
            [repository, raw.get("tag_name", raw.get("tag")), raw.get("published_at", raw.get("published"))],
            ensure_ascii=False, separators=(",", ":"),
        )
        return "fixture:" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
    if isinstance(value, bool) or not (
        isinstance(value, int) or (isinstance(value, str) and value.isascii() and value.isdecimal())
    ):
        raise ValueError(f"invalid GitHub Release ID for {repository}")
    number = int(value)
    if number <= 0:
        raise ValueError(f"invalid GitHub Release ID for {repository}")
    return number


def normalize_release(repository: str, raw: Mapping[str, Any], *, is_special: bool = False, fixture: bool = False) -> ReleaseEvent:
    """Preserve release facts and provenance; never derive live identity from tag/time."""
    repository = _normalize_repo(repository)
    release_id = _release_id(repository, raw, fixture)
    event_id = f"github:release:{release_id}"
    created = str(raw.get("created_at") or raw.get("published_at") or raw.get("published") or "")
    published = str(raw.get("published_at") or raw.get("published") or created)
    if not published:
        raise ValueError(f"release {event_id} has no publication or creation timestamp")
    updated = str(raw.get("updated_at") or published)
    fields = {
        "repository": repository,
        "release_id": release_id,
        "tag_name": str(raw.get("tag_name") or raw.get("tag") or ""),
        "release_name": str(raw.get("name") or raw.get("title") or ""),
        "body": str(raw.get("body") or ""),
        "html_url": str(raw.get("html_url") or raw.get("url") or ""),
        "published_at": published,
        "created_at": created,
        "updated_at": updated,
        "draft": bool(raw.get("draft", False)),
        "prerelease": bool(raw.get("prerelease", False)),
    }
    content = json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    content_hash = "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()
    return ReleaseEvent(
        event_id=event_id,
        event_type="github_release",
        source_id=event_id,
        is_special=is_special,
        content_hash=content_hash,
        raw_metadata={"collector": "github-releases", "api_resource_id": release_id, "release": dict(raw)},
        **fields,
    )


def _safe_error(repository: str, exc: Exception) -> CollectorError:
    # Never expose exception text: HTTP exceptions can embed URLs, headers or bodies.
    status = getattr(exc, "status", None)
    safe_status = status if isinstance(status, int) and not isinstance(status, bool) else None
    return CollectorError(repository, safe_status,
                          f"http_{safe_status}" if safe_status is not None else "collection_error")


def collect_releases(
    repos: Iterable[str], source: ReleaseSource,
    special_projects: set[str] | None = None,
    seen_event_ids: set[str] | None = None,
    *, config: Mapping[str, int] | None = None,
    initialized_repos: set[str] | None = None,
    reconciliation_cursors: Mapping[str, int] | None = None,
    reconciliation_visits: Mapping[str, int] | None = None,
    start_index: int = 0,
    clock: Callable[[], float] = time.monotonic,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> CollectionResult:
    """Scan bounded recent pages and a deterministic, resumable deep shard.

    All events on completed pages are returned, even previously-seen IDs, so the
    store can refresh edits/draft state. `seen_event_ids` controls stopping only.
    A failed page never advances the reconciliation cursor.
    """
    cfg = dict(DEFAULT_COLLECTOR)
    cfg.update(config or {})
    if any(not isinstance(value, int) or isinstance(value, bool) or value < 1 for value in cfg.values()):
        raise ValueError("collector configuration must contain positive integers")
    if cfg["per_page"] > 100 or cfg["global_budget_seconds"] > 1200:
        raise ValueError("collector per_page or global budget exceeds safe maximum")
    if cfg["reconciliation_pages_per_repo"] < 2 or cfg["reconciliation_pages_special_project"] < 2:
        raise ValueError("reconciliation page caps must allow overlap and forward progress")
    names = [_normalize_repo(repo) for repo in repos]
    if len(set(names)) != len(names):
        raise ValueError("duplicate repository in collection input")
    total = len(names)
    if total:
        start_index %= total
    ordered = names[start_index:] + names[:start_index]
    special = {_normalize_repo(repo) for repo in (special_projects or set())}
    initialized = {_normalize_repo(repo) for repo in (initialized_repos or set())}
    cursors = reconciliation_cursors or {}
    visits = reconciliation_visits or {}
    known = set(seen_event_ids or set())
    emitted: set[str] = set()
    events: list[ReleaseEvent] = []
    errors: list[CollectorError] = []
    updates: list[RepositoryUpdate] = []
    started = completed = deferred = with_release = pages_fetched = observed = 0
    start = clock()
    global_deadline = start + cfg["global_budget_seconds"]
    reserve = min(15.0, cfg["global_budget_seconds"] / 10,
                  cfg["per_repo_budget_seconds"] / 4)
    budget_exhausted = False
    reconciliation_started = reconciliation_deferred = 0
    # A per-invocation key makes short references non-enumerable from a known repo list.
    reference_key = secrets.token_bytes(16)
    fixture = isinstance(source, FixtureReleaseSource)

    def emit(ordinal: int, repo: str, pages: int, status: str) -> None:
        if progress is not None:
            reference = hashlib.blake2s(repo.encode("utf-8"), key=reference_key,
                                          digest_size=6).hexdigest()
            progress({"ordinal": ordinal, "total": total, "reference": reference,
                      "pages": pages, "status": status})

    for ordinal, repository in enumerate(ordered, start=1):
        if clock() >= global_deadline - reserve:
            budget_exhausted = True
            deferred += total - started
            break
        started += 1
        emit(ordinal, repository, 0, "started")
        repo_start = clock()
        repo_deadline = min(global_deadline, repo_start + cfg["per_repo_budget_seconds"])
        repo_pages = 0
        repo_has_release = False
        repo_error = False
        repo_deferred = False
        is_initialized = repository in initialized
        is_special = repository in special
        incremental_cap = (cfg["max_incremental_pages_special_project"] if is_special else
                           cfg["max_incremental_pages_per_repo"]) if is_initialized else cfg["bootstrap_pages"]

        def fetch(page_number: int) -> ReleasePage:
            nonlocal pages_fetched, observed, repo_pages, repo_has_release
            def fetch_and_normalize() -> ReleasePage:
                nonlocal pages_fetched, observed, repo_pages, repo_has_release
                page = source.fetch_page(repository, page_number, cfg["per_page"])
                if page.page_number != page_number or page.request_count < 1:
                    raise ValueError("release source returned an invalid page")
                pages_fetched += page.request_count
                repo_pages += page.request_count
                observed += len(page.releases)
                if page.releases:
                    repo_has_release = True
                for raw in page.releases:
                    event = normalize_release(repository, raw, is_special=is_special, fixture=fixture)
                    if event.event_id not in emitted:
                        events.append(event)
                        emitted.add(event.event_id)
                return page
            if isinstance(source, LiveReleaseSource):
                with _live_page_deadline(repo_deadline - clock()):
                    page = fetch_and_normalize()
            else:
                page = fetch_and_normalize()
            return page

        page_number = 1
        known_only_streak = 0
        last_page: ReleasePage | None = None
        try:
            for _ in range(incremental_cap):
                if clock() >= repo_deadline - reserve:
                    repo_deferred = True
                    break
                # A source page is an atomic visibility boundary. Do not consider
                # a partially normalized page complete.
                previous_count = len(events)
                previous_ids = set(emitted)
                try:
                    page = fetch(page_number)
                except BaseException:
                    del events[previous_count:]
                    emitted.clear()
                    emitted.update(previous_ids)
                    raise
                last_page = page
                if is_initialized and page.releases and all(
                    f"github:release:{_release_id(repository, raw, fixture)}" in known
                    for raw in page.releases
                ):
                    known_only_streak += 1
                else:
                    known_only_streak = 0
                if not page.has_next or (is_initialized and known_only_streak >= cfg["known_only_pages_to_stop"]):
                    break
                page_number += 1
        except BudgetExpired:
            repo_deferred = True
        except Exception as exc:
            errors.append(_safe_error(repository, exc))
            repo_error = True
        if not repo_error and not repo_deferred and not is_initialized:
            updates.append(RepositoryUpdate(repository, initialized=True))
        # A deterministic shard continues deeper than the fast window. A cursor
        # points at the overlap page for the next selected run.
        shards = cfg["special_reconciliation_shards"] if is_special else cfg["reconciliation_shards"]
        visit = int(visits.get(repository, 0))
        selected = (visit + 1) % shards == int(hashlib.sha256(repository.encode("utf-8")).hexdigest(), 16) % shards
        recon_due = is_initialized and selected and not repo_error and not repo_deferred and last_page and last_page.has_next
        recon_cap_deferred = bool(recon_due and reconciliation_started >=
                                  cfg["max_reconciliation_repositories_per_run"])
        recon_unfinished = False
        if recon_cap_deferred:
            reconciliation_deferred += 1
        if recon_due and not recon_cap_deferred:
            reconciliation_started += 1
            cursor = int(cursors.get(repository, 1))
            if cursor < 1:
                raise ValueError("reconciliation cursor must be positive")
            recon_cap = cfg["reconciliation_pages_special_project"] if is_special else cfg["reconciliation_pages_per_repo"]
            last_completed: int | None = None
            reached_terminal = False
            try:
                for page_number in range(cursor, cursor + recon_cap):
                    if clock() >= repo_deadline - reserve:
                        repo_deferred = True
                        break
                    previous_count = len(events)
                    previous_ids = set(emitted)
                    try:
                        page = fetch(page_number)
                    except BaseException:
                        del events[previous_count:]
                        emitted.clear()
                        emitted.update(previous_ids)
                        raise
                    last_completed = page_number
                    if not page.has_next:
                        reached_terminal = True
                        break
            except BudgetExpired:
                repo_deferred = True
            except Exception as exc:
                errors.append(_safe_error(repository, exc))
                repo_error = True
            if not repo_error and last_completed is not None:
                # The last completed page is fetched again next time. If a page
                # shifted at the boundary, the overlap exposes it.
                updates.append(RepositoryUpdate(repository,
                                                reconciliation_cursor=1 if reached_terminal else last_completed))
                if not reached_terminal:
                    reconciliation_deferred += 1
                    recon_unfinished = True
        if is_initialized and not repo_error and not repo_deferred and not recon_cap_deferred and not recon_unfinished:
            updates.append(RepositoryUpdate(repository, reconciliation_visit=visit + 1))
        if repo_has_release:
            with_release += 1
        if repo_error:
            emit(ordinal, repository, repo_pages, "error")
        elif repo_deferred:
            deferred += 1
            if clock() >= global_deadline - reserve:
                budget_exhausted = True
            emit(ordinal, repository, repo_pages, "deferred")
        else:
            completed += 1
            emit(ordinal, repository, repo_pages, "completed")
        if clock() >= global_deadline - reserve:
            budget_exhausted = True
        if budget_exhausted:
            deferred += total - started
            break
    next_index = (start_index + (max(1, started) if budget_exhausted else 1)) % total if total else 0
    return CollectionResult(tuple(events), started, with_release, tuple(errors), total, started,
                            completed, deferred, pages_fetched, observed,
                            max(0.0, clock() - start), budget_exhausted, tuple(updates), next_index,
                            reconciliation_deferred)
