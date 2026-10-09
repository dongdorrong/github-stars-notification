"""Opt-in, public-only local artifacts. This module never uploads or reads state."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .security import redact, visibility
from scripts.export_knowledge_jsonl import document_from_release


ARTIFACT_SCHEMA = "github-stars-public-artifacts/v1"
_PRODUCTS = {
    "inventory": "inventory-public.json",
    "release_feed": "release-feed-public.json",
    "knowledge_export": "knowledge-public.jsonl",
}
_REPO = re.compile(r"^[a-z0-9][a-z0-9_.-]*/[a-z0-9][a-z0-9_.-]*$")
_EVENT_IDS = {
    "github_release": re.compile(r"^github:release:[0-9]+$"),
    "github_security_advisory": re.compile(r"^github:ghsa:GHSA-[A-Za-z0-9]{4}-[A-Za-z0-9]{4}-[A-Za-z0-9]{4}$"),
    "github_issue": re.compile(r"^github:issue:[0-9]+:[0-9]+$"),
    "github_discussion": re.compile(r"^github:discussion:[A-Za-z0-9_=-]{1,128}$"),
    "rss": re.compile(r"^rss:[0-9a-f]{64}$"),
    "official_rss": re.compile(r"^rss:[0-9a-f]{64}$"),
}
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})$")


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(redact(value), sort_keys=True, ensure_ascii=False,
                       separators=(",", ":")) + "\n").encode("utf-8")


def _enabled(config: dict) -> dict[str, bool]:
    if not isinstance(config, dict):
        raise ValueError("artifact configuration is malformed")
    section = config.get("artifacts", {})
    if not isinstance(section, dict) or set(section) - set(_PRODUCTS):
        raise ValueError("artifact configuration is malformed")
    result = {}
    for product in _PRODUCTS:
        policy = section.get(product, {})
        if not isinstance(policy, dict) or set(policy) - {"enabled", "public_only"}:
            raise ValueError("artifact policy is malformed")
        enabled = policy.get("enabled", False)
        public_only = policy.get("public_only", True)
        if type(enabled) is not bool or type(public_only) is not bool:
            raise ValueError("artifact policy is malformed")
        if enabled and not public_only:
            raise ValueError("public artifact requires public_only=true")
        result[product] = enabled
    return result


def _inventory(items: list[dict]) -> tuple[list[dict], dict[str, str]]:
    if not isinstance(items, list):
        raise ValueError("inventory is malformed")
    public: list[dict] = []
    scopes: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("inventory is malformed")
        name = item.get("full_name")
        if not isinstance(name, str) or not _REPO.fullmatch(name.lower()):
            raise ValueError("inventory identity is malformed")
        normalized = name.lower()
        if normalized in scopes:
            raise ValueError("duplicate inventory identity")
        scope = visibility(item)
        scopes[normalized] = scope
        if scope == "public":
            repo_id = item.get("id")
            if type(repo_id) is not int or repo_id <= 0:
                raise ValueError("public inventory ID is malformed")
            public.append({"id": repo_id, "full_name": normalized, "visibility": "public"})
    return sorted(public, key=lambda row: row["full_name"]), scopes


def _event(item: dict, scopes: dict[str, str], *, allow_signals: bool = False) -> dict | None:
    if not isinstance(item, dict):
        raise ValueError("release feed event is malformed")
    event_type = item.get("event_type", "github_release")
    if event_type != "github_release" and not allow_signals:
        raise ValueError("release feed contains a non-release event")
    pattern = _EVENT_IDS.get(event_type) if isinstance(event_type, str) else None
    if pattern is None:
        raise ValueError("unsupported public event type")
    repository = item.get("repository") or item.get("repo")
    if not isinstance(repository, str) or not _REPO.fullmatch(repository.lower()):
        raise ValueError("release feed repository is malformed")
    repository = repository.lower()
    if visibility(item) != "public" or scopes.get(repository) != "public":
        return None
    event_id, content_hash, published = (item.get("event_id"), item.get("content_hash"),
                                         item.get("published_at"))
    if not isinstance(event_id, str) or not pattern.fullmatch(event_id):
        raise ValueError("public event identity is malformed")
    if not isinstance(content_hash, str) or not _HASH.fullmatch(content_hash):
        raise ValueError("public event content hash is malformed")
    if not isinstance(published, str) or not _DATE.fullmatch(published):
        raise ValueError("public event timestamp is malformed")
    return {"event_id": event_id, "event_type": event_type,
            "repository": repository, "published_at": published,
            "content_hash": content_hash, "visibility": "public"}


def _feed(feed: dict, scopes: dict[str, str]) -> dict:
    if not isinstance(feed, dict) or feed.get("schema_version") != "github-stars-release-feed/v1":
        raise ValueError("release feed schema is malformed")
    if not isinstance(feed.get("releases"), list):
        raise ValueError("release feed discoveries are malformed")
    if "new_releases" in feed and feed["new_releases"] != feed["releases"]:
        raise ValueError("release discovery alias mismatch")
    result = {"schema_version": "github-stars-release-feed-public/v1"}
    for source, target in (("releases", "new_releases"),
                           ("pending_releases", "pending_releases"),
                           ("notification_batch", "notification_batch")):
        items = feed.get(source, [])
        if not isinstance(items, list):
            raise ValueError("release feed batch is malformed")
        selected = [public for item in items
                    if (public := _event(item, scopes,
                                         allow_signals=source == "notification_batch")) is not None]
        result[target] = selected
        result[target.removesuffix("s") + "_count"] = len(selected)
    # No untrusted release body/title, Slack payload, URLs or raw error text.
    return result


def _knowledge(releases: list[dict], scopes: dict[str, str]) -> bytes:
    # Reuse the fordongdorrong envelope. Only the explicit public Release
    # fields below may reach it; nested raw API metadata never crosses here.
    documents = []
    nonpublic = sorted((name for name, scope in scopes.items() if scope != "public"),
                       key=len, reverse=True)

    def safe_text(value: str) -> str:
        result = redact(value)
        for name in nonpublic:
            result = re.sub(re.escape(name), "[REDACTED_REPOSITORY]", result, flags=re.I)
        return result

    for item in releases:
        safe = _event(item, scopes)
        if safe is None:
            continue
        for key in ("tag_name", "release_name", "body", "html_url"):
            if key in item and not isinstance(item[key], str):
                raise ValueError("knowledge source field is malformed")
        uri = item.get("html_url") or ""
        if uri:
            try:
                parsed = urlsplit(uri)
                allowed = (parsed.scheme == "https" and parsed.hostname == "github.com"
                           and not parsed.username and not parsed.password and not parsed.port
                           and parsed.path.startswith("/" + safe["repository"] + "/releases/"))
            except ValueError:
                allowed = False
            if not allowed:
                uri = ""
            else:
                uri = urlunsplit(("https", "github.com", parsed.path, "", ""))
        rich = {**safe, "tag_name": safe_text(item.get("tag_name", "")),
                "release_name": safe_text(item.get("release_name", "")),
                "body": safe_text(item.get("body", "")), "html_url": uri,
                "provenance": {"collector": "github-releases",
                               "api_resource_id": safe["event_id"].rsplit(":", 1)[-1]}}
        documents.append(document_from_release(rich, {"schema_version": "github-stars-release-feed/v1"}))
    return b"".join((doc.to_json() + "\n").encode("utf-8")
                    for doc in sorted(documents, key=lambda value: value.document_id))


def export_artifacts(config: dict, inventory: list[dict], feed: dict,
                     output_dir: Path) -> dict | None:
    """Create only explicitly enabled sanitized files, returning a safe manifest.

    No network, SQLite, state, cache or upload operations are performed.
    Existing different file bytes (or unknown files) cause a fail-closed error.
    """
    enabled = _enabled(config)
    if not any(enabled.values()):
        return None
    public, scopes = _inventory(inventory)
    safe_feed = _feed(feed, scopes)
    files: dict[str, bytes] = {}
    if enabled["inventory"]:
        files[_PRODUCTS["inventory"]] = _json_bytes({
            "schema_version": "github-stars-inventory-public/v1",
            "repositories": public, "repository_count": len(public)})
    if enabled["release_feed"]:
        files[_PRODUCTS["release_feed"]] = _json_bytes(safe_feed)
    if enabled["knowledge_export"]:
        files[_PRODUCTS["knowledge_export"]] = _knowledge(feed["releases"], scopes)
    manifest = {"schema_version": ARTIFACT_SCHEMA,
                "files": sorted(files), "public_repository_count": len(public),
                "public_new_release_count": len(safe_feed["new_releases"]),
                "excluded_repository_count": len(scopes) - len(public)}
    files["manifest.json"] = _json_bytes(manifest)
    root = Path(output_dir)
    if root.exists():
        if not root.is_dir() or root.is_symlink():
            raise ValueError("artifact destination is unsafe")
        if any(path.name not in files or path.is_symlink() or not path.is_file()
               for path in root.iterdir()):
            raise ValueError("artifact destination contains unknown files")
        if any((root / name).exists() and (root / name).read_bytes() != data
               for name, data in files.items()):
            raise ValueError("artifact destination contains different content")
    else:
        root.mkdir(parents=True)
    for name, data in files.items():
        target = root / name
        if not target.exists():
            with target.open("xb") as stream:
                stream.write(data)
    return manifest
