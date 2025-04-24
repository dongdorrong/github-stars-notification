#!/usr/bin/env python3
"""
check_release.py
Star 목록( repos.txt )을 읽어 최신 릴리즈를 확인하고,
새 릴리즈가 있으면 GitHub Actions 출력 변수에 결과를 기록한다.

필수 환경 변수
-------------
GH_TOKEN        : GitHub Personal Access Token (repo:read)
GITHUB_OUTPUT   : GitHub Actions가 제공하는 출력용 파일 경로
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# 0. 설정 -------------------------------------------------------------------
CACHE_PATH = Path(".cache/releases.json")   # 이전 릴리즈 캐시
REPOS_FILE = Path("repos.txt")              # workflow 앞 단계에서 생성
GH_API     = "https://api.github.com/repos/{repo}/releases/latest"
HEADERS    = {"User-Agent": "check-release/1.0"}

token = os.getenv("GH_TOKEN")
if not token:
    print("GH_TOKEN env required", file=sys.stderr)
    sys.exit(1)
HEADERS["Authorization"] = f"Bearer {token}"

# 1. 유틸 함수 --------------------------------------------------------------
def gh_get(url: str) -> dict | None:
    """단일 REST GET, 404(릴리즈 없음) → None 반환"""
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise

def load_cache() -> dict:
    if CACHE_PATH.exists():
        return json.loads(CACHE_PATH.read_text())
    return {}

def save_cache(data: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2))

# 2. 메인 로직 --------------------------------------------------------------
def main() -> None:
    prev = load_cache()         # {repo: tag_name}
    current: dict[str, str] = {}
    new_releases: list[dict] = []

    for repo in REPOS_FILE.read_text().splitlines():
        repo = repo.strip()
        if not repo:
            continue
        url = GH_API.format(repo=repo)
        data = gh_get(url)
        if not data:            # 릴리즈가 없는 저장소
            continue

        tag = data["tag_name"]
        current[repo] = tag

        if prev.get(repo) != tag:
            new_releases.append(
                {
                    "repo": repo,
                    "tag": tag,
                    "name": data.get("name") or "",
                    "published": data["published_at"][:10],
                    "html_url": data["html_url"],
                }
            )

        # API rate-limit 대비 딜레이 (60req/min 익명, 5k/hr 인증)
        time.sleep(
