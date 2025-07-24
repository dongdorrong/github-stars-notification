# 🌟 GitHub Stars 릴리스 알림

<div align="center">

[![Workflow Status](https://github.com/dongdorrong/github-stars-notification/actions/workflows/notify-starred-releases.yml/badge.svg)](https://github.com/dongdorrong/github-stars-notification/actions)
[![GitHub stars](https://img.shields.io/github/stars/dongdorrong/github-stars-notification?style=social)](https://github.com/dongdorrong/github-stars-notification)

GitHub에서 스타를 준 저장소의 새로운 릴리스를 자동으로 감지하여 <br>
Slack으로 알림을 보내주는 GitHub Actions 워크플로우입니다. ✨

</div>

## 🎯 기능

- 🔍 GitHub 스타 저장소의 최신 릴리스 자동 감지
- ⏰ 매일 오전 9시 자동 체크
- 💬 Slack을 통한 새로운 릴리스 알림 (최신순 정렬)
- 💾 릴리스 정보 캐싱으로 중복 알림 방지
- ⭐ 관심 프로젝트 강조 표시
- 📝 버전 업데이트 감지

<div align="center">

![GitHub Stars Notification](images/sample.png)

</div>

## ⚙️ 설정 방법

### 1️⃣ GitHub Personal Access Token (PAT) 생성
```bash
✓ repo:read 권한 필요
✓ Repository Secrets에 GH_PAT로 저장
```

### 2️⃣ Slack Webhook URL 설정
```bash
✓ Slack 워크스페이스에서 Incoming Webhook 생성
✓ Repository Secrets에 SLACK_WEBHOOK_URL로 저장
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
GitHub에서 프로젝트 이름을 복사해서 그대로 붙여넣으면 됩니다.

## 📬 알림 형식

새로운 릴리스가 감지되면 다음과 같은 형식으로 Slack 메시지가 전송됩니다:

### 메시지 구성
1. 헤더
   ```
   🚀 새로운 릴리스를 확인했습니다
   ```

2. 가이드 메시지
   ```
   💡 중요한 프로젝트가 있다면 관심 프로젝트로 등록해보세요!
   • config.yaml 파일에 프로젝트를 추가하면 ⭐ 로 강조 표시됩니다
   • GitHub에서 프로젝트 이름을 복사해서 그대로 붙여넣으시면 됩니다
   ```

3. 프로젝트 목록
   ```
   [일반 프로젝트]
   *organization* / *repository* v1.2.3 - Release Name 25.04.16

   [관심 프로젝트]
   ⭐ *organization* / *repository* v1.2.3 - Release Name 25.04.16
   ```

### 표시 항목
- 저장소 이름 (`*organization* / *repository*` 형식)
- 릴리스 태그 (클릭 가능한 링크)
- 릴리스 이름 (태그와 다른 경우, 이탤릭체)
- 발행 날짜 (YY.MM.DD)

### 특별 표시
- ⭐ : 관심 프로젝트 (config.yaml에 등록된 프로젝트)

## 🚀 수동 실행

워크플로우는 GitHub Actions 탭에서 `Run workflow` 버튼을 통해 수동으로도 실행할 수 있습니다.

---

<div align="center">
Made with ❤️ by <a href="https://github.com/dongdorrong">dongdorrong</a>
</div> 