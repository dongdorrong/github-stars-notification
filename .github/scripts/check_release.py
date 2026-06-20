#!/usr/bin/env python3
"""
GitHub starred release detector.

역할 경계
---------
- GitHub/MCP/gh CLI: starred repo와 release 원천 데이터를 가져오는 수집 계층
- 이 Python 스크립트: 캐시 비교, 중복 방지, 알림 여부 결정, GitHub Actions output 생성
- 로컬 LLM: `.cache/release-feed.json`을 읽어 요약/분류/우선순위 초안을 만드는 선택 계층

필수 환경 변수(실제 GitHub 조회 시)
-----------------------------------
GH_TOKEN        : GitHub Personal Access Token
GITHUB_OUTPUT   : GitHub Actions output file path. 로컬 테스트는 --github-output로 대체 가능
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

CACHE_PATH = Path(".cache/releases.json")
LAST_NOTIFICATION_PATH = Path(".cache/last_notification.txt")
REPOS_FILE = Path("repos.txt")
CONFIG_PATH = Path("config.yaml")
FEED_PATH = Path(".cache/release-feed.json")
MAX_TEXT_LENGTH = 35_000

DEFAULT_CONFIG: dict[str, Any] = {
    "special_projects": [],
    "notification": {
        "min_release_count": 5,
        "special_project_always_notify": True,
        "first_run_notify": True,
        "max_slack_text_length": MAX_TEXT_LENGTH,
    },
    "feed": {
        "output_path": str(FEED_PATH),
    },
    "llm": {
        "enabled": False,
        "provider": "local",
        "role": "summarize_and_prioritize_only",
    },
}


@dataclass(frozen=True)
class Release:
    """Normalized release event used by cache, Slack, JSON feed, and local LLM input."""

    repo: str
    tag: str
    name: str
    published: str
    html_url: str
    is_special: bool = False

    def cache_entry(self) -> dict[str, str]:
        return {
            "tag": self.tag,
            "published": self.published,
            "name": self.name,
            "html_url": self.html_url,
        }


@dataclass(frozen=True)
class DetectionResult:
    first_run: bool
    releases: list[Release]
    current_cache: dict[str, dict[str, str]]
    scanned_repos: int
    repos_with_release: int


@dataclass(frozen=True)
class NotificationDecision:
    should_notify: bool
    reason: str


ReleaseFetcher = Callable[[str], dict[str, Any] | None]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def normalize_repo_name(repo: str) -> str:
    """Normalize `owner / repo`, `owner/repo`, and surrounding whitespace to `owner/repo`."""
    return re.sub(r"\s*/\s*", "/", repo.strip())


def parse_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    if value is None:
        return default
    return bool(value)


def parse_int(value: Any, default: int, minimum: int | None = None) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    if minimum is not None:
        return max(minimum, parsed)
    return parsed


def parse_scalar(value: str) -> Any:
    """Tiny YAML subset parser fallback for local tests without PyYAML."""
    value = value.strip()
    if not value:
        return ""
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        return value[1:-1]
    lower = value.lower()
    if lower in {"true", "false"}:
        return lower == "true"
    if lower in {"null", "none"}:
        return None
    try:
        return int(value)
    except ValueError:
        return value


def parse_limited_yaml(raw: str) -> dict[str, Any]:
    """Parse the simple YAML shape used by config.yaml when PyYAML is unavailable.

    Supports top-level lists and one-level nested mappings. It intentionally does not
    try to be a full YAML parser; GitHub Actions still installs PyYAML for production.
    """
    data: dict[str, Any] = {}
    current_key: str | None = None

    for original_line in raw.splitlines():
        line = original_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue

        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()

        if indent == 0:
            current_key = None
            if stripped.endswith(":"):
                key = stripped[:-1].strip()
                data[key] = [] if key == "special_projects" else {}
                current_key = key
            elif ":" in stripped:
                key, value = stripped.split(":", 1)
                data[key.strip()] = parse_scalar(value)
            continue

        if current_key is None:
            continue

        if stripped.startswith("- "):
            if not isinstance(data.get(current_key), list):
                data[current_key] = []
            data[current_key].append(parse_scalar(stripped[2:]))
        elif ":" in stripped:
            if not isinstance(data.get(current_key), dict):
                data[current_key] = {}
            key, value = stripped.split(":", 1)
            data[current_key][key.strip()] = parse_scalar(value)

    return data


def load_yaml(raw: str) -> dict[str, Any]:
    try:
        import yaml  # type: ignore
    except ModuleNotFoundError:
        return parse_limited_yaml(raw)

    loaded = yaml.safe_load(raw)
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ValueError("config.yaml must be a mapping")
    return loaded


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def normalize_config(raw_config: dict[str, Any]) -> dict[str, Any]:
    config = deep_merge(DEFAULT_CONFIG, raw_config)
    config["special_projects"] = [
        normalize_repo_name(str(repo)) for repo in config.get("special_projects", []) if str(repo).strip()
    ]

    notification = config.setdefault("notification", {})
    notification["min_release_count"] = parse_int(
        notification.get("min_release_count"),
        DEFAULT_CONFIG["notification"]["min_release_count"],
        minimum=1,
    )
    notification["special_project_always_notify"] = parse_bool(
        notification.get("special_project_always_notify"),
        DEFAULT_CONFIG["notification"]["special_project_always_notify"],
    )
    notification["first_run_notify"] = parse_bool(
        notification.get("first_run_notify"),
        DEFAULT_CONFIG["notification"]["first_run_notify"],
    )
    notification["max_slack_text_length"] = parse_int(
        notification.get("max_slack_text_length"),
        DEFAULT_CONFIG["notification"]["max_slack_text_length"],
        minimum=1_000,
    )

    feed = config.setdefault("feed", {})
    feed["output_path"] = str(feed.get("output_path") or FEED_PATH)

    llm = config.setdefault("llm", {})
    llm["enabled"] = parse_bool(llm.get("enabled"), DEFAULT_CONFIG["llm"]["enabled"])
    llm["provider"] = str(llm.get("provider") or DEFAULT_CONFIG["llm"]["provider"])
    llm["role"] = str(llm.get("role") or DEFAULT_CONFIG["llm"]["role"])

    return config


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    if not path.exists():
        return normalize_config({})
    raw = path.read_text(encoding="utf-8")
    if not raw.strip():
        return normalize_config({})
    return normalize_config(load_yaml(raw))


def load_json_file(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json_file(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_cache(path: Path = CACHE_PATH) -> dict[str, dict[str, str]]:
    data = load_json_file(path, {})
    if not isinstance(data, dict):
        return {}
    return data


def save_cache(data: dict[str, dict[str, str]], path: Path = CACHE_PATH) -> None:
    write_json_file(path, data)


def read_repos(path: Path = REPOS_FILE) -> list[str]:
    repos: list[str] = []
    seen: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        repo = normalize_repo_name(line)
        if not repo or repo in seen:
            continue
        repos.append(repo)
        seen.add(repo)
    return repos


def raw_release_to_release(repo: str, raw: dict[str, Any], special_projects: set[str]) -> Release:
    tag = str(raw.get("tag_name") or raw.get("tag") or "").strip()
    published = str(raw.get("published_at") or raw.get("published") or "").strip()
    if not tag or not published:
        raise ValueError(f"release for {repo} must include tag_name/tag and published_at/published")
    return Release(
        repo=normalize_repo_name(repo),
        tag=tag,
        name=str(raw.get("name") or raw.get("title") or "").strip(),
        published=published,
        html_url=str(raw.get("html_url") or raw.get("url") or "").strip(),
        is_special=normalize_repo_name(repo) in special_projects,
    )


def get_github_release_fetcher(token: str) -> ReleaseFetcher:
    try:
        from github import Github  # type: ignore
        from github.GithubException import GithubException  # type: ignore
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "PyGithub is required for live GitHub API calls. "
            "Install .github/scripts/requirements.txt or use --fixture-releases for local tests."
        ) from exc

    gh = Github(token)

    def fetch(repo: str) -> dict[str, Any] | None:
        try:
            repository = gh.get_repo(repo)
            latest_release = repository.get_latest_release()
            return {
                "tag_name": latest_release.tag_name,
                "name": latest_release.title,
                "published_at": latest_release.published_at.strftime("%Y-%m-%d %H:%M:%S"),
                "html_url": latest_release.html_url,
            }
        except GithubException as exc:  # pragma: no cover - exercised in Actions/live use
            if exc.status == 404:
                return None
            raise

    return fetch


def load_fixture_fetcher(path: Path) -> ReleaseFetcher:
    fixture = load_json_file(path, {})
    if isinstance(fixture, list):
        fixture = {normalize_repo_name(str(item["repo"])): item for item in fixture}
    if not isinstance(fixture, dict):
        raise ValueError("fixture releases must be a mapping or a list of objects with repo")

    normalized = {normalize_repo_name(str(repo)): value for repo, value in fixture.items()}

    def fetch(repo: str) -> dict[str, Any] | None:
        value = normalized.get(normalize_repo_name(repo))
        if value in (None, False):
            return None
        if not isinstance(value, dict):
            raise ValueError(f"fixture release for {repo} must be an object or null")
        return value

    return fetch


def is_new_release(repo: str, release: Release, previous_cache: dict[str, dict[str, str]], first_run: bool) -> bool:
    if first_run:
        return True
    previous = previous_cache.get(normalize_repo_name(repo))
    if not previous:
        return True
    return previous.get("tag") != release.tag or previous.get("published") != release.published


def detect_releases(
    repos: Iterable[str],
    fetch_release: ReleaseFetcher,
    previous_cache: dict[str, dict[str, str]],
    special_projects: set[str],
    first_run: bool,
    sleep_seconds: float,
) -> DetectionResult:
    current_cache: dict[str, dict[str, str]] = {}
    new_releases: list[Release] = []
    scanned = 0
    repos_with_release = 0

    for repo in repos:
        scanned += 1
        raw = fetch_release(repo)
        if not raw:
            continue

        release = raw_release_to_release(repo, raw, special_projects)
        current_cache[release.repo] = release.cache_entry()
        repos_with_release += 1

        if is_new_release(release.repo, release, previous_cache, first_run):
            new_releases.append(release)

        if sleep_seconds > 0:
            time.sleep(sleep_seconds)

    new_releases.sort(key=lambda item: item.published, reverse=True)
    return DetectionResult(
        first_run=first_run,
        releases=new_releases,
        current_cache=current_cache,
        scanned_repos=scanned,
        repos_with_release=repos_with_release,
    )


def decide_notification(releases: list[Release], first_run: bool, config: dict[str, Any]) -> NotificationDecision:
    policy = config["notification"]
    if not releases:
        return NotificationDecision(False, "no_new_releases")

    if first_run and not policy["first_run_notify"]:
        return NotificationDecision(False, "first_run_bootstrap_only")

    if len(releases) >= policy["min_release_count"]:
        return NotificationDecision(True, "threshold_reached")

    if policy["special_project_always_notify"] and any(release.is_special for release in releases):
        return NotificationDecision(True, "special_project_release")

    return NotificationDecision(False, "below_threshold")


def save_last_notification_time(releases: list[Release], path: Path = LAST_NOTIFICATION_PATH) -> None:
    if not releases:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(max(release.published for release in releases), encoding="utf-8")


def format_date(date_str: str) -> str:
    date_part = date_str.split(" ")[0]
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_part):
        return date_part.replace("-", ".")[2:]
    return date_part


def compact_release_name(name: str, tag: str) -> str:
    release_name = " ".join(name.split())
    if not release_name or release_name == tag:
        return ""
    for prefix in ("Release ", "release ", "version ", "Version "):
        if release_name.lower().startswith(prefix.lower()):
            release_name = release_name[len(prefix) :].strip()
            break
    if release_name == tag:
        return ""
    return release_name


def format_release_line(release: Release) -> str:
    if "/" in release.repo:
        org, repo_name = release.repo.split("/", 1)
        title = f"*{org}* / *{repo_name}*"
    else:
        title = f"*{release.repo}*"
    if release.is_special:
        title = f"⭐ {title}"

    tag_link = f"<{release.html_url}|`{release.tag}`>" if release.html_url else f"`{release.tag}`"
    description_parts = [tag_link]

    release_name = compact_release_name(release.name, release.tag)
    if release_name:
        description_parts.append(f"_{release_name}_")

    description_parts.append(format_date(release.published))
    return f"{title} {' - '.join(description_parts)}"


def build_slack_payloads(
    releases: list[Release],
    first_run: bool,
    config: dict[str, Any],
    decision: NotificationDecision,
) -> list[dict[str, str]]:
    if not decision.should_notify:
        return []

    if first_run:
        header_text = "🌟 *스타 저장소의 현재 릴리스 목록입니다*"
    elif decision.reason == "special_project_release":
        header_text = f"⭐ *관심 프로젝트 릴리스 {len(releases)}개를 확인했습니다*"
    else:
        header_text = f"🚀 *새로운 릴리스 {len(releases)}개를 확인했습니다*"

    guide_text = (
        "💡 *중요한 프로젝트가 있다면 관심 프로젝트로 등록해보세요!*\n"
        "• `config.yaml`의 `special_projects`에 등록하면 ⭐ 로 강조됩니다\n"
        "• `notification` 정책으로 임계값과 첫 실행 동작을 조정할 수 있습니다"
    )

    max_text_length = config["notification"]["max_slack_text_length"]
    release_lines = [format_release_line(release) for release in releases]
    messages: list[dict[str, str]] = []
    current_text = f"{header_text}\n\n{guide_text}\n\n---\n\n"
    current_count = 0

    for release_line in release_lines:
        candidate = current_text + release_line + "\n"
        if len(candidate) > max_text_length and current_count > 0:
            messages.append({"text": current_text.rstrip()})
            current_text = f"🚀 *새로운 릴리스 (계속) - {len(messages) + 1}*\n\n{release_line}\n"
            current_count = 1
        else:
            current_text = candidate
            current_count += 1

    if current_count > 0:
        messages.append({"text": current_text.rstrip()})

    if len(messages) > 1:
        for index, message in enumerate(messages, start=1):
            if index == 1:
                message["text"] = message["text"].replace(
                    header_text,
                    f"🚀 *새로운 릴리스 ({len(releases)}개) - {index}/{len(messages)}*",
                    1,
                )
            else:
                message["text"] = re.sub(
                    r"^🚀 \*새로운 릴리스 \(계속\) - \d+\*",
                    f"🚀 *새로운 릴리스 ({len(releases)}개) - {index}/{len(messages)}*",
                    message["text"],
                    count=1,
                )

    return messages


def build_release_feed(
    result: DetectionResult,
    decision: NotificationDecision,
    payloads: list[dict[str, str]],
    config: dict[str, Any],
    repos_file: Path,
    cache_path: Path,
) -> dict[str, Any]:
    releases = [asdict(release) for release in result.releases]
    special_release_count = sum(1 for release in result.releases if release.is_special)
    return {
        "schema_version": "github-stars-release-feed/v1",
        "generated_at": utc_now(),
        "source": {
            "collector": "github-cli-or-github-mcp",
            "detector": ".github/scripts/check_release.py",
            "repos_file": str(repos_file),
            "cache_path": str(cache_path),
        },
        "first_run": result.first_run,
        "scanned_repos": result.scanned_repos,
        "repos_with_release": result.repos_with_release,
        "release_count": len(result.releases),
        "special_release_count": special_release_count,
        "notify": decision.should_notify,
        "notify_reason": decision.reason,
        "policy": config["notification"],
        "releases": releases,
        "slack_payload_count": len(payloads),
        "llm_contract": {
            "enabled": config["llm"]["enabled"],
            "provider": config["llm"]["provider"],
            "allowed_roles": [
                "summarize_release_notes",
                "categorize_projects",
                "score_human_attention_priority",
                "draft_human_readable_digest",
            ],
            "must_not_do": [
                "decide_new_vs_duplicate",
                "mutate_cache_or_state",
                "send_notifications",
                "override_notification_policy",
            ],
            "input_guidance": "Use releases[] as the deterministic source. Treat summaries as advisory text only.",
        },
        "mcp_contract": {
            "role": "optional_read_only_collection_surface",
            "recommended_toolsets": ["stargazers", "repos", "actions"],
            "boundary": "GitHub MCP may collect GitHub context; this script remains the state and notification source of truth.",
        },
    }


def write_github_outputs(
    output_path: Path,
    decision: NotificationDecision,
    payloads: list[dict[str, str]],
    feed_path: Path,
    release_count: int,
    special_release_count: int,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("a", encoding="utf-8") as output:
        output.write(f"has_new={str(decision.should_notify).lower()}\n")
        output.write(f"message_count={len(payloads)}\n")
        output.write(f"payloads={json.dumps(payloads, ensure_ascii=False)}\n")
        output.write(f"feed_path={feed_path}\n")
        output.write(f"release_count={release_count}\n")
        output.write(f"special_release_count={special_release_count}\n")
        output.write(f"notify_reason={decision.reason}\n")
        if payloads:
            safe = json.dumps(payloads[0], ensure_ascii=False).replace("%", "%25").replace("\n", "%0A").replace("\r", "%0D")
            output.write(f"payload={safe}\n")


def print_summary(result: DetectionResult, decision: NotificationDecision, feed_path: Path) -> None:
    print(f"DEBUG: Scanned repos: {result.scanned_repos}")
    print(f"DEBUG: Repos with release: {result.repos_with_release}")
    print(f"DEBUG: New releases: {len(result.releases)}")
    print(f"DEBUG: First run: {result.first_run}")
    print(f"DEBUG: Notify: {decision.should_notify} ({decision.reason})")
    print(f"DEBUG: Feed path: {feed_path}")
    for release in result.releases[:5]:
        marker = " [special]" if release.is_special else ""
        print(f"  - {release.repo}: {release.tag} ({release.published}){marker}")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Detect new releases from GitHub starred repositories.")
    parser.add_argument("--repos-file", type=Path, default=REPOS_FILE)
    parser.add_argument("--cache-path", type=Path, default=CACHE_PATH)
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--feed-path", type=Path, default=None)
    parser.add_argument("--github-output", type=Path, default=None)
    parser.add_argument("--fixture-releases", type=Path, default=None, help="JSON fixture for token-free local tests")
    parser.add_argument("--sleep-seconds", type=float, default=0.3)
    parser.add_argument("--no-sleep", action="store_true")
    return parser


def run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    feed_path = args.feed_path or Path(config["feed"]["output_path"])
    github_output_env = os.environ.get("GITHUB_OUTPUT")
    output_path = args.github_output or (Path(github_output_env) if github_output_env else None)

    repos = read_repos(args.repos_file)
    previous_cache = load_cache(args.cache_path)
    first_run = not args.cache_path.exists()
    special_projects = set(config["special_projects"])

    if args.fixture_releases:
        fetch_release = load_fixture_fetcher(args.fixture_releases)
    else:
        token = os.getenv("GH_TOKEN")
        if not token:
            print("GH_TOKEN env required for live GitHub API calls", file=sys.stderr)
            return 1
        fetch_release = get_github_release_fetcher(token)

    result = detect_releases(
        repos=repos,
        fetch_release=fetch_release,
        previous_cache=previous_cache,
        special_projects=special_projects,
        first_run=first_run,
        sleep_seconds=0 if args.no_sleep else args.sleep_seconds,
    )
    decision = decide_notification(result.releases, result.first_run, config)
    payloads = build_slack_payloads(result.releases, result.first_run, config, decision)
    feed = build_release_feed(result, decision, payloads, config, args.repos_file, args.cache_path)

    save_cache(result.current_cache, args.cache_path)
    write_json_file(feed_path, feed)
    if decision.should_notify:
        save_last_notification_time(result.releases, args.cache_path.parent / LAST_NOTIFICATION_PATH.name)

    if output_path is not None:
        write_github_outputs(
            output_path=output_path,
            decision=decision,
            payloads=payloads,
            feed_path=feed_path,
            release_count=len(result.releases),
            special_release_count=feed["special_release_count"],
        )

    print_summary(result, decision, feed_path)
    return 0


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()
    try:
        raise SystemExit(run(args))
    except Exception as exc:  # pragma: no cover - last-resort CLI guard
        print("ERROR:", exc, file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
