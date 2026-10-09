"""Slack payload is bounded, injection-safe, and keeps delivery IDs per chunk."""

from __future__ import annotations

import unittest

from starwatch.slack_payload import build_chunks, release_line


def event(release_id: int, **changes: object) -> dict:
    value = {
        "event_id": f"github:release:{release_id}",
        "repository": "owner/repo",
        "tag_name": f"v{release_id}",
        "release_name": f"Release {release_id}",
        "published_at": "2026-10-01T00:00:00Z",
        "html_url": f"https://github.com/owner/repo/releases/tag/v{release_id}",
        "is_special": False,
    }
    value.update(changes)
    return value


class SlackPayloadTest(unittest.TestCase):
    def test_untrusted_repo_tag_and_title_cannot_create_mentions_or_markup(self) -> None:
        line = release_line(event(
            1,
            repository="owner/<@U123>&repo",
            tag_name="<!channel>&<@U456>",
            release_name="<@U789> <!here> & the team",
            html_url="",
        ))
        for raw in ("<@U123>", "<@U456>", "<@U789>", "<!channel>", "<!here>"):
            self.assertNotIn(raw, line)
        self.assertIn("&lt;@U123&gt;&amp;repo", line)
        self.assertIn("&lt;!channel&gt;&amp;&lt;@U456&gt;", line)
        self.assertIn("&lt;@U789&gt; &lt;!here&gt; &amp; the team", line)

    def test_url_pipe_and_newline_cannot_break_slack_link(self) -> None:
        for unsafe in (
            "https://github.com/owner/repo/releases/tag/v1|<@U123>",
            "https://github.com/owner/repo/releases/tag/v1\n<!channel>",
        ):
            with self.subTest(url=unsafe):
                line = release_line(event(1, html_url=unsafe))
                self.assertNotIn(unsafe, line)
                self.assertNotIn("<@U123>", line)
                self.assertNotIn("<!channel>", line)
                self.assertIn("`v1`", line)
                self.assertNotIn("<https://github.com/", line)

    def test_split_payloads_stay_bounded_and_own_exact_event_ids(self) -> None:
        events = [event(i, release_name="x" * 40) for i in range(1, 7)]
        chunks = build_chunks(events, max_length=160)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk.payload["text"]) <= 160 for chunk in chunks))
        self.assertEqual([event_id for chunk in chunks for event_id in chunk.event_ids],
                         [item["event_id"] for item in events])
        self.assertTrue(all(chunk.event_ids for chunk in chunks))

    def test_oversized_single_title_is_bounded_without_losing_ack_identity(self) -> None:
        item = event(99, release_name="x" * 100_000)
        chunks = build_chunks([item], max_length=1000)
        self.assertEqual(len(chunks), 1)
        self.assertLessEqual(len(chunks[0].payload["text"]), 1000)
        self.assertEqual(chunks[0].event_ids, ["github:release:99"])


if __name__ == "__main__":
    unittest.main()
