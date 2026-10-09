from __future__ import annotations

import json
import tempfile
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
    def __init__(self, releases: list[dict[str, object]]):
        self.releases = releases

    def get_releases(self):
        # PyGithub returns a lazy paginated iterable.
        for item in self.releases:
            yield type("GithubRelease", (), {"raw_data": item})()


class FakeGithub:
    def __init__(self, releases_by_repo: dict[str, list[dict[str, object]]]):
        self.releases_by_repo = releases_by_repo

    def get_repo(self, repo: str):
        return FakeRepository(self.releases_by_repo[repo])


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
        result = collect_releases(["owner/repo"], source, seen_event_ids={"github:release:1"})
        self.assertEqual({event.release_id for event in result.events}, {2, 3, 4})

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

    def test_late_page_failure_discards_partial_repository_results(self) -> None:
        class BrokenSource:
            def get_releases(self, repository: str):
                if repository == "owner/broken":
                    yield release(1)
                    raise OSError("page two unavailable")
                yield release(2)

        result = collect_releases(["owner/broken", "owner/good"], BrokenSource())
        self.assertEqual([event.release_id for event in result.events], [2])
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
            def get_releases(self, repository: str):
                raise FixtureLikeHttpError(secret)

        class FixtureLikeHttpError(Exception):
            status = 403

        result = collect_releases(["owner/repo"], LeakySource())
        self.assertEqual(result.errors[0].status, 403)
        self.assertEqual(result.errors[0].message, "http_403")
        self.assertNotIn(secret, repr(result.errors))


if __name__ == "__main__":
    unittest.main()
