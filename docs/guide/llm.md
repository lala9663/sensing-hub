# LLM 연결과 무료 한도 (공통 설정)

탭 하나에 속하지 않는 공통 설정입니다. LLM은 네 곳에서 쓰고, 모두 선택 사항입니다 — LLM이 없어도 수집·감시·이슈·구독은 동작합니다.

## 어디에 쓰나

| 쓰는 곳 | 하는 일 | 어디서 부르나 | 문서 |
| --- | --- | --- | --- |
| 등록 레시피 | 규칙이 못 잡는 사이트의 허브·URL 서명·셀렉터 제안 | 워커 (용도 `recipe`) | [01-registrations.md](01-registrations.md#llm-레시피-선택) |
| 요약·분류 | 문서마다 토픽·태그, 공공기관 문서 요약 | 워커 (용도 `enrich`) | [02-documents.md](02-documents.md) |
| 챗봇 | 질문을 도구 호출로 바꾸고 답을 씀 | 백엔드 (용도 `chat`) | [08-chatbot.md](08-chatbot.md) |
| 의미 검색 임베딩 | 문서 조각·질의를 벡터로 | 워커·백엔드 (별도 `SENSING_EMBED_*`) | [08-chatbot.md](08-chatbot.md#의미-검색) |

관리자 → 운영 지표의 "LLM 사용량 (오늘)" 카드에서 오늘 쓴 양을 봅니다([09-admin.md](09-admin.md#운영-지표)).

## 어떻게 동작하나

- 제공자는 교체할 수 있습니다: Claude API(`anthropic`), OpenAI 호환 `chat/completions`(`openai` — Gemini OpenAI 호환 주소·vLLM·Ollama 등 사내 반입 모델).
  백엔드는 추가 의존성 없이 `java.net.http`로, 워커는 `llm.py`로 부르고, 백엔드와 워커가 **같은 이름의 환경 변수**를 읽습니다.
- **JSON 형식은 모델에 맡기지 않습니다.** 제공자가 스키마 강제(`json_schema`)를 지원하면 힌트로 쓰되, 응답은 항상 우리 쪽에서 JSON을 꺼내
  스키마를 검증하고, 실패하면 오류 메시지를 붙여 한 번만 다시 요청합니다. OpenAI 호환 서버가 `response_format`을 400으로 거부하면
  `json_object` → 없음 순으로 낮춰 다시 보냅니다.
- LLM 장애는 수집을 막지 않습니다: 레시피는 휴리스틱으로, 요약은 다음 루프로 미루고, 챗봇은 안내 답을 저장합니다.

### Gemini 무료 등급 실측 (2026-10-19)

코드 변경 없이 `openai` 제공자로 붙었고, 쌓여 있던 문서 7,967건을 모두 분류·요약했습니다(실패 0, 비용 0). 언론 기사는 제목만 보내므로 30건 묶음 한 번이
입력 약 3천 토큰입니다. 공공기관 문서 요약은 두 문장으로 수치까지 정확했고, 분류는 정치 발언 기사를 "사건·사고"·"산업동향"에 넣는 식의 오분류가 가끔 있습니다.

| 모델 | 무료 한도 | 판단 |
| --- | --- | --- |
| `gemini-3.5-flash-lite` | 하루 약 500회 (모델별) | 권장. `SENSING_ENRICH_BATCH=30`이면 하루 약 1만5천 건 |
| `gemini-3.8-flash` | 하루 20회 | 분류가 조금 더 정확하지만 밀린 문서를 처리하면 곧 HTTP 429 (워커는 아무것도 쓰지 않고 다음에 다시) |
| `gemini-2.5-flash` 등 옛 모델 | — | 신규 사용자에게 404. 쓸 수 있는 모델은 `{BASE_URL}/models`로 확인 |

## LLM 무료 한도 나눠 쓰기

챗봇(백엔드)·자동 요약(워커 enrich)·등록 레시피(워커 recipe)는 같은 모델·같은 키를 쓰고, 무료 한도는 모델별·하루 요청 수입니다.
2026-10-19 워커 2대가 요약으로 Flash-Lite 하루 500번을 다 써 챗봇이 막혔고, 워커는 한도 초과 뒤에도 루프마다 다시 불러 429를 1,293번
받았습니다. 그래서 세 용도가 DB의 사용량 장부를 함께 씁니다(V19, 결정 배경은 [../decisions.md](../decisions.md)).

- **장부** (`llm_usage`): 쿼터 기준 날짜(`SENSING_LLM_QUOTA_TZ`)·모델·용도(`chat`·`enrich`·`recipe`)별 호출 수·429 수·실패 수·토큰.
  호출 전에 원자적으로 하나 늘리고, 429로 거절된 요청은 호출 수에서 빼 429 수로 옮깁니다.
- **요약 하루 상한** (`SENSING_ENRICH_DAILY_CALLS`, 기본 300): 워커 여러 대가 합쳐도 넘지 않게 DB에서 예약합니다. 닿으면 그날은 요약을 쉬고
  (문서를 잠그지도 않는다, 로그는 쉬기 시작할 때 한 번) 다음 날 남은 문서부터 이어 합니다. 레시피·챗봇은 상한 없이 남은 한도를 씁니다.
  기본값 300은 Gemini Flash-Lite 무료 하루 500번 중 200번을 챗봇·등록 레시피 몫으로 남긴 값입니다.
- **429 뒤 쉬기** (`llm_cooldown`): 응답의 `quotaId`·`retryDelay`(Claude 등은 `retry-after` 헤더)를 읽어, 하루 한도(`...PerDay...`)면
  그 모델을 쿼터 기준 다음 자정과 `retryDelay` 중 늦은 쪽까지, 분당 한도 등은 `retryDelay`(없으면 60초)만큼 "쉬는 중"으로 적습니다.
  그동안 워커는 요약·레시피 호출을 하지 않고(레시피는 휴리스틱으로 등록), 챗봇은 LLM을 부르지 않고 "LLM 하루 사용 한도(무료 등급)를 오늘 다
  썼습니다. 한국 시간 16:00쯤 풀리니…"를 바로 답합니다(다른 LLM 오류처럼 안내 답으로 저장되고 다음 턴에 다시 보내지 않는다).
- **챗봇 전용 모델** (`SENSING_CHAT_LLM_MODEL`, 백엔드): 한도가 모델별이므로 챗봇만 다른 무료 모델로 돌리면 요약과 한도를 따로 씁니다
  (비우면 `SENSING_LLM_MODEL`과 같다). 쉬는 중 기록도 모델별이라 요약 모델이 쉬어도 챗봇은 답합니다.
- **운영 지표**: 관리자 → 운영 지표의 "LLM 사용량 (오늘)" 카드에 쉬는 중인 모델과 풀리는 시각, 요약 상한 대비 호출 수, 모델·용도별 호출·429·토큰이 보입니다.

## 설정

| 변수 | 기본값 | 어디서 | 설명 |
| --- | --- | --- | --- |
| `SENSING_LLM_PROVIDER` | `none` | 워커·백엔드 | `none`(레시피·요약은 하지 않고 챗봇은 "LLM이 설정되지 않았습니다"로 답) \| `anthropic`(Claude Messages API + tools, 워커는 선택 의존성 `llm-anthropic`) \| `openai`(OpenAI 호환 `chat/completions` + tools: Gemini·vLLM·Ollama 등) |
| `SENSING_LLM_MODEL` | `claude-opus-5-5` | 워커·백엔드 | 모델 이름. `openai`면 서버에 올린 모델 이름을 반드시 지정 (백엔드는 주소·모델이 없으면 미설정으로 둔다) |
| `SENSING_LLM_BASE_URL` | (없음) | 워커·백엔드 | `openai` 제공자의 주소. `{base_url}/chat/completions`로 POST한다 |
| `SENSING_LLM_API_KEY` | (없음) | 워커·백엔드 | `openai` 제공자의 키 (`Authorization: Bearer`). 비우면 헤더를 보내지 않는다 (로컬 vLLM·Ollama) |
| `ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL` | (없음) / `https://api.anthropic.com` | 워커·백엔드 | `anthropic` 제공자의 키(필수)와 주소(게이트웨이를 거치면 지정) |
| `SENSING_LLM_FALLBACKS` | `true` | 워커 | `anthropic`: 안전 분류기가 거절하면 서버가 다른 모델로 대신 답하게 한다 (`false`면 끈다) |
| `SENSING_LLM_TIMEOUT` | 워커 `120` / 백엔드 `60` | 워커·백엔드 | 호출 한 번의 제한 시간(초) |
| `SENSING_LLM_MAX_TOKENS` | `0` | 백엔드 | 답 최대 토큰 (0: `openai`는 서버 기본, `anthropic`은 4096) |
| `SENSING_CHAT_LLM_MODEL` | (없음) | 백엔드 | 챗봇만 쓰는 모델. 비우면 `SENSING_LLM_MODEL`. 무료 한도가 모델별이라 다른 무료 모델을 주면 워커 요약과 한도를 따로 쓴다 |
| `SENSING_LLM_QUOTA_TZ` | `America/Los_Angeles` | 워커·백엔드 (같은 값) | 무료 한도의 하루 기준 시간대 (Gemini는 태평양 시간 자정 = 한국 시간 오후 4~5시에 초기화). 하루 한도 429 뒤 쉬는 끝 시각과 장부 날짜에 쓴다 |
| `SENSING_ENRICH_DAILY_CALLS` | `300` | 워커 (백엔드는 표시만) | 요약·분류의 하루 LLM 호출 수 상한 (워커 여러 대 합산, DB `llm_usage` 기준). `0`이면 상한 없음. 백엔드는 운영 지표에 보이는 데만 쓴다 (오늘 워커 기록이 있으면 워커가 남긴 값) |

레시피 전용(`SENSING_LLM_MAX_SAMPLE_PAGES` 등)은 [01-registrations.md](01-registrations.md#설정), 요약 묶음(`SENSING_ENRICH_*`)은 [02-documents.md](02-documents.md#설정),
임베딩(`SENSING_EMBED_*`)은 [08-chatbot.md](08-chatbot.md#설정)에 있습니다.

```bash
# Claude
SENSING_LLM_PROVIDER=anthropic ANTHROPIC_API_KEY=sk-ant-... sensing-worker
# Ollama (로컬, 키 없음)
SENSING_LLM_PROVIDER=openai SENSING_LLM_BASE_URL=http://127.0.0.1:11434/v1 SENSING_LLM_MODEL=qwen2.5:14b sensing-worker
# vLLM (사내 서버)
SENSING_LLM_PROVIDER=openai SENSING_LLM_BASE_URL=http://vllm:8000/v1 SENSING_LLM_MODEL=Qwen/Qwen2.5-32B-Instruct SENSING_LLM_API_KEY=... sensing-worker
# Google Gemini 무료 등급 (OpenAI 호환 주소, 키는 aistudio.google.com)
SENSING_LLM_PROVIDER=openai SENSING_LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai SENSING_LLM_MODEL=gemini-3.5-flash-lite SENSING_LLM_API_KEY=... sensing-worker
```

로컬 개발에서는 키를 명령줄 대신 git에 올라가지 않는 `scripts/dev/worker.local.env`에 둡니다(`run-worker.ps1`이 읽는다, 예시:
[`worker.local.env.example`](../../scripts/dev/worker.local.env.example)). 백엔드는 `scripts/dev/run-backend.ps1`이 같은 파일의
`SENSING_LLM_*`·`SENSING_CHAT_*`·`SENSING_ENRICH_DAILY_CALLS`·`SENSING_EMBED_*`·`SENSING_NAVER_*`·`ANTHROPIC_*` 값만 읽습니다(이미 설정된 환경 변수가 우선, 키 값은 찍지 않고 `key=set`만 표시).
바꾼 뒤에는 워커·백엔드를 재시작합니다.

## API

LLM 전용 API는 없습니다. 상태는 다음으로 봅니다.

```bash
curl http://127.0.0.1:8080/api/v1/chat/status -H 'X-User: viewer'    # 챗봇 설정 여부·모델·문서 검색 방식
curl http://127.0.0.1:8080/api/v1/ops/overview -H 'X-User: admin'    # 운영 지표 (LLM 사용량 오늘 포함)
```

## 한계와 주의

- 무료 등급은 보낸 내용이 Google 모델 개선에 쓰일 수 있어(수집 기사는 공개 뉴스) 운영 전에 회사 규정을 확인해야 합니다.
- Claude API 키는 `sk-ant-api03-`로 시작하는 Console 키여야 합니다(다른 형식의 서비스 키는 401).
- 언론 기사와 AI 봇을 막은 사이트(`ai_restricted`)는 제목·분류만 LLM에 보냅니다(요약·본문 없음).
- 하루 한도는 모델별이라, 요약과 챗봇이 같은 모델이면 요약이 한도를 다 쓰지 않게 `SENSING_ENRICH_DAILY_CALLS`를 남겨 두거나 챗봇 전용 모델을 줍니다.
