"""Normalized, source-preserving events for the durable release pipeline."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class ReleaseEvent:
    event_id: str
    event_type: str
    source_id: str
    repository: str
    release_id: int | str
    tag_name: str
    release_name: str
    body: str
    html_url: str
    published_at: str
    created_at: str
    updated_at: str
    draft: bool
    prerelease: bool
    is_special: bool
    content_hash: str
    raw_metadata: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
