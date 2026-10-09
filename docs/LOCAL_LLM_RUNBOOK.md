# 선택적 로컬 LLM 분석 운영

`starwatch.analysis`는 공식 이벤트의 **보조 분석**만 수행한다. 기본값은 `analysis.enabled: false`이며 네트워크 없이 결정적 fallback을 반환한다. 분석 결과는 원본 event/outbox와 별도로 저장해야 한다. LLM은 event ID, 중복, 출처 신뢰도, outbox, Slack 전송, 보안 우선순위 하향 권한이 없다.

## 설정 및 비밀정보

```yaml
analysis:
  enabled: false
  provider: openai_compatible
  base_url_env: LLM_BASE_URL
  api_key_env: LLM_API_KEY
  model: local-model
  timeout_seconds: 30
  max_retries: 1
  max_input_chars: 24000
  max_output_tokens: 1200
  prompt_version: v1
  fail_open_to_fallback: true
```

URL과 API key **값**은 환경/비밀 저장소에서만 받으며 YAML, feed, 로그, GitHub Actions output, PR에 남기지 않는다. `LLM_BASE_URL`은 OpenAI-compatible API의 base URL (`.../v1` 또는 origin)이다. 원격/LAN endpoint는 HTTPS가 필수이며 HTTP는 숫자 loopback IP (`127.0.0.1`, `::1`)만 허용한다. `localhost` 대신 숫자 loopback을 사용한다. 클라이언트는 `POST /v1/chat/completions`만 사용하며, redirect를 따르지 않고 proxy를 사용하지 않는다. Socket idle timeout과 POSIX main-thread hard wall deadline을 함께 적용해 응답이 한 바이트씩 도착해도 설정 시간을 넘기지 않는다. 기존 alarm이 있는/지원되지 않는 실행면에서는 fail closed 후 fallback한다. 기본 Actions 경로는 접근할 수 없는 개인 네트워크 LLM에 의존하지 않도록 분석을 비활성화한다. 이 작업의 fixture 검증은 가짜 transport와 localhost fake HTTP server만 사용하며 실제 모델 endpoint를 호출하지 않는다.

## 입력·출력 계약

- 입력은 normalized raw event 사본을 결정적으로 JSON 직렬화해 길이를 제한한 뒤 `untrusted_event_json` 문자열 필드에 넣는다. 고정 system policy와 별도의 user message다. 소스에 포함된 `Ignore previous instructions`, `</system>`, JSON/Markdown, Slack 명령은 **인용 데이터**일 뿐이다.
- 모델에는 도구, 브라우징, shell, GitHub/Slack 쓰기 권한을 제공하지 않는다. 권고된 작업은 실행하지 않는다.
- 응답은 정확히 `summary_ko`, `impact`, `categories`, `operator_attention`, `reason`, `recommended_actions`, `affected_components`, `confidence`만 허용한다. event ID/hash, provider/model, prompt version, 절단 여부, UTC 시각은 코드가 채운다.
- 완성된 결과는 `schemas/ai-analysis-v1.json`과 동등한 엄격한 런타임 validator를 통과해야만 성공 분석으로 저장한다. unknown fields, 중복 JSON key, enum 이탈, 0~1 범위 밖 confidence, 길이 초과, 과대 응답은 거절한다.
- cache key는 `(event_id, content_hash, schema_version, prompt_version, provider, model)`이다. 원본 수정, prompt/model 변경은 다시 분석한다. 결정적 fallback도 별도 `provider: deterministic`, floor별 `model: deterministic-rules-<floor>`로 저장·재사용하지만 성공 AI 분석으로 세지 않는다. AI endpoint 장애 뒤에는 해당 provider/model key가 비어 있으므로 다음 실행에서 정상 분석을 재시도한다.
- `effective_priority`는 유효한 AI 제안과 결정적 floor의 상한이다. `SUPPRESSED`는 AI로 승격할 수 없다. Critical GHSA는 모델이 `low`라고 해도 `CRITICAL`이다.

## 실패와 복구

HTTP 401/403, invalid JSON/schema, timeout, 네트워크 장애, 429/5xx는 안전한 범주만 노출한다. 응답 오류 본문을 읽거나 로그에 남기지 않는다. 기본 `fail_open_to_fallback: true`는 공식 이벤트 수집과 결정적 알림을 유지한다. 재시도는 429/5xx/네트워크 오류에 한해 최대 `max_retries`회이며 고정 sleep을 넣지 않는다. 장애가 지속되면 차기 실행에서 재시도할 수 있고 모델 성공으로 거짓 기록하지 않는다. `fail_open_to_fallback: false`는 명시적 로컬 실험용이며 안전 범주 오류를 발생시킨다.

Fallback은 이벤트 유형·프로젝트·결정적 priority floor만 사용한다. GHSA에는 affected/patched version을 **공식 권고문에서 확인하라**고 권하지만, 버전이나 취약 구성요소를 지어내지 않는다. 일반 Release에는 security claim을 만들지 않는다.

## 검증

```bash
python3 -m unittest tests.test_analysis -v
```

테스트는 fake transport로 JSON/schema, timeout/401/403/429/5xx, cache invalidation, prompt injection, Critical 하향 금지, 무동작 fallback을 검증한다. 실제 LLM 품질·가용성은 허가된 별도 운영 검증 전까지 미검증이다.
