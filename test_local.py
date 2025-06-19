#!/usr/bin/env python3
"""
로컬 테스트용 스크립트
GitHub starred repos를 가져와서 Discord 웹훅 테스트
"""
import os
import json
import requests
from github import Github

# 환경변수 설정 (GitHub 토큰 필요)
GH_TOKEN = os.getenv("GH_TOKEN")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

if not GH_TOKEN:
    print("GH_TOKEN 환경변수를 설정해주세요")
    print("export GH_TOKEN=your_github_token")
    exit(1)

if not DISCORD_WEBHOOK_URL:
    print("DISCORD_WEBHOOK_URL 환경변수를 설정해주세요")
    print("export DISCORD_WEBHOOK_URL=your_discord_webhook_url")
    exit(1)

def get_starred_repos():
    """GitHub starred repos 목록 가져오기"""
    gh = Github(GH_TOKEN)
    user = gh.get_user()
    starred = user.get_starred()
    
    repos = []
    print("   전체 starred repos를 가져오는 중...")
    for repo in starred:  # 모든 starred repos
        repos.append(repo.full_name)
        if len(repos) % 50 == 0:  # 50개마다 진행상황 출력
            print(f"   진행중... {len(repos)}개 완료")
        
    return repos

def create_test_payload():
    """테스트용 Discord payload 생성"""
    payload = {
        "embeds": [{
            "title": "🚀 **GitHub Stars Notification 테스트**",
            "description": "로컬에서 Discord 웹훅 테스트 중입니다.",
            "color": 0x5865F2,
            "fields": [
                {
                    "name": "테스트 상태",
                    "value": "성공적으로 메시지가 전송되었습니다!",
                    "inline": False
                }
            ]
        }]
    }
    return payload

def send_to_discord(payload):
    """Discord로 메시지 전송"""
    try:
        response = requests.post(DISCORD_WEBHOOK_URL, json=payload)
        print(f"Discord 응답 상태코드: {response.status_code}")
        if response.status_code == 204:
            print("✅ Discord 메시지 전송 성공!")
        else:
            print(f"❌ Discord 메시지 전송 실패: {response.text}")
        return response.status_code == 204
    except Exception as e:
        print(f"❌ 오류 발생: {e}")
        return False

def main():
    print("=== GitHub Stars Notification 로컬 테스트 ===")
    
    # GitHub starred repos 가져오기
    print("1. GitHub starred repos 가져오는 중...")
    try:
        repos = get_starred_repos()
        print(f"✅ {len(repos)}개의 starred repos를 가져왔습니다")
        for repo in repos[:5]:  # 처음 5개만 출력
            print(f"   - {repo}")
        if len(repos) > 5:
            print(f"   ... 그리고 {len(repos)-5}개 더")
            
        # repos.txt 파일 생성
        with open("repos.txt", "w") as f:
            f.write("\n".join(repos))
        print("repos.txt 파일을 생성했습니다")
            
    except Exception as e:
        print(f"❌ GitHub API 오류: {e}")
        return
    
    # Discord 테스트 메시지 전송
    print("\n2. Discord 웹훅 테스트 중...")
    payload = create_test_payload()
    
    print("전송할 payload:")
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    
    success = send_to_discord(payload)
    
    if success:
        print("\n🎉 로컬 테스트 완료! Discord 웹훅이 정상 작동합니다.")
        print("이제 실제 스크립트도 실행해보겠습니다...")
        
        # 실제 스크립트 실행
        print("\n3. 실제 check_release.py 스크립트 실행...")
        os.environ["GITHUB_OUTPUT"] = "/tmp/github_output_test"
        
        # GitHub Actions 출력 파일 생성
        with open("/tmp/github_output_test", "w") as f:
            pass
            
        try:
            import sys
            sys.path.append('.github/scripts')
            from check_release import main as check_main
            check_main()
            
            # 결과 확인
            with open("/tmp/github_output_test", "r") as f:
                output = f.read()
                print("GitHub Actions 출력:")
                print(output)
                
        except Exception as e:
            print(f"❌ check_release.py 실행 오류: {e}")
    else:
        print("\n❌ Discord 웹훅 테스트 실패")
        print("DISCORD_WEBHOOK_URL이 올바른지 확인해주세요")

if __name__ == "__main__":
    main() 