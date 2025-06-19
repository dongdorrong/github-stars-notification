#!/usr/bin/env python3
"""
여러 Discord 메시지 전송 테스트 스크립트
"""
import json
import requests
import urllib.parse
import time
import os

DISCORD_WEBHOOK_URL = "https://discordapp.com/api/webhooks/1385049714264772709/jHlG2m90iDBGspBbNNcgcycAqw8HslzOrfYDB6CzPQj98py1ac59ERJL8kKs5Jk8Bjs0"

def read_github_output():
    """GitHub Actions 출력에서 payload들을 읽어옴"""
    payloads = {}
    
    try:
        with open("/tmp/github_output_test", "r") as f:
            lines = f.readlines()
            
        for line in lines:
            line = line.strip()
            if line.startswith("payload_"):
                parts = line.split("=", 1)
                if len(parts) == 2:
                    key = parts[0]
                    value = parts[1]
                    # URL 디코딩
                    decoded = urllib.parse.unquote(value)
                    try:
                        payload = json.loads(decoded)
                        payloads[key] = payload
                        print(f"✅ {key} 로드 완료")
                    except json.JSONDecodeError as e:
                        print(f"❌ {key} JSON 디코딩 실패: {e}")
                        
    except FileNotFoundError:
        print("❌ /tmp/github_output_test 파일을 찾을 수 없습니다")
        
    return payloads

def send_to_discord(payload, message_num):
    """Discord로 메시지 전송"""
    try:
        response = requests.post(DISCORD_WEBHOOK_URL, json=payload)
        if response.status_code == 204:
            print(f"✅ 메시지 {message_num} 전송 성공!")
            return True
        else:
            print(f"❌ 메시지 {message_num} 전송 실패: {response.status_code} - {response.text}")
            return False
    except Exception as e:
        print(f"❌ 메시지 {message_num} 전송 오류: {e}")
        return False

def main():
    print("=== Discord 여러 메시지 전송 테스트 ===")
    
    # Payload 읽기
    payloads = read_github_output()
    
    if not payloads:
        print("❌ 전송할 payload가 없습니다")
        return
    
    print(f"📝 총 {len(payloads)}개의 메시지를 전송합니다")
    
    # 순차적으로 전송
    for i in range(1, 6):  # payload_1부터 payload_5까지
        key = f"payload_{i}"
        if key in payloads:
            print(f"\n📤 메시지 {i} 전송 중...")
            
            payload = payloads[key]
            embed = payload['embeds'][0]
            print(f"   제목: {embed['title']}")
            print(f"   설명 길이: {len(embed['description'])}자")
            
            success = send_to_discord(payload, i)
            
            if success and i < len(payloads):
                print(f"⏳ Rate limit 방지를 위해 3초 대기...")
                time.sleep(3)
        else:
            print(f"ℹ️ {key}가 존재하지 않습니다")
            break
    
    print("\n🎉 모든 메시지 전송 완료!")

if __name__ == "__main__":
    main()
