"""Pure, deterministic P1 priority, digest selection and Slack formatting.

The caller owns persistence, rollout gating, transport and acknowledgements.
Nothing in this module can change an event's identity, trust or outbox state.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from .slack_payload import SlackChunk, escape


_RANK = {"SUPPRESSED": 0, "DIGEST": 1, "HIGH": 2, "CRITICAL": 3}
_IMPACT = {
    "none": "DIGEST",
    "low": "DIGEST",
    "medium": "DIGEST",
    "high": "HIGH",
    "critical": "CRITICAL",
}
_SECURITY_TERMS = (
    "remote code execution",
    "rce",
    "authentication bypass",
    "authorization bypass",
    "privilege escalation",
    "arbitrary code execution",
    "sandbox escape",
    "container escape",
    "secret disclosure",
    "supply-chain compromise",
)
_BREAKING_TERMS = (
    "breaking change",
    "api removal",
    "deprecated kubernetes api",
    "crd incompatibility",
    "schema incompatibility",
    "required migration",
)
_TRUSTED_MINIMUM = 85
_KST = timezone(timedelta(hours=9))


def _has_phrase(text: str, phrases: tuple[str, ...]) -> bool:
    return any(
        re.search(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", text)
        for phrase in phrases
    )


def _utc(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else None


def _source_kind(event: dict) -> str:
    kind = str(event.get("event_type") or "release").lower()
    if kind in {
        "ghsa",
        "advisory",
        "github_advisory",
        "github_security_advisory",
        "repository_advisory",
    }:
        return "advisory"
    if kind in {
        "discussion",
        "github_discussion",
        "issue",
        "github_issue",
        "rss",
        "rss_atom",
        "official_rss",
        "announcement",
    }:
        return "announcement"
    return "release"


def _configured_floor(project: dict, kind: str) -> str | None:
    if kind == "announcement":
        return None
    routing = project.get("routing") or {}
    value = routing.get("advisory_floor" if kind == "advisory" else "release_floor")
    if value is None:
        return None
    floor = str(value).upper()
    if floor not in {"DIGEST", "HIGH", "CRITICAL"}:
        raise ValueError("invalid project routing floor")
    return floor


def decide(
    event: dict, project: dict, config: dict, analysis: dict | None = None
) -> dict:
    """Return an auditable routing decision; explicit suppression outranks AI.

    `event` is a normalized raw-event dictionary, `project` is the explicit
    registry policy (or translated legacy policy), and analysis is advisory.
    """
    kind = _source_kind(event)
    policy = config.get("routing") or {}
    version = str(policy.get("policy_version", "v1"))
    reasons: list[str] = []
    suppression: str | None = None
    signals = project.get("signals") or {}
    raw_kind = str(event.get("event_type") or "release").lower()
    source_signal = {
        "discussion": "discussions",
        "github_discussion": "discussions",
        "issue": "issues",
        "github_issue": "issues",
        "rss": "rss",
        "rss_atom": "rss",
        "official_rss": "rss",
    }.get(raw_kind)
    if not project.get("enabled", True) or project.get("tier") == "ignore":
        suppression = "project_ignored"
    elif event.get("candidate") is False:
        suppression = "source_policy_excluded"
    elif event.get("draft"):
        suppression = "draft"
    elif (
        signals.get(kind, True) is False
        or (kind == "announcement" and signals.get("announcement", False) is False)
        or (source_signal and signals.get(source_signal, False) is False)
    ):
        suppression = "signal_disabled"
    elif (
        kind == "release"
        and event.get("prerelease")
        and signals.get("prerelease", True) is False
    ):
        suppression = "prerelease_disabled"
    elif kind == "advisory" and (event.get("project_mapping") or {}).get("status") in {
        "unmapped",
        "ambiguous",
    }:
        suppression = "advisory_unmapped"
    elif kind == "advisory" and event.get("withdrawn_at"):
        suppression = "advisory_withdrawn"
    elif (
        kind == "announcement"
        and int(event.get("source_trust") or 0) < _TRUSTED_MINIMUM
    ):
        suppression = "source_trust_below_threshold"
    destination = str(policy.get("destination_visibility", "private")).lower()
    visibility = str(event.get("visibility") or "unknown").lower()
    if (
        not suppression
        and destination == "public"
        and (
            visibility != "public"
            or event.get(
                "project_visibility",
                "unknown"
                if kind == "advisory"
                and (event.get("project_mapping") or {}).get("status") == "mapped"
                else "public",
            )
            != "public"
        )
    ):
        suppression = "visibility_not_public"

    floor = "DIGEST"
    if kind == "advisory":
        severity = str(
            event.get("severity") or (event.get("metadata") or {}).get("severity") or ""
        ).lower()
        if severity == "critical":
            floor, reasons = "CRITICAL", ["critical_advisory"]
        elif severity == "high":
            floor, reasons = "HIGH", ["high_advisory"]
        elif severity == "medium" and project.get("tier") == "critical":
            floor, reasons = "HIGH", ["medium_advisory_critical_project"]
        else:
            reasons = ["advisory_digest_floor"]
    else:
        reasons = [f"{kind}_digest_floor"]
    explicit_floor = _configured_floor(project, kind)
    if explicit_floor and _RANK[explicit_floor] > _RANK[floor]:
        floor = explicit_floor
        reasons.append("explicit_project_floor")
    if kind == "release" and event.get("is_special") and not explicit_floor:
        floor = "HIGH"
        reasons.append("legacy_special_project")

    # Match phrases/words, not broad substrings. Source trust is supplied by
    # deterministic collectors; model output never enters this calculation.
    text = " ".join(
        str(event.get(key) or "")
        for key in ("title", "release_name", "body", "summary")
    ).lower()
    source_trust = event.get("source_trust")
    if source_trust is None:
        source_trust = 100 if kind in {"release", "advisory"} else 0
    trusted = int(source_trust) >= _TRUSTED_MINIMUM
    if trusted and _has_phrase(text, _SECURITY_TERMS):
        if _RANK[floor] < _RANK["CRITICAL"]:
            floor = "CRITICAL"
        reasons.append("trusted_security_signal")
    elif trusted and _has_phrase(text, _BREAKING_TERMS):
        if _RANK[floor] < _RANK["HIGH"]:
            floor = "HIGH"
        reasons.append("trusted_breaking_signal")

    suggestion = None
    if analysis:
        impact = str(analysis.get("impact") or "").lower()
        suggestion = _IMPACT.get(impact)
    effective = floor
    if (
        suggestion
        and policy.get("allow_ai_promotion", True)
        and _RANK[suggestion] > _RANK[effective]
    ):
        effective = suggestion
        reasons.append("ai_promotion")
    if suppression:
        effective = "SUPPRESSED"
        reasons.append(suppression)
    return {
        "policy_version": version,
        "deterministic_reasons": reasons,
        "deterministic_floor": floor,
        "ai_suggested_priority": suggestion,
        "effective_priority": effective,
        "route": effective,
        "suppression_reason": suppression,
    }


def select(events: list[dict], config: dict, now: datetime) -> list[dict]:
    """Select due pending events without dropping below-threshold digest rows."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    policy = config.get("routing") or {}
    digest = policy.get("digest") or {}
    eligible = [
        e
        for e in events
        if e.get("notification_state") in {"PENDING_NOTIFICATION", "DELIVERY_FAILED"}
        and (e.get("routing") or {}).get("route") in {"CRITICAL", "HIGH", "DIGEST"}
    ]
    immediate = [e for e in eligible if e["routing"]["route"] in {"CRITICAL", "HIGH"}]
    pending_digest = [e for e in eligible if e["routing"]["route"] == "DIGEST"]
    minimum = int(digest.get("min_count", 5))
    maximum_age = float(digest.get("max_age_hours", 24))
    hour_list = digest.get("send_hours_kst", [17])
    due = bool(pending_digest) and (
        any(e["notification_state"] == "DELIVERY_FAILED" for e in pending_digest)
        or len(pending_digest) >= minimum
        or any(
            (queued := _utc(e.get("first_queued_at"))) is not None
            and now.astimezone(timezone.utc) - queued >= timedelta(hours=maximum_age)
            for e in pending_digest
        )
        or now.astimezone(_KST).hour in hour_list
    )
    chosen_ids = {id(e) for e in immediate}
    if due:
        chosen_ids.update(id(e) for e in pending_digest)
    return [e for e in eligible if id(e) in chosen_ids]


def _safe_link(url: object, label: str) -> str:
    value = str(url or "")
    try:
        parsed = urlsplit(value)
    except ValueError:
        return label
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        return label
    if parsed.query or any(ord(char) <= 32 or char in "<>|" for char in value):
        return label
    return f"<{value}|{label}>"


def _line(event: dict, route: str) -> str:
    repository = escape(event.get("repository") or "unknown project")
    title = escape(
        event.get("title")
        or event.get("release_name")
        or event.get("tag_name")
        or event.get("event_type")
        or "Event"
    )
    event_id = escape(event["event_id"])
    source = _safe_link(event.get("url") or event.get("html_url"), "official source")
    details: list[str] = []
    if route == "CRITICAL":
        ghsa = event.get("ghsa_id")
        cve = event.get("cve_id")
        if ghsa:
            details.append(escape(ghsa))
        if cve:
            details.append(escape(cve))
        affected = event.get("affected_versions")
        patched = event.get("patched_versions")
        if affected:
            details.append(
                "affected: "
                + escape(
                    ", ".join(map(str, affected))
                    if isinstance(affected, list)
                    else affected
                )
            )
        if patched:
            details.append(
                "patched: "
                + escape(
                    ", ".join(map(str, patched))
                    if isinstance(patched, list)
                    else patched
                )
            )
    audit = event.get("routing") or {}
    reasons = audit.get("deterministic_reasons") or []
    if reasons:
        details.append("reason: " + escape(", ".join(str(item) for item in reasons)))
    summary = event.get("summary_ko") or event.get("summary")
    if summary:
        details.append(escape(summary))
    actions = event.get("recommended_actions") or []
    if isinstance(actions, list) and actions:
        details.append("review: " + escape(actions[0]))
    tail = " — ".join(details)
    return f"• {repository}: {title} {source} [{event_id}]" + (
        f" — {tail}" if tail else ""
    )


def build_routed_chunks(
    selected: list[dict], max_length: int, config: dict
) -> list[SlackChunk]:
    """Format every selected ID exactly once, split by route and byte-safe text cap."""
    if max_length < 100:
        raise ValueError("max_length too small")
    if not selected:
        return []
    policy = config.get("routing") or {}
    limits = {
        "CRITICAL": 1,
        "HIGH": int((policy.get("high") or {}).get("max_batch_size", 10)),
        "DIGEST": int((policy.get("digest") or {}).get("max_batch_size", 50)),
    }
    headers = {
        "CRITICAL": "🚨 *CRITICAL official signal*\n",
        "HIGH": "⚠️ *HIGH official signals*\n",
        "DIGEST": "📰 *Kubernetes ecosystem digest*\n",
    }
    seen: set[str] = set()
    chunks: list[SlackChunk] = []
    for route in ("CRITICAL", "HIGH", "DIGEST"):
        items = [e for e in selected if (e.get("routing") or {}).get("route") == route]
        if limits[route] < 1:
            raise ValueError("invalid route batch size")
        header = headers[route]
        lines: list[str] = []
        ids: list[str] = []
        for event in items:
            event_id = str(event["event_id"])
            if event_id in seen:
                raise ValueError("duplicate selected event ID")
            seen.add(event_id)
            line = _line(event, route)
            allowed = max_length - len(header) - 1
            line = line[:allowed]
            if lines and (
                len(ids) >= limits[route]
                or len(header) + sum(len(s) + 1 for s in lines) + len(line) + 1
                > max_length
            ):
                chunks.append(SlackChunk({"text": header + "\n".join(lines)}, ids))
                lines, ids = [], []
            lines.append(line)
            ids.append(event_id)
        if lines:
            chunks.append(SlackChunk({"text": header + "\n".join(lines)}, ids))
    if len(seen) != len(selected):
        raise ValueError("selected event has no routable priority")
    return chunks
