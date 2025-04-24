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

def truncate_text(text: str, max_length: int = 500) -> str:
    """텍스트를 지정된 길이로 제한하고 필요한 경우 말줄임표를 추가합니다."""
    if not text:
        return ""
    text = text.strip()
    if len(text) <= max_length:
        return text
    return text[:max_length-3] + "..."

def format_release_body(body: str) -> str:
    """릴리스 노트 본문을 포맷팅합니다."""
    if not body:
        return ""
    
    # 줄바꿈 정리
    lines = [line.strip() for line in body.split("\n")]
    lines = [line for line in lines if line]
    
    # 처음 몇 줄만 사용
    summary = "\n".join(lines[:5])
    if len(lines) > 5:
        summary += "\n..."
    
    return truncate_text(summary, 800)

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
            blocks = []
            text_contents = []
            
            for nr in new_releases:
                # 릴리스 정보 가져오기
                release_data = gh_get(f"https://api.github.com/repos/{nr['repo']}/releases/tags/{nr['tag']}")
                if not release_data:
                    continue

                # 제목 구성 (저장소명과 릴리스 이름)
                title = f"*{nr['repo']}*"
                if release_data.get("name"):
                    title += f": {release_data['name']}"
                
                # 첫 번째 섹션 - 제목과 태그
                blocks.append({
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"{title}\n`{nr['tag']}` • {nr['published']} • <{nr['html_url']}|릴리스 보기>"
                    }
                })

                # 두 번째 섹션 - 설명 (있는 경우에만)
                description = release_data.get("body", "").strip()
                if description:
                    # 첫 줄만 사용
                    first_line = description.split('\n')[0].strip()
                    if first_line:
                        blocks.append({
                            "type": "context",
                            "elements": [{
                                "type": "mrkdwn",
                                "text": first_line
                            }]
                        })

                blocks.append({"type": "divider"})
                
                # 폴백 텍스트용
                text_contents.extend([
                    title,
                    f"`{nr['tag']}` • {nr['published']}",
                    first_line if description else "",
                    "---"
                ])
            
            # 마지막 구분선 제거
            if blocks:
                blocks.pop()
            
            payload = {
                "blocks": blocks,
                "text": "\n".join(text for text in text_contents if text)
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
