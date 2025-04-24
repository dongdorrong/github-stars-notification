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

def format_date(date_str: str) -> str:
    """날짜를 더 읽기 쉬운 형식으로 변환"""
    return date_str.replace('-', '.')[2:]  # '2025-04-16' -> '25.04.16'

def format_release_info(repo: str, release_data: dict, tag: str, published: str) -> str:
    """릴리스 정보를 슬랙 스타일로 포맷팅"""
    parts = []
    
    # 1. 저장소 이름
    org, repo_name = repo.split('/')
    header = f"*{org}* / *{repo_name}*"
    parts.append(header)
    
    # 2. 태그 정보와 릴리스 링크
    tag_info = f"<{release_data['html_url']}|`{tag}`>"
    if release_name := release_data.get("name", "").strip():
        # 태그와 다른 경우에만 릴리스 제목 추가
        if release_name != tag:
            # 일반적인 접두사 제거
            prefixes = ["Release ", "release ", "version ", "v", "Version "]
            for prefix in prefixes:
                if release_name.lower().startswith(prefix.lower()):
                    release_name = release_name[len(prefix):]
            tag_info = f"<{release_data['html_url']}|`{tag}`> - _{release_name.strip()}_"
    parts.append(tag_info)
    
    # 3. 날짜 정보
    date_info = format_date(published)
    parts.append(date_info)
    
    return " ".join(parts)

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
            blocks = []
            text_contents = []
            
            # 헤더 추가
            header_text = "🚀 *새로운 릴리스를 확인했습니다*"
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": header_text
                }
            })
            blocks.append({"type": "divider"})
            text_contents.append(header_text)
            
            for nr in new_releases:
                # 릴리스 정보 가져오기
                release_data = gh_get(f"https://api.github.com/repos/{nr['repo']}/releases/tags/{nr['tag']}")
                if not release_data:
                    continue

                # 메시지 블록 구성
                message = format_release_info(nr['repo'], release_data, nr['tag'], nr['published'])
                blocks.append({
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": message
                    }
                })
                
                # 폴백 텍스트용
                text_contents.append(message)
            
            payload = {
                "blocks": blocks,
                "text": "\n".join(text_contents)
            }
            
            f.write(f"has_new=true\n")
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
