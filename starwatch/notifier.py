"""Slack transport and ack coordinator. Tests inject a fake transport."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol

from .event_store import now
from .slack_payload import SlackChunk


@dataclass(frozen=True)
class SlackResult:
    status_code: int | None
    retry_after: int | None = None
    error: str | None = None

    @property
    def success(self) -> bool:
        return self.status_code is not None and 200 <= self.status_code < 300


class SlackTransport(Protocol):
    def send(self, payload: dict[str, str]) -> SlackResult: ...


class WebhookTransport:
    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url

    def send(self, payload: dict[str, str]) -> SlackResult:
        request = urllib.request.Request(self.webhook_url, data=json.dumps(payload).encode(),
                                         headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return SlackResult(response.status)
        except urllib.error.HTTPError as exc:
            raw_retry = exc.headers.get("Retry-After")
            retry = int(raw_retry) if raw_retry and raw_retry.isdecimal() else None
            return SlackResult(exc.code, retry, "http_error")
        except (urllib.error.URLError, TimeoutError, OSError):
            return SlackResult(None, error="transport_error")


def deliver(store, chunks: list[SlackChunk], transport: SlackTransport) -> bool:
    if (deadline := store.get_meta("slack_retry_after")) and deadline > now():
        return False
    all_success = True
    for index, chunk in enumerate(chunks):
        if not store.claim(chunk.event_ids):
            all_success = False
            continue
        try:
            result = transport.send(chunk.payload)
        except (urllib.error.URLError, TimeoutError, OSError):
            result = SlackResult(None, error="transport_error")
        except Exception:
            # Unexpected fake/adapter failures are bugs, not ordinary HTTP
            # outcomes. Keep the batch retryable but surface a sanitized error.
            store.fail(chunk.event_ids, "unexpected_transport_error")
            raise RuntimeError("unexpected Slack transport failure") from None
        if result.success:
            store.acknowledge(chunk.event_ids)
        else:
            category = "rate_limited" if result.status_code == 429 else (
                "server_error" if result.status_code and result.status_code >= 500 else
                "client_error" if result.status_code else "transport_error")
            store.fail(chunk.event_ids, category, result.retry_after,
                       endpoint_backoff=result.status_code == 429)
            all_success = False
            if result.status_code == 429:
                # A webhook-wide limit applies to remaining chunks too. Leave
                # them unsent but retry-marked, even if the first chunk is
                # later suppressed and the remainder falls below threshold.
                remaining_ids = [event_id for later in chunks[index + 1:]
                                 for event_id in later.event_ids]
                store.defer_after_rate_limit(remaining_ids)
                break
    return all_success
