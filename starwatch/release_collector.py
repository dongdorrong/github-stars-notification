"""Complete GitHub Release collection with stable IDs and isolated repo failures.

The live adapter iterates PyGithub's PaginatedList to exhaustion. There is no
timestamp/first-seen early stop: GitHub does not promise a monotonic publication
order, and backdated releases must still be discovered.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol

from .models import ReleaseEvent


class ReleaseSource(Protocol):
    def get_releases(self, repository: str) -> Iterable[Mapping[str, Any]]: ...


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


class FixtureSourceError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


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

    def get_releases(self, repository: str) -> Iterable[Mapping[str, Any]]:
        value = self._releases.get(_normalize_repo(repository))
        if value is None or value is False:
            return ()
        if isinstance(value, dict) and "error" in value:
            error = value["error"]
            if not isinstance(error, dict):
                raise ValueError("fixture error must be an object")
            raise FixtureSourceError(int(error["status"]), str(error.get("message", "fixture error")))
        if isinstance(value, dict) and "pages" in value:
            pages = value["pages"]
            if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
                raise ValueError("fixture pages must be a list of lists")
            releases = [item for page in pages for item in page]
        elif isinstance(value, list):
            releases = value
        elif isinstance(value, dict):
            releases = [value]
        else:
            raise ValueError(f"fixture releases for {repository} must be an object or list")
        if any(not isinstance(item, dict) for item in releases):
            raise ValueError(f"fixture releases for {repository} contain a non-object")
        return releases


class LiveReleaseSource:
    """PyGithub adapter; injected client permits token-free adapter tests."""

    def __init__(self, token: str | None = None, *, client: Any | None = None):
        if client is None:
            if not token:
                raise ValueError("GitHub token is required for live collection")
            try:
                from github import Github  # type: ignore
            except ModuleNotFoundError as exc:
                raise RuntimeError("PyGithub is required for live collection") from exc
            client = Github(token, per_page=100)
        self._client = client

    def get_releases(self, repository: str) -> Iterable[Mapping[str, Any]]:
        github_repo = self._client.get_repo(repository)
        # Iteration, not get_page(0), consumes every page of PyGithub's PaginatedList.
        for release in github_repo.get_releases():
            raw = release.raw_data
            if not isinstance(raw, dict):
                raise ValueError(f"GitHub release for {repository} has no raw object")
            yield raw


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


def collect_releases(
    repos: Iterable[str],
    source: ReleaseSource,
    special_projects: set[str] | None = None,
    seen_event_ids: set[str] | None = None,
) -> CollectionResult:
    """Collect all pages for every repo, isolating failures without partial repo results."""
    special_projects = {_normalize_repo(repo) for repo in (special_projects or set())}
    seen = set(seen_event_ids or set())
    fixture = isinstance(source, FixtureReleaseSource)
    collected: list[ReleaseEvent] = []
    errors: list[CollectorError] = []
    scanned = 0
    with_release = 0

    for original_repo in repos:
        repository = _normalize_repo(original_repo)
        scanned += 1
        local: list[ReleaseEvent] = []
        try:
            for raw in source.get_releases(repository):
                local.append(normalize_release(repository, raw, is_special=repository in special_projects, fixture=fixture))
        except Exception as exc:
            # In particular, late-page API failures must not leak a partial cursor.
            # Exception text from HTTP clients can contain response bodies or headers.
            status = getattr(exc, "status", None)
            safe_status = status if isinstance(status, int) and not isinstance(status, bool) else None
            category = f"http_{safe_status}" if safe_status is not None else "collection_error"
            errors.append(CollectorError(repository, safe_status, category))
            continue
        if local:
            with_release += 1
        for event in local:
            if event.event_id not in seen:
                collected.append(event)
                seen.add(event.event_id)

    return CollectionResult(tuple(collected), scanned, with_release, tuple(errors))
