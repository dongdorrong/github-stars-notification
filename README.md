# 🌟 GitHub Stars 릴리스 알림

<div align="center">

[![Workflow Status](https://github.com/dongdorrong/github-stars-notification/actions/workflows/notify-starred-releases.yml/badge.svg)](https://github.com/dongdorrong/github-stars-notification/actions)
[![GitHub stars](https://img.shields.io/github/stars/dongdorrong/github-stars-notification?style=social)](https://github.com/dongdorrong/github-stars-notification)

GitHub에서 스타를 준 저장소의 새로운 릴리스를 자동으로 감지하여 <br>
Slack으로 알림을 보내주는 GitHub Actions 워크플로우입니다. ✨

</div>

## 🎯 기능

- 🔍 GitHub 스타 저장소의 최신 릴리스 자동 감지
- ⏰ 하루 2회 (오전 9시, 오후 6시) 자동 체크
- 💬 Slack을 통한 새로운 릴리스 알림
- 💾 릴리스 정보 캐싱으로 중복 알림 방지

## ⚙️ 설정 방법

### 1️⃣ GitHub Personal Access Token (PAT) 생성
```bash
✓ repo:read 권한 필요
✓ Repository Secrets에 GH_PAT로 저장
```

### 2️⃣ Slack Webhook URL 설정
```bash
✓ Slack App에서 Incoming Webhook 생성
✓ Repository Secrets에 SLACK_WEBHOOK로 저장
```

## 📬 알림 형식

새로운 릴리스가 감지되면 다음 정보가 Slack으로 전송됩니다:

| 항목 | 설명 |
|------|------|
| 📦 저장소 이름 | organization/repository |
| 🏷️ 릴리스 태그 | 클릭 가능한 링크 포함 |
| 📝 릴리스 이름 | 태그와 다른 경우 표시 |
| 📅 발행 날짜 | YY.MM.DD 형식 |

## 🚀 수동 실행

워크플로우는 GitHub Actions 탭에서 `Run workflow` 버튼을 통해 수동으로도 실행할 수 있습니다.

---

<div align="center">
Made with ❤️ by <a href="https://github.com/dongdorrong">dongdorrong</a>
</div> 