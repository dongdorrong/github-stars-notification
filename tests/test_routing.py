"""Token-free P1 routing and Slack-content fixtures."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from starwatch.routing import build_routed_chunks, decide, select


NOW = datetime(2026, 10, 9, 0, 0, tzinfo=timezone.utc)  # 09:00 KST
CONFIG = {
    "routing": {
        "policy_version": "v1",
        "destination_visibility": "private",
        "digest": {
            "min_count": 5,
            "max_age_hours": 24,
            "send_hours_kst": [17],
            "max_batch_size": 50,
        },
        "high": {"max_batch_size": 10},
    }
}


def event(number: int, **changes: object) -> dict:
    value = {
        "event_id": f"github:release:{number}",
        "event_type": "release",
        "repository": "owner/repo",
        "title": f"Release {number}",
        "url": f"https://github.com/owner/repo/releases/tag/v{number}",
        "visibility": "public",
        "notification_state": "PENDING_NOTIFICATION",
        "first_queued_at": NOW.isoformat(),
        "routing": {"route": "DIGEST"},
    }
    value.update(changes)
    return value


class RoutingDecisionTests(unittest.TestCase):
    def test_critical_high_medium_project_and_explicit_floors(self) -> None:
        for severity, tier, expected in (
            ("critical", "standard", "CRITICAL"),
            ("high", "standard", "HIGH"),
            ("medium", "critical", "HIGH"),
            ("medium", "standard", "DIGEST"),
        ):
            with self.subTest(severity=severity, tier=tier):
                item = event(1, event_type="ghsa", severity=severity)
                self.assertEqual(
                    decide(item, {"tier": tier}, CONFIG)["route"], expected
                )
        self.assertEqual(
            decide(event(2), {"routing": {"release_floor": "high"}}, CONFIG)["route"],
            "HIGH",
        )
        self.assertEqual(decide(event(3, is_special=True), {}, CONFIG)["route"], "HIGH")
        self.assertEqual(
            decide(
                event(4, event_type="github_security_advisory", severity="critical"),
                {"signals": {"advisory": True}},
                CONFIG,
            )["route"],
            "CRITICAL",
        )

    def test_ai_cannot_demote_critical_or_raise_source_trust(self) -> None:
        critical = event(1, event_type="ghsa", severity="critical")
        audit = decide(critical, {}, CONFIG, {"impact": "low"})
        self.assertEqual(
            (audit["deterministic_floor"], audit["effective_priority"]),
            ("CRITICAL", "CRITICAL"),
        )
        untrusted = event(
            2, event_type="issue", source_trust=30, title="RCE authentication bypass"
        )
        audit = decide(
            untrusted,
            {"signals": {"announcement": True, "issues": True}},
            CONFIG,
            {"impact": "critical"},
        )
        self.assertEqual(audit["route"], "SUPPRESSED")
        self.assertEqual(audit["suppression_reason"], "source_trust_below_threshold")
        self.assertEqual(audit["deterministic_floor"], "DIGEST")

    def test_security_breaking_and_suppression_reasons(self) -> None:
        self.assertEqual(
            decide(event(1, title="Fix remote code execution"), {}, CONFIG)["route"],
            "CRITICAL",
        )
        self.assertEqual(
            decide(event(2, title="required migration"), {}, CONFIG)["route"], "HIGH"
        )
        self.assertEqual(
            decide(event(3, title="microservice"), {}, CONFIG)["route"], "DIGEST"
        )
        self.assertEqual(
            decide(event(7, title="resource limits"), {}, CONFIG)["route"], "DIGEST"
        )
        cases = [
            (event(1, draft=True), {}, "draft"),
            (event(2), {"tier": "ignore"}, "project_ignored"),
            (
                event(3, prerelease=True),
                {"signals": {"prerelease": False}},
                "prerelease_disabled",
            ),
            (
                event(4, event_type="ghsa", project_mapping={"status": "unmapped"}),
                {},
                "advisory_unmapped",
            ),
            (
                event(5, event_type="ghsa", withdrawn_at="2026-10-09T00:00:00Z"),
                {},
                "advisory_withdrawn",
            ),
            (event(6, event_type="discussion"), {}, "signal_disabled"),
            (
                event(8, event_type="github_discussion", source_trust=90),
                {"signals": {"announcement": True}},
                "signal_disabled",
            ),
            (
                event(9, event_type="github_issue", source_trust=85),
                {"signals": {"announcement": True}},
                "signal_disabled",
            ),
            (
                event(10, event_type="rss_atom", source_trust=95),
                {"signals": {"announcement": True}},
                "signal_disabled",
            ),
        ]
        for item, project, reason in cases:
            with self.subTest(reason=reason):
                audit = decide(item, project, CONFIG)
                self.assertEqual(audit["route"], "SUPPRESSED")
                self.assertEqual(audit["suppression_reason"], reason)
                self.assertIn(reason, audit["deterministic_reasons"])
        self.assertEqual(
            decide(
                event(11, event_type="github_discussion", source_trust=90),
                {"signals": {"announcement": True, "discussions": True}},
                CONFIG,
            )["route"],
            "DIGEST",
        )
        self.assertEqual(
            decide(
                event(12, event_type="github_discussion", source_trust=90),
                {
                    "signals": {"announcement": True, "discussions": True},
                    "routing": {"release_floor": "high"},
                },
                CONFIG,
            )["route"],
            "DIGEST",
        )

    def test_public_destination_excludes_nonpublic_and_unknown(self) -> None:
        config = {"routing": {"destination_visibility": "public"}}
        for visibility in ("private", "internal", "unknown", None):
            self.assertEqual(
                decide(event(1, visibility=visibility), {}, config)[
                    "suppression_reason"
                ],
                "visibility_not_public",
            )
        self.assertEqual(decide(event(2), {}, config)["route"], "DIGEST")


class DigestSelectionTests(unittest.TestCase):
    def test_one_digest_waits_five_send(self) -> None:
        self.assertEqual(select([event(1)], CONFIG, NOW), [])
        chosen = select([event(i) for i in range(1, 6)], CONFIG, NOW)
        self.assertEqual(
            [e["event_id"] for e in chosen],
            [f"github:release:{i}" for i in range(1, 6)],
        )

    def test_age_kst_hour_and_retry_send_below_count(self) -> None:
        old = event(1, first_queued_at=(NOW - timedelta(hours=25)).isoformat())
        self.assertEqual(select([old], CONFIG, NOW), [old])
        at_kst_17 = NOW.replace(hour=8)
        fresh = event(2)
        self.assertEqual(select([fresh], CONFIG, at_kst_17), [fresh])
        failed = event(3, notification_state="DELIVERY_FAILED")
        self.assertEqual(select([failed], CONFIG, NOW), [failed])

    def test_immediate_routes_and_suppressed_exclusion(self) -> None:
        items = [
            event(1),
            event(2, routing={"route": "CRITICAL"}),
            event(3, routing={"route": "HIGH"}),
            event(4, routing={"route": "SUPPRESSED"}),
            event(5, routing={"route": "HIGH"}, notification_state="DELIVERED"),
        ]
        self.assertEqual(
            [e["event_id"] for e in select(items, CONFIG, NOW)],
            ["github:release:2", "github:release:3"],
        )
        with self.assertRaises(ValueError):
            select(items, CONFIG, NOW.replace(tzinfo=None))


class RoutedSlackTests(unittest.TestCase):
    def test_chunk_boundaries_preserve_order_and_exact_ids(self) -> None:
        items = [
            event(i, title="x" * 100, routing={"route": "DIGEST"}) for i in range(1, 8)
        ]
        chunks = build_routed_chunks(items, 150, CONFIG)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk.payload["text"]) <= 150 for chunk in chunks))
        self.assertEqual(
            [identifier for chunk in chunks for identifier in chunk.event_ids],
            [item["event_id"] for item in items],
        )

    def test_high_batch_cap_and_critical_one_per_chunk(self) -> None:
        items = [event(i, routing={"route": "HIGH"}) for i in range(1, 13)]
        chunks = build_routed_chunks(items, 3000, CONFIG)
        self.assertEqual([len(chunk.event_ids) for chunk in chunks], [10, 2])
        critical = [event(i, routing={"route": "CRITICAL"}) for i in range(20, 22)]
        self.assertEqual(len(build_routed_chunks(critical, 1000, CONFIG)), 2)

    def test_untrusted_slack_metacharacters_cannot_mention(self) -> None:
        item = event(
            1,
            repository="<@U1>",
            title="<!channel> <!here> <http://evil|bad>",
            summary_ko="Call Slack now <@U2>",
            recommended_actions=["<@U3>"],
            url="https://github.com/x/y|<!here>",
        )
        chunk = build_routed_chunks([item], 1000, CONFIG)[0]
        text = chunk.payload["text"]
        for raw in (
            "<@U1>",
            "<@U2>",
            "<@U3>",
            "<!channel>",
            "<!here>",
            "<http://evil|bad>",
        ):
            self.assertNotIn(raw, text)
        self.assertIn("&lt;@U1&gt;", text)
        self.assertNotIn("<https://github.com/x/y|", text)

    def test_duplicate_identity_or_invalid_route_fails_closed(self) -> None:
        with self.assertRaises(ValueError):
            build_routed_chunks([event(1), event(1)], 1000, CONFIG)
        with self.assertRaises(ValueError):
            build_routed_chunks(
                [event(1, routing={"route": "SUPPRESSED"})], 1000, CONFIG
            )


if __name__ == "__main__":
    unittest.main()
