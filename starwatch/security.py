"""Output-boundary redaction and fail-closed visibility, without network access."""
from __future__ import annotations

import re
from typing import Any

_SECRET_KEY = re.compile(r"(?:authorization|password|passwd|secret|token|api[_-]?key|webhook)", re.I)
_PATTERNS = (
    (r"(?i)(authorization\s*:\s*(?:bearer|token|basic)\s+)[^\s,;\"']+", r"\1[REDACTED]"),
    (r"(?i)((?:[a-z_]*token|[a-z_]*password|[a-z_]*secret|[a-z_]*api[_-]?key|slack_webhook_url)\s*[=:]\s*)[^\s&,;\"'<>]+", r"\1[REDACTED]"),
    (r"https://hooks\.slack\.com/services/[^\s<>\"']+", "[REDACTED_WEBHOOK]"),
    (r"\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|xox[baprs]-[A-Za-z0-9-]+)\b", "[REDACTED_TOKEN]"),
    (r"(https?://)[^\s/@]+@", r"\1[REDACTED]@"),
)


def redact_text(value: str) -> str:
    value = re.sub(r'(?i)(["\'](?:[a-z_]*token|[a-z_]*password|[a-z_]*secret|[a-z_]*api[_-]?key|authorization|slack_webhook_url)["\']\s*:\s*)["\'][^"\']*["\']',
                   r'\1"[REDACTED]"', value)
    for pattern, replacement in _PATTERNS:
        value = re.sub(pattern, replacement, value)
    return value


def redact(value: Any) -> Any:
    """Remove complete sensitive values, including nested JSON secret fields."""
    if isinstance(value, dict):
        return {key: "[REDACTED]" if _SECRET_KEY.search(str(key)) else redact(item)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    return redact_text(value) if isinstance(value, str) else value


def visibility(item: dict) -> str:
    declared = item.get("visibility")
    if declared in {"private", "internal"}:
        return declared
    if item.get("private") is True:
        return "private"
    if declared == "public":
        return "public"
    return "unknown"


def export_allowed(item: dict, *, include_private: bool = False,
                   destination: str = "public") -> bool:
    if destination not in {"public", "private"}:
        raise ValueError("invalid export destination")
    if include_private and destination != "private":
        raise ValueError("private export requires a private destination")
    scope = visibility(item)
    return scope == "public" or (include_private and scope in {"private", "internal"})
