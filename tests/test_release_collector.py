from __future__ import annotations

import json
import signal
import tempfile
import time
import unittest
from pathlib import Path

from starwatch.release_collector import FixtureReleaseSource, LiveReleaseSource, collect_releases


def release(release_id: int, *, tag: str | None = None, **extra: object) -> dict[str, object]:
    return {
        "id": release_id,
        "tag_name": tag or f"v{release_id}",
        "name": f"Release {release_id}",
        "body": f"Notes {release_id}",
        "html_url": f"https://github.com/owner/repo/releases/tag/v{release_id}",
        "published_at": "2026-10-01T00:00:00Z",
        "created_at": "2026-09-30T00:00:00Z",
        "updated_at": "2026-10-02T00:00:00Z",
        "draft": False,
        "prerelease": False,
        **extra,
    }


class FakeRepository:
    def __init__(self, releases: list[dict[str, object]], page_requests: list[int] | None = None):
        self.releases = releases
        self.page_requests = page_requests if page_requests is not None else []

    def get_releases(self):
        # PyGithub exposes explicit get_page; an unbounded iterator is forbidden.
        class Pages:
            def __init__(self, releases, page_requests):
                self.releases = releases
                self.page_requests = page_requests

            def get_page(self, index):
                self.page_requests.append(index)
                page = self.releases[index * 100:(index + 1) * 100]
                return [PageRelease(item) for item in page]

            def __iter__(self):
                raise AssertionError("collector must not iterate all releases")

        return Pages(self.releases, self.page_requests)


class PageRelease:
    """PyGithub page rows must not trigger one detail request per Release."""

    def __init__(self, raw):
        self._rawData = raw

    @property
    def raw_data(self):
        raise AssertionError("per-release detail request forbidden")


class FakeGithub:
    def __init__(self, releases_by_repo: dict[str, list[dict[str, object]]]):
        self.releases_by_repo = releases_by_repo
        self.page_requests: list[int] = []

    def get_repo(self, repo: str, *, lazy: bool = False):
        return FakeRepository(self.releases_by_repo[repo], self.page_requests)


class ReleaseCollectorTest(unittest.TestCase):
    def fixture(self, data: dict[str, object]) -> FixtureReleaseSource:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "releases.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return FixtureReleaseSource(path)

    def test_three_unseen_releases_and_stable_identity(self) -> None:
        source = self.fixture({"owner/repo": [release(1), release(2), release(3)]})
        result = collect_releases(["owner/repo"], source)
        self.assertEqual({event.event_id for event in result.events}, {
            "github:release:1", "github:release:2", "github:release:3"
        })
        self.assertEqual(result.errors, ())
        self.assertEqual(result.events[0].source_id, result.events[0].event_id)
        self.assertEqual(result.events[0].raw_metadata["api_resource_id"], 1)
        self.assertEqual(result.events[0].body, "Notes 1")

    def test_page_two_and_backdated_release_are_not_skipped(self) -> None:
        old = release(4, published_at="2020-01-01T00:00:00Z")
        source = self.fixture({"owner/repo": {"pages": [[release(1), release(2)], [release(3), old]]}})
        result = collect_releases(["owner/repo"], source, seen_event_ids={"github:release:1"},
                                  initialized_repos={"owner/repo"},
                                  config={"per_page": 2, "max_incremental_pages_per_repo": 3,
                                          "known_only_pages_to_stop": 2})
        self.assertEqual({event.release_id for event in result.events}, {1, 2, 3, 4})

    def test_duplicate_release_id_dedupes_and_preserves_flags(self) -> None:
        source = self.fixture({"owner/repo": [release(1, draft=True, prerelease=True), release(1), release(2)]})
        result = collect_releases(["owner/repo"], source, special_projects={"owner/repo"})
        self.assertEqual(len(result.events), 2)
        event = result.events[0]
        self.assertTrue(event.draft)
        self.assertTrue(event.prerelease)
        self.assertTrue(event.is_special)
        self.assertEqual(event.content_hash[:7], "sha256:")

    def test_repository_error_isolation_and_empty_repo_distinction(self) -> None:
        source = self.fixture({
            "bad/missing": {"error": {"status": 404, "message": "inaccessible"}},
            "bad/limited": {"error": {"status": 429, "message": "rate limited"}},
            "bad/server": {"error": {"status": 500, "message": "server error"}},
            "empty/repo": [],
            "owner/repo": [release(9)],
        })
        result = collect_releases(["bad/missing", "bad/limited", "bad/server", "empty/repo", "owner/repo"], source)
        self.assertEqual([error.status for error in result.errors], [404, 429, 500])
        self.assertEqual({error.repository for error in result.errors}, {"bad/missing", "bad/limited", "bad/server"})
        self.assertEqual(result.repositories_with_release, 1)
        self.assertEqual([event.release_id for event in result.events], [9])

    def test_live_fixture_adapter_contract_parity(self) -> None:
        raw = [release(1), release(2, prerelease=True)]
        fixture_events = collect_releases(["owner/repo"], self.fixture({"owner/repo": raw})).events
        live_events = collect_releases(["owner/repo"], LiveReleaseSource(client=FakeGithub({"owner/repo": raw}))).events
        self.assertEqual([event.as_dict() for event in fixture_events], [event.as_dict() for event in live_events])

    def test_live_page_uses_only_list_page_data_not_release_detail_gets(self) -> None:
        raw = [release(number) for number in range(1, 101)]
        client = FakeGithub({"owner/repo": raw})
        source = LiveReleaseSource(client=client)
        page = source.fetch_page("owner/repo", 1, 100)
        self.assertEqual([item["id"] for item in page.releases], list(range(1, 101)))
        self.assertTrue(page.has_next)  # Full page requires one bounded terminal probe.
        result = collect_releases(["owner/repo"], source)
        self.assertEqual(len(result.events), 100)
        self.assertEqual(result.pages_fetched, 1)
        self.assertEqual(client.page_requests, [0, 0])  # explicit fetch + collection

    def test_live_release_id_is_required_but_legacy_fixture_has_deterministic_fallback(self) -> None:
        raw = release(1)
        del raw["id"]
        fixture = self.fixture({"owner/repo": raw})
        first = collect_releases(["owner/repo"], fixture).events[0]
        second = collect_releases(["owner/repo"], fixture).events[0]
        self.assertEqual(first.event_id, second.event_id)
        self.assertTrue(first.event_id.startswith("github:release:fixture:"))
        live = collect_releases(["owner/repo"], LiveReleaseSource(client=FakeGithub({"owner/repo": [raw]})))
        self.assertEqual(live.events, ())
        self.assertEqual(len(live.errors), 1)

    def test_late_page_failure_retains_complete_page_results(self) -> None:
        class BrokenSource:
            def fetch_page(self, repository: str, page_number: int, per_page: int):
                from starwatch.release_collector import ReleasePage
                if repository == "owner/broken" and page_number == 2:
                    raise OSError("page two unavailable")
                if repository == "owner/broken":
                    return ReleasePage((release(1),), page_number, True)
                return ReleasePage((release(2),), page_number, False)

        result = collect_releases(["owner/broken", "owner/good"], BrokenSource(),
                                  initialized_repos={"owner/broken", "owner/good"},
                                  config={"per_page": 1, "max_incremental_pages_per_repo": 2,
                                          "known_only_pages_to_stop": 2})
        self.assertEqual([event.release_id for event in result.events], [1, 2])
        self.assertEqual(result.errors[0].repository, "owner/broken")

    def test_content_hash_changes_when_release_body_is_edited(self) -> None:
        before = collect_releases(["owner/repo"], self.fixture({"owner/repo": release(1)})).events[0]
        after = collect_releases(["owner/repo"], self.fixture({"owner/repo": release(1, body="Edited notes")})).events[0]
        self.assertEqual(before.event_id, after.event_id)
        self.assertNotEqual(before.content_hash, after.content_hash)

    def test_rejects_non_integral_release_ids_without_aliasing(self) -> None:
        for invalid in (1.5, True, False, "1.5", "-1", " 1", 0):
            with self.subTest(invalid=invalid):
                fixture = self.fixture({"owner/repo": release(1, id=invalid)})
                result = collect_releases(["owner/repo"], fixture)
                self.assertEqual(result.events, ())
                self.assertEqual(len(result.errors), 1)
        result = collect_releases(["owner/repo"], self.fixture({"owner/repo": release(1, id="123")}))
        self.assertEqual(result.events[0].event_id, "github:release:123")

    def test_exception_details_cannot_leak_into_error_report(self) -> None:
        secret = "SENSITIVE-HEADER-BODY-SENTINEL"

        class LeakySource:
            def fetch_page(self, repository: str, page_number: int, per_page: int):
                raise FixtureLikeHttpError(secret)

        class FixtureLikeHttpError(Exception):
            status = 403

        result = collect_releases(["owner/repo"], LeakySource())
        self.assertEqual(result.errors[0].status, 403)
        self.assertEqual(result.errors[0].message, "http_403")
        self.assertNotIn(secret, repr(result.errors))

    @unittest.skipUnless(hasattr(signal, "setitimer"), "POSIX deadline required")
    def test_live_page_deadline_defers_without_partial_state(self) -> None:
        class SlowGithub:
            def get_repo(self, repository: str, *, lazy: bool = False):
                time.sleep(2)  # interrupted by the collector's one-second wall deadline
                return FakeRepository([release(1)])

        start = time.monotonic()
        result = collect_releases(["owner/slow"], LiveReleaseSource(client=SlowGithub()),
                                  config={"per_repo_budget_seconds": 1,
                                          "global_budget_seconds": 2})
        self.assertLess(time.monotonic() - start, 1.9)
        self.assertEqual(result.events, ())
        self.assertEqual(result.pages_fetched, 0)
        self.assertEqual(result.repositories_deferred, 1)
        self.assertEqual(result.errors, ())
        self.assertEqual(result.repo_updates, ())


if __name__ == "__main__":
    unittest.main()
