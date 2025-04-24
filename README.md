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
- 🎨 프로젝트별 자동 테마 적용
- ⭐ 관심 프로젝트 하이라이팅

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
special_projects:
  - "kubernetes/kubernetes"
  - "elastic/elasticsearch"
  - "grafana/grafana"
```
관심 프로젝트로 등록된 저장소의 릴리스는 🌟 이모지와 함께 강조되어 표시됩니다.

## 📬 알림 스타일

릴리스 알림은 프로젝트별로 다른 스타일이 자동으로 적용됩니다:

### 주요 프로젝트 테마
| 프로젝트 | 색상 | 이모지 | 예시 |
|---------|------|--------|------|
| Kubernetes | 파란색 | ☸️ | ☸️ kubernetes/kubernetes |
| Elastic | 티얼 | 🔍 | 🔍 elastic/elasticsearch |
| Grafana | 주황색 | 📊 | 📊 grafana/grafana |
| Prometheus | 빨간색 | 📈 | 📈 prometheus/prometheus |
| 기타 | 자동 할당 | 자동 할당 | ✨ owner/repository |

- 주요 오픈소스 프로젝트는 해당 프로젝트의 브랜드 색상과 관련 이모지가 적용됩니다
- 그 외 프로젝트는 저장소 이름을 기반으로 일관된 테마가 자동으로 할당됩니다
- `config.yaml`에 등록된 관심 프로젝트는 🌟 이모지가 추가로 표시됩니다

## 🚀 수동 실행

워크플로우는 GitHub Actions 탭에서 `Run workflow` 버튼을 통해 수동으로도 실행할 수 있습니다.

---

<div align="center">
Made with ❤️ by <a href="https://github.com/dongdorrong">dongdorrong</a>
</div> 