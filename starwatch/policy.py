"""Deterministic selection of accumulated notification candidates."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Decision:
    should_notify: bool
    reason: str


def select(pending: list[dict], config: dict) -> Decision:
    if not pending:
        return Decision(False, "no_pending_releases")
    # An earlier attempted digest must be retried even if only part of it remains.
    if any(item["notification_state"] == "DELIVERY_FAILED" for item in pending):
        return Decision(True, "retry_failed_delivery")
    policy = config["notification"]
    if len(pending) >= policy["min_release_count"]:
        return Decision(True, "threshold_reached")
    if policy["special_project_always_notify"] and any(item["is_special"] for item in pending):
        return Decision(True, "special_project_release")
    return Decision(False, "below_threshold")
