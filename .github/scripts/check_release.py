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
        time.sleep(0.3)

    # 캐시 저장(항상)
    save_cache(current)

    # GitHub Actions 출력 -----------------------------------------------
    outputs_file = Path(os.environ["GITHUB_OUTPUT"])
    with outputs_file.open("a") as f:
        if new_releases:
            # Slack Block Kit 형식으로 메시지 생성
            blocks = []
            text_contents = []
            
            for nr in new_releases:
                # 헤더 섹션 (저장소 이름과 태그)
                header = f"*{nr['repo']}* tagged `{nr['tag']}`"
                blocks.append({
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": header
                    }
                })
                
                # 릴리스 제목과 설명
                release_data = gh_get(f"https://api.github.com/repos/{nr['repo']}/releases/tags/{nr['tag']}")
                if release_data:
                    body = release_data.get("body", "").strip()
                    if body:
                        # 설명이 너무 길면 잘라내기
                        if len(body) > 2000:
                            body = body[:2000] + "..."
                        blocks.append({
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": body
                            }
                        })
                
                # 릴리스 링크와 날짜
                footer = f"<{nr['html_url']}|전체 릴리스 노트 보기> • {nr['published']}"
                blocks.append({
                    "type": "context",
                    "elements": [{
                        "type": "mrkdwn",
                        "text": footer
                    }]
                })
                
                # 구분선 추가
                blocks.append({"type": "divider"})
                
                # 폴백 텍스트용
                text_contents.extend([
                    header,
                    body if release_data and release_data.get("body") else "",
                    footer,
                    "---"
                ])
            
            payload = {
                "blocks": blocks,
                "text": "\n\n".join(text for text in text_contents if text)  # 빈 문자열 제외하고 결합
            }
            
            f.write(f"has_new=true\n")
            # JSON 문자열로 변환하고 이스케이프
            safe = json.dumps(payload).replace("%", "%25").replace("\n", "%0A").replace("\r", "%0D")
            f.write(f"payload={safe}\n")
        else:
            f.write("has_new=false\n")

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # 실패하면 워크플로우를 실패시키기 위해 예외 출력 후 종료 코드 1
        print("ERROR:", exc, file=sys.stderr)
        sys.exit(1)
