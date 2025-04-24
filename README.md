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
- ⭐ 관심 프로젝트 강조 표시

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

### 3️⃣ 관심 프로젝트 설정 (선택사항)
특별히 관심있는 프로젝트는 `config.yaml` 파일에 추가할 수 있습니다:
```yaml
# 특별히 관심있는 프로젝트 목록
special_projects:
  - "kubernetes / kubernetes"
  - "elastic / elasticsearch"
  - "grafana / grafana"
```
GitHub에서 프로젝트 이름을 복사해서 그대로 붙여넣으면 됩니다. (슬래시 주변 공백은 자동으로 처리됩니다)

## 📬 알림 형식

새로운 릴리스가 감지되면 다음 정보가 Slack으로 전송됩니다:

```
[일반 프로젝트]
organization / repository v1.2.3 - Release Name 25.04.16

[관심 프로젝트]
⭐ organization / repository v1.2.3 - Release Name 25.04.16
```

- 저장소 이름
- 릴리스 태그 (클릭 가능한 링크)
- 릴리스 이름 (태그와 다른 경우)
- 발행 날짜 (YY.MM.DD)
- 관심 프로젝트는 ⭐로 강조 표시

## 🚀 수동 실행

워크플로우는 GitHub Actions 탭에서 `Run workflow` 버튼을 통해 수동으로도 실행할 수 있습니다.

---

<div align="center">
Made with ❤️ by <a href="https://github.com/dongdorrong">dongdorrong</a>
</div> 