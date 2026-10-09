#!/usr/bin/env python3
"""Read-only, visibility-gated Knowledge JSONL export for fordongdorrong."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from starwatch.event_store import EventStore  # noqa: E402
from starwatch.security import export_allowed, redact, visibility  # noqa: E402

DEFAULT_FEED = Path(".cache/release-feed.json")
DOCUMENT_SCHEMA = "github-stars-knowledge/v2"


@dataclass(frozen=True)
class KnowledgeDocument:
    # Existing fordongdorrong envelope is retained; v2 fields are additive.
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
    schema_version: str = DOCUMENT_SCHEMA
    event_id: str | None = None
    document_type: str = "raw_event"
    project: str | None = None
    categories: list[str] = field(default_factory=list)
    related_document_ids: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_timestamp(value: str | None) -> str:
    if value and "T" in value:
        return value if value.endswith("Z") or "+" in value else f"{value}Z"
    if value:
        return value.replace(" ", "T") + "Z"
    return "1970-01-01T00:00:00Z"


def _safe(value: Any) -> Any:
    return redact(value)


def _require_event(value: Any, *, row_id: str | None = None) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("malformed event document")
    event_id = value.get("event_id")
    if row_id is not None and event_id != row_id:
        raise ValueError("event identity mismatch")
    if event_id is not None and (not isinstance(event_id, str) or not event_id):
        raise ValueError("malformed event identity")
    for key in ("repository", "repo", "title", "body", "release_name", "html_url", "published_at", "content_hash"):
        if key in value and not isinstance(value[key], str):
            raise ValueError("malformed event field")
    return value


def _permitted(item: dict[str, Any], *, include_private: bool,
               destination: str) -> bool:
    return export_allowed(item, include_private=include_private, destination=destination)


def _event_document(event: dict[str, Any], *, feed: dict[str, Any] | None = None) -> KnowledgeDocument:
    event = _safe(event)
    repo = str(event.get("repository") or event.get("repo") or "")
    event_id = event.get("event_id")
    tag = str(event.get("tag_name") or event.get("tag") or "")
    title = str(event.get("title") or event.get("release_name") or event.get("name") or tag or event_id or "Official event")
    body_text = str(event.get("body") or event.get("description") or "")
    published = normalize_timestamp(str(event.get("published_at") or event.get("published") or ""))
    updated = normalize_timestamp(str(event.get("updated_at") or (feed or {}).get("generated_at") or published))
    uri = str(event.get("html_url") or event.get("url") or "")
    text = f"{title}\n{body_text}" if body_text else title
    owner, repo_name = repo.split("/", 1) if "/" in repo else ("", repo)
    metadata = {
        "owner": owner, "repo": repo, "repo_name": repo_name,
        "source_id": event.get("source_id") or event_id,
        "published_at": published, "updated_at": updated,
        "tag_name": tag, "is_special": bool(event.get("is_special", False)),
        "draft": bool(event.get("draft", False)),
        "prerelease": bool(event.get("prerelease", False)),
        "provenance": event.get("provenance") or {
            "collector": (event.get("raw_metadata") or {}).get("collector"),
            "api_resource_id": (event.get("raw_metadata") or {}).get("api_resource_id"),
        },
        "advisory": {key: event[key] for key in (
            "ghsa_id", "cve_id", "identifiers", "severity", "cvss", "cvss_severities",
            "epss", "cwes", "vulnerabilities", "references", "withdrawn_at",
            "project_mapping", "repository_advisory_url", "source_code_location") if key in event},
    }
    if feed is not None:
        metadata["notify_reason"] = feed.get("notify_reason", "")
        metadata["feed_schema_version"] = feed.get("schema_version", "")
    if event.get("metadata") and str(event.get("event_type", "")).endswith("advisory"):
        raw = event["metadata"]
        if isinstance(raw, dict):
            metadata["advisory"].update({key: raw[key] for key in (
                "ghsa_id", "cve_id", "identifiers", "severity", "cvss", "cvss_severities",
                "epss", "cwes", "vulnerabilities", "references", "withdrawn_at",
                "repository_advisory_url", "source_code_location") if key in raw})
    metadata = _safe(metadata)
    scope = visibility(event)
    if event_id:
        document_id = "event:" + event_id
    else:
        # Legacy feed fixtures predate GitHub Release IDs. Preserve their old
        # fordongdorrong document IDs for one compatibility period.
        document_id = f"releases/{repo.strip('/')}/{tag.strip('/')}"
    categories = event.get("categories")
    if not isinstance(categories, list):
        categories = ["security"] if "advisory" in str(event.get("event_type", "")) else ["maintenance"]
    return KnowledgeDocument(
        source_id="github-stars", document_id=document_id, title=title,
        body=text, uri=uri,
        content_hash=str(event.get("content_hash") or sha256_text(text)),
        created_at=published, updated_at=updated, visibility=scope,
        lifecycle="withdrawn" if event.get("withdrawn_at") else "active",
        deleted_at=event.get("withdrawn_at"), indexable=bool(event_id or (repo and tag)),
        metadata=metadata, event_id=event_id, document_type="raw_event",
        project=repo or None, categories=categories,
    )


def document_from_release(release: dict[str, Any], feed: dict[str, Any]) -> KnowledgeDocument:
    return _event_document(_require_event(release), feed=feed)


def load_feed(path: Path) -> dict[str, Any]:
    try:
        feed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("release feed is malformed or unavailable") from exc
    if not isinstance(feed, dict) or feed.get("schema_version") != "github-stars-release-feed/v1":
        raise ValueError("unsupported release feed schema")
    if "releases" not in feed or not isinstance(feed["releases"], list):
        raise ValueError("release feed must include releases[]")
    if "new_releases" in feed and feed["new_releases"] != feed["releases"]:
        raise ValueError("release discovery alias mismatch")
    return feed


def export_documents(feed_path: Path, *, include_private: bool = False,
                     destination: str = "public") -> list[KnowledgeDocument]:
    feed = load_feed(feed_path)
    result = []
    for release in feed["releases"]:
        event = _require_event(release)
        if _permitted(event, include_private=include_private, destination=destination):
            result.append(document_from_release(event, feed))
    return sorted(result, key=lambda doc: doc.document_id)


def _readonly_database(path: Path) -> sqlite3.Connection:
    if not path.is_file() or path.stat().st_size == 0:
        raise ValueError("event database is missing or empty")
    # immutable avoids creating journal/WAL sidecars. Export a quiescent DB
    # snapshot, not a live writer's uncheckpointed WAL.
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    db.row_factory = sqlite3.Row
    try:
        EventStore._validate(db)
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
    except Exception:
        db.close()
        raise
    return db


def _parsed_json(raw: str, kind: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"malformed {kind} JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"malformed {kind} JSON")
    return value


def _current_visibility(db: sqlite3.Connection, event: dict[str, Any]) -> dict[str, Any]:
    """Use the latest inventory scope for repository-origin events.

    A formerly public repository may become private while an older raw/revision
    payload still says public. The metadata value is written by the current
    inventory reconciliation and must override that historical claim.
    """
    if event.get("event_type") not in {"github_release", "github_issue", "github_discussion", "rss", "rss_atom", "official_rss"}:
        return event  # A Global GHSA is a public source, independent of repo scope.
    repo = event.get("repository")
    if not isinstance(repo, str) or not repo:
        return event
    row = db.execute("SELECT value FROM state_metadata WHERE key=?",
                     ("repository_visibility:" + repo.lower(),)).fetchone()
    if row is None:
        return event
    updated = dict(event)
    updated["visibility"] = row[0] if row[0] in {"public", "private", "internal", "unknown"} else "unknown"
    return updated


def _db_permitted(db: sqlite3.Connection, event: dict[str, Any], *,
                  include_private: bool, destination: str) -> bool:
    if not _permitted(event, include_private=include_private, destination=destination):
        return False
    # A Global GHSA is itself public, but a deterministic mapping can expose
    # a currently private project name in title/metadata/linked documents.
    # Exclude the whole linked cohort from public export rather than trying to
    # redact one occurrence of that name in an untrusted advisory body.
    mapping = event.get("project_mapping")
    if destination == "public" and isinstance(mapping, dict) and mapping.get("status") == "mapped":
        project = mapping.get("project")
        if not isinstance(project, str) or not project:
            return False
        scopes = [row[0] for key in ("project_visibility:", "repository_visibility:")
                  if (row := db.execute("SELECT value FROM state_metadata WHERE key=?",
                                        (key + project.lower(),)).fetchone()) is not None]
        if not scopes or any(scope != "public" for scope in scopes):
            return False
    return True


def _analysis_document(event: dict[str, Any], analysis: dict[str, Any],
                       event_doc: KnowledgeDocument, revision_hashes: set[str]) -> KnowledgeDocument:
    from starwatch.analysis import validate_analysis

    validate_analysis(analysis)
    if analysis["event_id"] != event["event_id"]:
        raise ValueError("analysis event identity mismatch")
    if analysis["event_content_hash"] not in revision_hashes:
        raise ValueError("analysis content hash has no event revision")
    analysis = _safe(analysis)
    doc_id = (f"analysis:{event['event_id']}:{analysis['event_content_hash']}:"
              f"{analysis['prompt_version']}:{analysis['provider']}:{analysis['model']}")
    content = analysis["summary_ko"] + "\n" + analysis["reason"]
    related = (event_doc.document_id if analysis["event_content_hash"] == event["content_hash"]
               else f"revision:{event['event_id']}:{analysis['event_content_hash']}")
    return KnowledgeDocument(
        source_id="github-stars", document_id=doc_id,
        title="Analysis: " + event_doc.title, body=content,
        uri=event_doc.uri, content_hash=sha256_text(content),
        created_at=analysis["created_at"], updated_at=analysis["created_at"],
        visibility=event_doc.visibility, lifecycle=event_doc.lifecycle,
        deleted_at=event_doc.deleted_at, indexable=True,
        metadata={"analysis": analysis, "parent_document_id": related},
        event_id=event["event_id"], document_type="ai_analysis", project=event_doc.project,
        categories=list(analysis["categories"]), related_document_ids=[related],
    )


def export_database(path: Path, *, include_private: bool = False,
                    destination: str = "public") -> list[KnowledgeDocument]:
    db = _readonly_database(path)
    try:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        documents: list[KnowledgeDocument] = []
        events: dict[str, tuple[dict[str, Any], KnowledgeDocument]] = {}
        revision_hashes: dict[str, set[str]] = {}
        for row in db.execute("SELECT event_id, content_hash, payload_json FROM events ORDER BY event_id"):
            event = _current_visibility(db, _require_event(_parsed_json(row["payload_json"], "event"), row_id=row["event_id"]))
            if event.get("content_hash") != row["content_hash"]:
                raise ValueError("event content hash mismatch")
            if not _db_permitted(db, event, include_private=include_private, destination=destination):
                continue
            doc = _event_document(event)
            events[row["event_id"]] = (event, doc)
            revision_hashes[row["event_id"]] = {row["content_hash"]}
            documents.append(doc)
        if version >= 2:
            for row in db.execute("SELECT event_id, content_hash, payload_json, observed_at FROM event_revisions ORDER BY event_id, content_hash"):
                if row["event_id"] not in events:
                    continue
                old = _current_visibility(db, _require_event(_parsed_json(row["payload_json"], "revision"), row_id=row["event_id"]))
                if old.get("content_hash") != row["content_hash"]:
                    raise ValueError("revision content hash mismatch")
                _, parent = events[row["event_id"]]
                if not _db_permitted(db, old, include_private=include_private, destination=destination):
                    continue
                revision_hashes[row["event_id"]].add(row["content_hash"])
                revision = _event_document(old)
                documents.append(KnowledgeDocument(
                    source_id="github-stars", document_id=f"revision:{row['event_id']}:{row['content_hash']}",
                    title="Revision: " + revision.title, body=revision.body,
                    uri=revision.uri, content_hash=row["content_hash"],
                    created_at=row["observed_at"], updated_at=row["observed_at"],
                    visibility=revision.visibility, lifecycle=revision.lifecycle,
                    deleted_at=revision.deleted_at, indexable=False,
                    metadata={"provenance": revision.metadata.get("provenance"),
                              "parent_document_id": parent.document_id,
                              "observed_at": row["observed_at"]},
                    event_id=row["event_id"], document_type="revision", project=revision.project,
                    categories=revision.categories, related_document_ids=[parent.document_id],
                ))
            for row in db.execute("SELECT event_id, analysis_json FROM ai_analyses ORDER BY event_id, event_content_hash, schema_version, prompt_version, provider, model"):
                if row["event_id"] not in events:
                    continue
                event, parent = events[row["event_id"]]
                documents.append(_analysis_document(event, _parsed_json(row["analysis_json"], "analysis"), parent,
                                                    revision_hashes[row["event_id"]]))
        return sorted(documents, key=lambda doc: doc.document_id)
    finally:
        db.close()


def write_jsonl(documents: Iterable[KnowledgeDocument], output: Path | None) -> None:
    lines = [document.to_json() for document in documents]
    payload = "\n".join(lines) + ("\n" if lines else "")
    if output is None:
        print(payload, end="")
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".knowledge-", dir=output.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(payload)
        os.replace(temporary, output)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export visibility-safe GitHub Stars Knowledge JSONL")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--feed", type=Path, default=None, help="Release feed path")
    group.add_argument("--db", type=Path, help="Read-only event DB snapshot")
    parser.add_argument("--output", type=Path, help="JSONL path (otherwise stdout)")
    parser.add_argument("--destination", choices=("public", "private"), default="public")
    parser.add_argument("--include-private", action="store_true", help="Requires --destination private and --output")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.include_private and (args.destination != "private" or args.output is None):
        raise ValueError("private export requires explicit private destination and output file")
    source = args.db or args.feed or DEFAULT_FEED
    if args.output is not None and args.output.resolve() in {source.resolve(), DEFAULT_FEED.resolve(), Path(".cache/events.sqlite3").resolve(), Path(".cache/releases.json").resolve()}:
        raise ValueError("export output cannot overwrite source or state")
    documents = (export_database(source, include_private=args.include_private, destination=args.destination)
                 if args.db else export_documents(source, include_private=args.include_private, destination=args.destination))
    write_jsonl(documents, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
