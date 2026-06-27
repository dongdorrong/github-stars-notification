#!/usr/bin/env python3
"""Export cached GitHub release feed as fordongdorrong Knowledge JSONL.

This command is read-only. It consumes an existing `.cache/release-feed.json`
or a supplied feed fixture and never calls the GitHub API or sends Slack
notifications.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

DEFAULT_FEED = Path(".cache/release-feed.json")
SECRET_MARKERS = (
    r"authorization:\s*bearer",
    r"api[_-]?key\s*[=:]",
    r"access_token\s*[=:]",
    r"client_secret\s*[=:]",
    r"github_token\s*[=:]",
    r"slack_webhook\s*[=:]",
    r"webhook_url\s*[=:]",
    r"password\s*[=:]",
    r"ghp_",
    r"xoxb-",
)


@dataclass(frozen=True)
class KnowledgeDocument:
    source_id: str
    document_id: str
    title: str
    body: str
    uri: str
    content_hash: str
    created_at: str | None
    updated_at: str
    visibility: str
    lifecycle: str
    deleted_at: str | None
    indexable: bool
    metadata: dict[str, Any]

    def to_json(self) -> str:
        return json.dumps(self.__dict__, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def redact_secret_like_text(text: str) -> str:
    redacted = text
    for marker in SECRET_MARKERS:
        redacted = re.sub(marker, "[redacted-sensitive-marker]", redacted, flags=re.IGNORECASE)
    return redacted


def normalize_timestamp(value: str | None) -> str:
    if value and "T" in value:
        return value if value.endswith("Z") or "+" in value else f"{value}Z"
    if value:
        return value.replace(" ", "T") + "Z"
    return "1970-01-01T00:00:00Z"


def load_feed(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _release_value(release: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = release.get(key)
        if value:
            return str(value)
    return ""


def document_from_release(release: dict[str, Any], feed: dict[str, Any]) -> KnowledgeDocument:
    repo = _release_value(release, "repo", "repository")
    tag = _release_value(release, "tag", "tag_name")
    name = _release_value(release, "name", "release_name", "title") or tag
    published = normalize_timestamp(_release_value(release, "published", "published_at"))
    html_url = _release_value(release, "html_url", "url") or f"https://github.com/{repo}/releases/tag/{tag}"
    owner, repo_name = repo.split("/", 1) if "/" in repo else ("", repo)
    body = "\n".join(
        [
            f"Repository: {repo}",
            f"Tag: {tag}",
            f"Release name: {name}",
            f"Published: {published}",
            f"Notification reason: {feed.get('notify_reason', '')}",
            "This Knowledge export is generated from a cached release feed and does not call GitHub live APIs.",
        ]
    )
    body = redact_secret_like_text(body)
    safe_repo = repo.strip("/")
    safe_tag = tag.strip("/")
    return KnowledgeDocument(
        source_id="github-stars",
        document_id=f"releases/{safe_repo}/{safe_tag}",
        title=f"{repo} {tag}".strip(),
        body=body,
        uri=html_url,
        content_hash=sha256_text(body),
        created_at=published,
        updated_at=normalize_timestamp(str(feed.get("generated_at") or published)),
        visibility="public",
        lifecycle="active",
        deleted_at=None,
        indexable=bool(repo and tag),
        metadata={
            "owner": owner,
            "repo": repo,
            "repo_name": repo_name,
            "tag_name": tag,
            "published_at": published,
            "is_special": bool(release.get("is_special", False)),
            "notify_reason": str(feed.get("notify_reason", "")),
            "feed_schema_version": str(feed.get("schema_version", "")),
        },
    )


def export_documents(feed_path: Path) -> list[KnowledgeDocument]:
    feed = load_feed(feed_path)
    releases = feed.get("releases") or []
    if not isinstance(releases, list):
        raise ValueError("release feed must include releases[]")
    return [document_from_release(release, feed) for release in releases if isinstance(release, dict)]


def write_jsonl(documents: Iterable[KnowledgeDocument], output: Path | None) -> None:
    lines = [document.to_json() for document in documents]
    text = "\n".join(lines) + ("\n" if lines else "")
    if output is None:
        print(text, end="")
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export cached GitHub Stars release feed as Knowledge JSONL")
    parser.add_argument("--feed", type=Path, default=DEFAULT_FEED, help=f"Release feed path (default: {DEFAULT_FEED})")
    parser.add_argument("--output", type=Path, default=None, help="Write JSONL to this file instead of stdout")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    write_jsonl(export_documents(args.feed), args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
