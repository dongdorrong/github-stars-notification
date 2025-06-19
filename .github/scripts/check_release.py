#!/usr/bin/env python3
# pylint: disable=import-error
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
import sys
import time
from pathlib import Path
import yaml
from github import Github  # type: ignore
from github.GithubException import GithubException  # type: ignore

# 0. 설정 -------------------------------------------------------------------
CACHE_PATH = Path(".cache/releases.json")   # 이전 릴리즈 캐시
REPOS_FILE = Path("repos.txt")              # workflow 앞 단계에서 생성

token = os.getenv("GH_TOKEN")
if not token:
    print("GH_TOKEN env required", file=sys.stderr)
    sys.exit(1)

# GitHub API 클라이언트 초기화
gh = Github(token)

def normalize_repo_name(repo: str) -> str:
    """저장소 이름에서 슬래시 주변의 공백을 제거"""
    return repo.replace(" / ", "/")

def get_latest_release(repo: str) -> dict | None:
    """저장소의 최신 릴리즈 정보를 가져옴"""
    try:
        repository = gh.get_repo(repo)
        latest_release = repository.get_latest_release()
        return {
            "tag_name": latest_release.tag_name,
            "name": latest_release.title,
            "published_at": latest_release.published_at.strftime("%Y-%m-%d"),
            "html_url": latest_release.html_url
        }
    except GithubException as e:
        if e.status == 404:
            return None
        raise

def load_cache() -> dict:
    """캐시 파일에서 이전 릴리즈 정보 로드"""
    if CACHE_PATH.exists():
        return json.loads(CACHE_PATH.read_text())
    return {}

def save_cache(data: dict) -> None:
    """릴리즈 정보를 캐시 파일에 저장"""
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2))

def is_first_run() -> bool:
    """캐시 파일의 존재 여부로 첫 실행인지 확인"""
    return not CACHE_PATH.exists()

def load_config() -> dict:
    """설정 파일 로드"""
    config_path = Path("config.yaml")
    if config_path.exists():
        data = yaml.safe_load(config_path.read_text())
        # 프로젝트 이름 정규화
        if "special_projects" in data:
            data["special_projects"] = [normalize_repo_name(repo) for repo in data["special_projects"]]
        return data
    return {"special_projects": []}

def format_date(date_str: str) -> str:
    """날짜를 더 읽기 쉬운 형식으로 변환"""
    return date_str.replace('-', '.')[2:]  # '2025-04-16' -> '25.04.16'



def main() -> None:
    # 캐시 파일 존재 여부 확인
    first_run = is_first_run()
    prev = load_cache()
    current: dict[str, dict] = {}
    new_releases: list[dict] = []

    for repo in REPOS_FILE.read_text().splitlines():
        repo = repo.strip()
        if not repo:
            continue
        data = get_latest_release(repo)
        if not data:
            continue
        tag = data["tag_name"]
        published = data["published_at"]
        current[repo] = {"tag": tag, "published": published}

        # 첫 실행일 때는 모든 릴리스를 포함
        if first_run:
            new_releases.append({
                "repo": repo,
                "tag": tag,
                "name": data.get("name") or "",
                "published": published,
                "html_url": data["html_url"],
            })
        else:
            prev_info = prev.get(repo)
            prev_published = prev_info["published"] if prev_info else None
            # 날짜 비교: 최신 릴리스가 더 최신이면 알림
            if (not prev_published) or (published > prev_published):
                new_releases.append({
                    "repo": repo,
                    "tag": tag,
                    "name": data.get("name") or "",
                    "published": published,
                    "html_url": data["html_url"],
                })
        time.sleep(0.3)

    # 날짜순으로 정렬 (최신순)
    new_releases.sort(key=lambda x: x["published"], reverse=True)

    # 캐시 저장
    save_cache(current)

    # 디버깅 정보 출력
    print(f"DEBUG: Found {len(new_releases)} new releases")
    print(f"DEBUG: First run: {first_run}")
    if new_releases:
        print("DEBUG: New releases found:")
        for nr in new_releases[:3]:  # 처음 3개만 출력
            print(f"  - {nr['repo']}: {nr['tag']} ({nr['published']})")

    # GitHub Actions 출력
    outputs_file = Path(os.environ["GITHUB_OUTPUT"])
    with outputs_file.open("a") as f:
        if new_releases:
            # 헤더 텍스트
            header_text = "🌟 **스타 저장소의 현재 릴리스 목록입니다**" if first_run else "🚀 **새로운 릴리스를 확인했습니다**"
            guide_text = ("💡 **중요한 프로젝트가 있다면 관심 프로젝트로 등록해보세요!**\n"
                         "• `config.yaml` 파일에 프로젝트를 추가하면 ⭐ 로 강조 표시됩니다\n"
                         "• GitHub에서 프로젝트 이름을 복사해서 그대로 붙여넣으시면 됩니다")
            
            # 모든 릴리스 정보를 하나의 문자열로 구성
            config = load_config()
            release_lines = []
            
            for nr in new_releases:
                is_special = normalize_repo_name(nr['repo']) in config["special_projects"]
                org, repo_name = nr['repo'].split('/')
                
                # 제목 구성
                title = f"**{org}** / **{repo_name}**"
                if is_special:
                    title = f"⭐ {title}"
                
                # 설명 구성
                description_parts = [f"[`{nr['tag']}`]({nr['html_url']})"]
                if release_name := nr.get("name", "").strip():
                    if release_name != nr['tag']:
                        prefixes = ["Release ", "release ", "version ", "v", "Version "]
                        for prefix in prefixes:
                            if release_name.lower().startswith(prefix.lower()):
                                release_name = release_name[len(prefix):]
                        description_parts.append(f"*{release_name.strip()}*")
                
                description_parts.append(format_date(nr['published']))
                
                # 한 줄로 구성
                release_line = f"{title} {' - '.join(description_parts)}"
                release_lines.append(release_line)
            
            # Discord 제한에 맞게 여러 메시지로 분할
            MAX_DESC_LENGTH = 3800  # 4096자 제한에서 여유분 확보
            
            # 첫 번째 메시지: 헤더 + 가이드 + 일부 릴리스
            header_desc = f"{guide_text}\n\n---\n\n"
            current_desc = header_desc
            messages = []
            current_releases = []
            
            for i, release_line in enumerate(release_lines):
                test_desc = current_desc + release_line + "\n"
                
                if len(test_desc) > MAX_DESC_LENGTH and current_releases:
                    # 현재 메시지 완성하고 새 메시지 시작
                    embed = {
                        "title": header_text if not messages else f"🚀 **새로운 릴리스 (계속) - {len(messages)+1}**",
                        "description": current_desc.rstrip(),
                        "color": 0x5865F2,
                        "timestamp": new_releases[0]["published"] + "T00:00:00.000Z"
                    }
                    messages.append(embed)
                    
                    # 새 메시지 시작 (헤더는 첫 번째만)
                    current_desc = release_line + "\n"
                    current_releases = [release_line]
                else:
                    current_desc = test_desc
                    current_releases.append(release_line)
            
            # 마지막 메시지 추가
            if current_releases:
                embed = {
                    "title": header_text if not messages else f"🚀 **새로운 릴리스 (마지막) - {len(messages)+1}**",
                    "description": current_desc.rstrip(),
                    "color": 0x5865F2,
                    "timestamp": new_releases[0]["published"] + "T00:00:00.000Z"
                }
                messages.append(embed)
            
            # 여러 메시지가 있을 경우 총 개수 표시
            if len(messages) > 1:
                for i, message in enumerate(messages):
                    if i == 0:
                        message["title"] = f"🚀 **새로운 릴리스 ({len(new_releases)}개) - 1/{len(messages)}**"
                    else:
                        message["title"] = f"🚀 **새로운 릴리스 (계속) - {i+1}/{len(messages)}**"
            
            embeds = messages
            
            # 여러 Discord 웹훅 payload 형식
            payloads = []
            for i, embed in enumerate(embeds):
                payload = {
                    "content": "",
                    "embeds": [embed]
                }
                payloads.append(payload)
            
            # JSON 배열로 모든 payload 출력 (동적 처리용)
            payloads_json = json.dumps(payloads)
            
            f.write(f"has_new=true\n")
            f.write(f"message_count={len(payloads)}\n")
            f.write(f"payloads={payloads_json}\n")
            
            print(f"DEBUG: Generated {len(payloads)} messages")
            for i, payload in enumerate(payloads):
                print(f"DEBUG: Message {i+1}: {payload['embeds'][0]['title']}")
                print(f"DEBUG: Description length: {len(payload['embeds'][0]['description'])}")
            
            # 기존 호환성을 위한 첫 번째 payload
            if payloads:
                safe = json.dumps(payloads[0]).replace("%", "%25").replace("\n", "%0A").replace("\r", "%0D")
                f.write(f"payload={safe}\n")
        else:
            # 테스트를 위해 빈 메시지라도 보내도록 함
            empty_payload = {
                "content": "테스트: 새로운 릴리스가 없습니다.",
                "embeds": []
            }
            safe = json.dumps(empty_payload).replace("%", "%25").replace("\n", "%0A").replace("\r", "%0D")
            f.write(f"payload={safe}\n")
            f.write("has_new=false\n")
            print("DEBUG: No new releases found, sending test message")

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("ERROR:", exc, file=sys.stderr)
        sys.exit(1)
