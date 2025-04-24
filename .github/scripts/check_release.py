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
import yaml  # 상단에 추가

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

def get_project_theme(repo: str) -> tuple[str, str]:
    """프로젝트별 테마(색상, 이모지) 반환"""
    # 프로젝트별 고정 테마
    THEMES = {
        "kubernetes": ("#326CE5", "☸️"),    # 쿠버네티스 블루
        "elastic": ("#00BFB3", "🔍"),       # Elastic 티얼
        "grafana": ("#F46800", "📊"),       # Grafana 오렌지
        "prometheus": ("#E6522C", "📈"),     # Prometheus 레드
        "istio": ("#466BB0", "🔀"),         # Istio 블루
        "terraform": ("#7B42BC", "🏗️"),     # Terraform 퍼플
        "helm": ("#0F1689", "⎈"),          # Helm 네이비
        "docker": ("#2496ED", "🐳"),        # Docker 블루
    }
    
    # 기본 테마 (여러 가지 중 하나를 해시값 기반으로 선택)
    DEFAULT_THEMES = [
        ("#2EB67D", "📦"),  # 초록색
        ("#ECB22E", "🔆"),  # 노란색
        ("#E01E5A", "💫"),  # 빨간색
        ("#36C5F0", "✨"),  # 하늘색
    ]
    
    org = repo.split('/')[0].lower()
    if theme := THEMES.get(org):
        return theme
        
    # 저장소 이름을 해시하여 일관된 테마 선택
    hash_value = sum(ord(c) for c in repo)
    return DEFAULT_THEMES[hash_value % len(DEFAULT_THEMES)]

def normalize_repo_name(repo: str) -> str:
    """저장소 이름에서 슬래시 주변의 공백을 제거"""
    return repo.replace(" / ", "/")

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

def format_release_info(repo: str, release_data: dict, tag: str, published: str) -> dict:
    """릴리스 정보를 슬랙 메시지 블록으로 포맷팅"""
    # 설정 로드
    config = load_config()
    is_special = normalize_repo_name(repo) in config["special_projects"]
    
    # 색상과 이모지 결정
    color, emoji = get_project_theme(repo)
    
    parts = []
    
    # 1. 저장소 이름
    org, repo_name = repo.split('/')
    header = f"{emoji} *{org}* / *{repo_name}*"
    
    # 특별 프로젝트인 경우 스타일 강조
    if is_special:
        header = f"🌟 {header}"  # 특별 프로젝트 표시
    
    parts.append(header)
    
    # 2. 태그 정보와 릴리스 링크
    tag_info = f"<{release_data['html_url']}|`{tag}`>"
    if release_name := release_data.get("name", "").strip():
        if release_name != tag:
            prefixes = ["Release ", "release ", "version ", "v", "Version "]
            for prefix in prefixes:
                if release_name.lower().startswith(prefix.lower()):
                    release_name = release_name[len(prefix):]
            tag_info = f"<{release_data['html_url']}|`{tag}`> - _{release_name.strip()}_"
    parts.append(tag_info)
    
    # 3. 날짜 정보
    date_info = format_date(published)
    parts.append(date_info)
    
    return {
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": " ".join(parts)
        },
        "color": color
    }

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
            
            # 안내 메시지 추가
            guide_text = ("💡 *중요한 프로젝트가 있다면 관심 프로젝트로 등록해보세요!*\n"
                         "• `config.yaml` 파일에 프로젝트를 추가하면 🌟 로 강조 표시됩니다\n"
                         "• GitHub에서 프로젝트 이름을 복사해서 그대로 붙여넣으시면 됩니다")
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": guide_text
                }
            })
            
            blocks.append({"type": "divider"})
            text_contents.extend([header_text, guide_text])
            
            for nr in new_releases:
                release_data = gh_get(f"https://api.github.com/repos/{nr['repo']}/releases/tags/{nr['tag']}")
                if not release_data:
                    continue

                # 메시지 블록 구성
                block = format_release_info(nr['repo'], release_data, nr['tag'], nr['published'])
                blocks.append(block)
                text_contents.append(block["text"]["text"])
            
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
