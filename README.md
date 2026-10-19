# sensing-hub

사이트 주소만 등록하면 기사·보도자료를 **자동으로 찾아 주기적으로 수집**하고, 수집한 자료를 **챗봇에 자연어로 물어** 근거 링크와 함께 답을 받는 외부정보 수집 허브입니다. 현재 단계는 PoC 골격(M0)입니다.

- 사용자는 **URL 입력 한 번**만 합니다. 사전 점검 → 자동 탐색 → 품질 검사 → 기본값 적용 → 첫 수집은 시스템이 자동으로 처리합니다.
- 자동으로 판단할 수 없는 사이트는 사용자에게 다시 묻지 않고 **관리자 처리 대기(REVIEW)** 로 보냅니다.
- CAPTCHA·봇 차단·로그인 사이트는 **우회하지 않습니다.** 실패 사유로 분류하고 멈춥니다.

## 구조

```mermaid
flowchart LR
    U[사용자 / 관리자] -->|화면·챗봇| B[Spring Boot<br/>backend · Java 17]
    B -->|작업 등록·조회| DB[(PostgreSQL 16<br/>+ pgvector)]
    W[Python worker<br/>수집 하네스] -->|FOR UPDATE SKIP LOCKED| DB
    W -->|robots·간격·차단 시 중단| S[등록된 외부 사이트]
    B -->|챗봇 도구 호출·질의 임베딩| L[LLM API<br/>OpenAI 호환·Claude]
    W -->|요약·분류·레시피·조각 임베딩| L
```

- **backend (Java)**: 시스템 본체. 등록 API, 관리자 API, 스케줄러, 권한, 챗봇(도구 호출형, 하이브리드 문서 검색 — M5 1·2단계).
- **worker (Python)**: 교체 가능한 작업자. 사전 점검, 자동 탐색, 기사 추출, 요약·분류, 전문 검색 색인·이슈 묶기, 의미 검색 임베딩, (M4) 첨부 변환.
- 둘은 DB 작업 큐(`crawl_job`)로만 연결됩니다. 메시지 브로커는 쓰지 않습니다.

## 사이트 등록 흐름

```mermaid
flowchart TD
    A[URL 입력] --> B[도메인 정규화 · 중복 · 권한]
    B -->|이미 등록| X1[이미 수집 중 안내]
    B -->|VIEWER| X2[관리자 승인 대기]
    B --> C[ONBOARD 작업 생성]
    C --> D[사전 점검: robots · 봇 차단 · 로그인]
    D -->|막힘| X3[BLOCKED · 등록 불가 알림]
    D --> E[자동 탐색: RSS → 사이트맵 → 링크 패턴]
    E -->|못 찾음| X4[REVIEW · 관리자 큐]
    E --> F[품질 검사: 샘플 5건 중 4건]
    F -->|불합격| X4
    F --> G[기본값 적용 · 설정 v1 · ACTIVE]
    G --> H[첫 수집 → 완료 알림 → 주기 수집]
```

소스 상태: `PENDING → ACTIVE ⇄ PAUSED`, `ACTIVE → BROKEN(0건·급감) → 수정 → ACTIVE`, `ACTIVE → BLOCKED(403·429·챌린지) → 관리자 재점검 → ACTIVE 또는 EXCLUDED`

자동 탐색이 못 찾은 홈이 다른 언론사로 가는 링크 모음(뉴스스탠드 같은 포털)이면 관리자 큐로 보내지 않고, 그 페이지가 가리키는 언론사들을 같은 요청자 이름으로
대신 등록한다 (모음 페이지 소스는 `EXCLUDED/PORTAL_EXPANDED`, 언론사들은 묶음 `portal:<소스 id>`로 진행을 본다).

## 빠른 시작

### Docker Compose

```bash
docker compose up --build
```

### 로컬 개발

필요: JDK 17, Python 3.11+, PostgreSQL 16 + pgvector

```bash
# DB
createdb sensing   # 사용자/비밀번호 sensing/sensing 기준 (application.yml 참고)

# backend (Flyway가 스키마를 만든다)
cd backend && ./gradlew bootRun

# worker
cd worker && pip install -e ".[dev,extract,llm-anthropic,nlp]"
SENSING_DB_DSN=postgresql://sensing:sensing@localhost:5432/sensing sensing-worker
```

사내망처럼 프록시로만 외부에 나가는 환경이면 워커에 `HTTPS_PROXY`를 지정합니다.

### 화면

브라우저에서 http://localhost:8080 (상단에서 사용자 선택: admin · registrar · viewer — PoC 인증이라 선택한 사용자가 `X-User` 헤더로 나간다).
빌드 도구·CDN 없이 `backend/src/main/resources/static/`의 HTML·CSS·JS만으로 동작한다.

| 화면 | 내용 |
| --- | --- |
| 등록·내 요청 | URL 한 건 등록, 여러 개 등록(한 줄에 하나, 묶음 진행을 5초마다 확인), 내 요청 목록(상태·단계·결과, 진행 중이면 자동 갱신) |
| 문서 | 기간(오늘·3일·7일)·토픽 칩(건수), 검색어(전문 검색)·소스 필터, 목록(제목·출처·발행·토픽·요약·이슈 링크), 펼쳐 보는 본문 일부·첨부 |
| 이슈 | 같은 사건을 다룬 기사 묶음. 기간·언론사 수(2곳+/3곳+/5곳+) 칩, 검색, 카드(대표 제목·언론사 N곳·기사 M건·기간·핵심 낱말), 펼쳐 보는 소속 기사 |
| 거래처 | 회사 목록(사업자번호·신호 수·HIGH·최근 신호·감시 상태), 등록, CSV 붙여넣기 import, 회사 상세(최근 신호·연결 문서), 전체 신호(종류·심각도·기간) |
| 키워드 감시 | 목록, 생성, 키워드·제외어 수정(설정 새 버전) |
| 다이제스트 | 구독 목록, 생성(채널·대상·시각·필터 체크), 미리보기(HTML), 지금 보내기, 보낸 기록 |
| 챗봇 | 내 대화 목록(지우기), 질문 입력(Enter 보내기·Shift+Enter 줄바꿈, 예시 질문), "생각 중" 표시, 답의 `[n]` → 근거 목록(원문 링크·이슈 화면), 사용한 도구와 화면 바로 가기. 아래 "챗봇" |
| 관리자 · 처리 대기 큐 | 종류별 건수, 항목별 사유·메시지·대기 시간과 가능한 조치만(승인 / 반려 사유 / API 키 등록 / 재점검 / 다시 탐색 / 일시정지 / 제외) |
| 관리자 · 소스 | 상태별 건수 필터, 건강 상태 탭(지연 소스 강조), 소스 상세(작업·오류·설정 버전 활성화·수집 주기 잠금) |
| 관리자 · 운영 지표 | `/ops/overview`를 카드로(소스 상태·큐 적체·워커 부족 경고·24시간 결과·수집량 상위·실패 코드), 30초마다 갱신 |
| 관리자 · API 키 | 제공자(dart · naver_news · data_go_kr)·키·메모 등록, 다시 진행된 소스 수 표시, 목록(비밀 없음)·삭제 |
| 관리자 · 케이던시아 연동 | 연결 주소·목업 표시·마지막 동기화 결과(공급사·생성·갱신·보관·그대로)·이벤트 전달 대기/보낸 수 카드, 지금 동기화 / 지금 전달, 동기화 실행 기록, (데모 전용) 케이던시아가 받은 이벤트 |

### 워커 환경 변수 (접속)

| 변수 | 기본값 | 설명 |
| --- | --- | --- |
| `SENSING_TLS_COMPAT` | `true` | TLS 핸드셰이크가 실패한 호스트를 호환 컨텍스트(`DEFAULT:@SECLEVEL=1`, TLS 1.2 이상, legacy renegotiation 허용, 인증서 검증은 유지)로 한 번 더 시도하고 성공하면 그 호스트는 계속 호환 설정으로 접속한다. RSA 키 교환만 지원하는 공공기관 사이트(예: 용인시)에 필요. 인증서 검증 실패는 재시도하지 않는다 |

### 워커 환경 변수 (LLM 레시피)

사이트를 등록하면 워커가 홈과 샘플 기사 페이지를 LLM에 보여 주고, LLM이 허브·기사 URL 서명·추출 셀렉터를 JSON 레시피로 써냅니다.
레시피는 스키마 검증 → 셀렉터 문법·서명 형식 검사 → 허브에서 서명에 맞는 링크 3개 이상 → 기존 샘플 5건 품질 검사를 모두
통과해야 설정 버전(`recipe_source: "llm"`, `selectors`)으로 저장되고, 아니면 휴리스틱 탐색 결과를 씁니다.
LLM 장애는 경고만 남기고 휴리스틱으로 진행하므로 LLM이 멈춰도 등록은 실패하지 않습니다. 기본값은 LLM을 쓰지 않는 것입니다.

| 변수 | 기본값 | 설명 |
| --- | --- | --- |
| `SENSING_LLM_PROVIDER` | `none` | `none`(휴리스틱만) \| `anthropic`(Claude API, 선택 의존성 `llm-anthropic`) \| `openai`(OpenAI 호환 `chat/completions`: vLLM·Ollama 등 사내 반입 모델) |
| `SENSING_LLM_MODEL` | `claude-opus-5-5` | 모델 이름. `openai`면 서버에 올린 모델 이름을 반드시 지정 |
| `SENSING_LLM_BASE_URL` | (없음) | `openai` 제공자의 주소. `{base_url}/chat/completions`로 POST한다 |
| `SENSING_LLM_API_KEY` | (없음) | `openai` 제공자의 키 (`Authorization: Bearer`). 비우면 헤더를 보내지 않는다 (로컬 vLLM·Ollama) |
| `ANTHROPIC_API_KEY` | (없음) | `anthropic` 제공자의 키. 게이트웨이를 거치면 `ANTHROPIC_BASE_URL`도 지정 |
| `SENSING_LLM_FALLBACKS` | `true` | `anthropic`: 안전 분류기가 거절하면 서버가 다른 모델로 대신 답하게 한다 (`false`면 끈다) |
| `SENSING_LLM_MAX_SAMPLE_PAGES` / `SENSING_LLM_PAGE_CHARS` / `SENSING_LLM_TIMEOUT` | `5` / `30000` / `120` | LLM에 보여 줄 샘플 페이지 수, 페이지당 글자 수 상한(script·style·svg 제거 후), 요청 제한 시간(초) |
| `SENSING_ENRICH_ENABLED` | `true` | 수집 시점 요약·분류(보강). 큐에 작업이 없을 때 LLM이 설정돼 있으면 한 묶음씩 돌린다 (`none`이면 하지 않는다) |
| `SENSING_ENRICH_BATCH` | `10` | 한 번의 LLM 호출에 묶는 문서 수 |
| `SENSING_ENRICH_BODY_CHARS` | `4000` | 문서당 LLM에 보여 줄 본문 글자 수 상한 (언론 기사는 본문을 보여 주지 않는다) |

```bash
# Claude
SENSING_LLM_PROVIDER=anthropic ANTHROPIC_API_KEY=sk-ant-... sensing-worker
# Ollama (로컬, 키 없음)
SENSING_LLM_PROVIDER=openai SENSING_LLM_BASE_URL=http://localhost:11434/v1 SENSING_LLM_MODEL=qwen2.5:14b sensing-worker
# vLLM (사내 서버)
SENSING_LLM_PROVIDER=openai SENSING_LLM_BASE_URL=http://vllm:8000/v1 SENSING_LLM_MODEL=Qwen/Qwen2.5-32B-Instruct SENSING_LLM_API_KEY=... sensing-worker
# Google Gemini 무료 등급 (OpenAI 호환 주소, 키는 aistudio.google.com)
SENSING_LLM_PROVIDER=openai SENSING_LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai SENSING_LLM_MODEL=gemini-3.5-flash-lite SENSING_LLM_API_KEY=... sensing-worker
```

로컬 개발에서는 키를 명령줄 대신 git에 올라가지 않는 `scripts/dev/worker.local.env`에 둡니다
(`run-worker.ps1`이 읽는다, 예시: [`worker.local.env.example`](scripts/dev/worker.local.env.example)). 바꾼 뒤에는 워커를 재시작합니다.

**실측 (2026-10-19, Gemini 무료 등급)** — 코드 변경 없이 `openai` 제공자로 붙었고, 쌓여 있던 문서 7,967건을 모두 분류·요약했습니다
(실패 0, 비용 0). 언론 기사는 제목만 보내므로 30건 묶음 한 번이 입력 약 3천 토큰입니다. 공공기관 문서 요약은 두 문장으로 수치까지
정확했고, 분류는 정치 발언 기사를 "사건·사고"·"산업동향"에 넣는 식의 오분류가 가끔 있습니다.

| 모델 | 무료 한도 | 판단 |
| --- | --- | --- |
| `gemini-3.5-flash-lite` | 하루 약 500회 (모델별) | 권장. `SENSING_ENRICH_BATCH=30`이면 하루 약 1만5천 건 |
| `gemini-3.8-flash` | 하루 20회 | 분류가 조금 더 정확하지만 밀린 문서를 처리하면 곧 HTTP 429 (워커는 아무것도 쓰지 않고 다음에 다시) |
| `gemini-2.5-flash` 등 옛 모델 | — | 신규 사용자에게 404. 쓸 수 있는 모델은 `{BASE_URL}/models`로 확인 |

무료 등급은 보낸 내용이 Google 모델 개선에 쓰일 수 있어(수집 기사는 공개 뉴스) 운영 전에 회사 규정을 확인해야 합니다.
Claude API 키는 `sk-ant-api03-`로 시작하는 Console 키여야 합니다(다른 형식의 서비스 키는 401).

JSON 형식은 모델에 맡기지 않습니다. 제공자가 스키마 강제(`json_schema`)를 지원하면 힌트로 쓰되, 응답은 항상 우리 쪽에서 JSON을 꺼내
스키마를 검증하고, 실패하면 오류 메시지를 붙여 한 번만 다시 요청합니다. OpenAI 호환 서버가 `response_format`을 400으로 거부하면
`json_object` → 없음 순으로 낮춰 다시 보냅니다. 측정 도구(`sensing-discovery-eval`)도 같은 변수를 읽고 결과 표에 레시피 출처를 표시합니다.

### 공개 API 소스와 자격 증명 (M2)

언론 보도·공시처럼 공개 API가 있는 제공처는 크롤링하지 않습니다. 워커의 API 카탈로그(`apis/catalog.py`, 첫 항목 DART)에 있는
도메인을 등록하면 사전 점검 없이 **"API 키 발급 필요"** 로 관리자 큐(`kind=API_KEY`)에 들어가고, 관리자가 키를 넣으면 시험 호출로
검증한 뒤 `method: API` 설정으로 첫 수집과 주기 수집(6시간)을 자동으로 시작합니다. 키 발급은 사람이 합니다 (자동 가입·약관 동의 대행 없음).
키는 pgcrypto로 암호화해 `api_credential`에만 두고 응답·로그·오류 메시지·증거 파일 어디에도 남기지 않습니다.

| 변수 | 기본값 | 설명 |
| --- | --- | --- |
| `SENSING_SECRET_KEY` | (없음) | 백엔드·워커 공통. API 키를 암호화·복호화하는 대칭키(`pgp_sym_encrypt`). 비우면 백엔드 자격 증명 API는 503, 워커는 API 소스를 관리자 큐로 보낸다 |
| `SENSING_MAX_API_ITEMS` | `500` | 워커. API 소스 실행 1회당 받을 항목 수 상한 |
| `SENSING_NAVER_DAILY_LIMIT` | `20000` | 워커. 네이버 검색 API 일 호출 상한(공식 25,000). 지금은 호출 수를 로그에 남기는 기준이고 정확한 쿼터 추적은 후속 |

### 키워드 감시 (M2)

현업(대외협력·커뮤니케이션)은 사이트가 아니라 키워드(자사·경쟁사·이슈) 단위로 보도를 봅니다. 키워드 묶음을 등록하면 워커가 **등록된 언론사에서
수집한 기사**마다 모든 감시의 키워드를 대조해(제목·본문, 제외어는 제목 기준) 어느 감시의 어느 검색어에 걸렸는지 `document_watch`에 남깁니다
(다이제스트가 이 표를 읽습니다). 이것이 기본 동작인 **수집 기사 대조 모드**이고, 감시는 만들자마자 `ACTIVE`(화면 "수집 기사 대조 중")가 되지만
스스로 수집하지 않으므로 `next_run_at`이 비어 있어 스케줄러가 작업을 넣지 않고, 관리자 키 큐에도 쌓이지 않습니다. 새로 수집되는 기사부터 대조하며,
이미 쌓인 기사는 `python -m sensing_worker.backfill_watch --days 30`으로 한 번 대조할 수 있습니다. 커버리지는 등록 언론사 수가 정합니다.
네이버 뉴스 검색 API는 2026-07-31부터 신규 발급이 중단돼 보통 키가 없습니다. 키(INVALID가 아닌 `naver_news`)가 있으면 감시를 만들 때 네이버 검색도
함께 붙여(화면 "네이버 검색 함께") 60분(회사 감시 360분)마다 받아 옵니다. 키를 나중에 넣으면 대조 모드 감시에 네이버 검색을 붙이고(`attachedWatches`),
키를 지우면 다시 대조 모드로 되돌립니다. 감시 하나는 `source` 한 행(`kind = WATCH`, `watch_name`)이고 키워드는 설정 버전
config(`{"method":"API","provider":"naver_news","naver":false,"keywords":[...],"exclude":[...]}`, `naver`가 없으면 V13 이전 감시로 네이버 모드)에
두므로, 키워드 수정은 설정 새 버전 활성화이고 롤백·일시정지·제외 같은 소스 조치를 그대로 씁니다. V13 마이그레이션이 키 없이 멈춰 있던
감시(REVIEW `API_KEY_REQUIRED`/`API_KEY_INVALID`)를 대조 모드로 옮겼습니다. 네이버 뉴스 웹은 robots.txt가 전체 차단이라 크롤링하지 않으며,
`news.naver.com`을 사이트처럼 등록하면 "키워드 감시로 등록하세요"(`KEYWORDS_REQUIRED`)로 안내합니다. 네이버 키가 있다면 `client_id:client_secret`
한 문자열로 `/api/v1/api-credentials`에 넣고(provider `naver_news`), 언론 기사이므로 제목·링크·발행일만 저장합니다.

### 수집 시점 요약·분류 (M2)

워커는 큐에 작업이 없을 때 보강되지 않은 문서를 최신순으로 한 묶음(기본 10건) 가져와 LLM에 한 번에 보내고, 문서마다 분류
토픽(정책·규제, 입찰·공고, 지원사업, 자사, 경쟁사, 산업동향, 인사·조직, 사건·사고, 기타)과 자유 태그(최대 5개)를 `document_enrichment`에
둡니다. 공공기관·기타 문서는 본문(앞 4,000자)으로 2문장 이내 요약도 만들지만, 언론 기사는 제목만으로 분류하고 요약은 저장하지 않습니다.
수집과 분리돼 있어 LLM 장애는 수집을 막지 않고(아무것도 쓰지 않고 다음 루프에 다시), 응답 검증 실패는 `FAILED`로 남겨 24시간 뒤
다시 시도합니다. 본문이 바뀌어 문서 버전이 오르면 다시 보강 대상이 되고, 워커 여러 대는 `FOR UPDATE SKIP LOCKED`로 겹치지 않습니다.
결과는 `/api/v1/documents`(VIEWER 이상)로 읽으며 다이제스트(M2-4)의 재료입니다.

### 전문 검색 색인과 이슈 묶기 (M5 기반)

| 변수 | 기본값 | 설명 |
| --- | --- | --- |
| `SENSING_INDEX_ENABLED` / `SENSING_INDEX_BATCH` | `true` / `200` | 큐가 빌 때마다 색인·이슈 묶기를 한 묶음씩 (LLM 설정과 무관) |
| `SENSING_TOKENIZER` | `auto` | `regex`면 Kiwi가 설치돼 있어도 정규식 분석기를 쓴다 (Kiwi: `pip install -e ".[nlp]"`) |
| `SENSING_ISSUE_WINDOW_HOURS` | `72` | 이 시간 안의 이슈와만 비교하고, 마지막 기사가 이보다 오래된 이슈는 닫는다 |
| `SENSING_ISSUE_THRESHOLD` / `SENSING_ISSUE_MIN_SHARED` / `SENSING_ISSUE_PAIR_THRESHOLD` | `0.4` / `2` / `0.66` | 붙이는 기준: 제목 낱말과 이슈 서명의 겹침 계수, 겹친 낱말 수, 2개만 겹쳤을 때의 겹침 계수 |

이미 쌓인 문서는 유휴 틱마다 200건씩 색인되며, 한 번에 끝내려면 `sensing-index-backfill [--limit N]` (2,998건에 약 16초).

### 일간 다이제스트 알림 (M2)

현업은 아침에 정리된 보고를 받아 윗선에 전달합니다. 구독(`/api/v1/digests`: 어떤 감시·토픽·소스를 몇 시에 어디로)을 등록하면
백엔드 스케줄러가 5분마다 KST 기준 현재 시가 `hourKst`이고 오늘 아직 보내지 않은 구독을 찾아, **지난 다이제스트 이후(`last_sent_at`,
처음은 24시간 전) 새로 수집된(`collected_at`) 문서**를 ① 감시별(검색어별) → ② 토픽별(정책·규제 → … → 기타) → ③ 분류 전 순으로 묶어
메일(HTML) 또는 웹훅(`{"text": ...}`, Slack 수신 웹훅·Mattermost 형식)으로 보냅니다. 항목은 "제목 — 출처 도메인 · 발행일 · 링크"이고
요약이 있으면 다음 줄에 붙습니다 (언론 기사는 요약 없음). 필터 `{"watchIds":[],"sourceIds":[],"topics":[]}`는 셋 중 하나라도 맞으면
포함(합집합), 모두 비면 전체이며 상한은 200건입니다. 0건이면 보내지 않고 `EMPTY`로만 기록하고, 전송 실패는 `FAILED`로 남긴 뒤 같은
시간대의 다음 틱에 다시 시도합니다. 보낸 본문은 `digest_run.body_text`에 남아 `/api/v1/digests/{id}/runs`로 다시 볼 수 있습니다.
웹훅 URL은 사이트 등록과 같은 검사(http(s) 절대 주소, 내부망 거부)를 거칩니다.

| 변수 | 기본값 | 설명 |
| --- | --- | --- |
| `SENSING_MAIL_HOST` / `SENSING_MAIL_PORT` | (없음) / `587` | SMTP 서버. 호스트가 비어 있으면 EMAIL 구독은 `FAILED`("메일 서버가 설정되지 않았습니다")로 남고 웹훅 구독만 보낸다 |
| `SENSING_MAIL_USERNAME` / `SENSING_MAIL_PASSWORD` | (없음) | SMTP 계정. 비밀번호는 로그·오류·응답 어디에도 남기지 않는다 |
| `SENSING_MAIL_FROM` | (없음) | 보내는 주소. 비우면 `SENSING_MAIL_USERNAME`, 그것도 비면 메일 서버가 정한다 |
| `SENSING_MAIL_AUTH` / `SENSING_MAIL_STARTTLS` | `true` / `true` | 인증 없는 사내 릴레이(25번 포트)면 둘 다 `false` |
| `SENSING_DIGEST_ENABLED` / `SENSING_DIGEST_TICK_MS` | `true` / `300000` | 다이제스트 스케줄러 켜기·확인 간격 |

### 케이던시아 연동 환경 변수 (M3, 목업)

| 변수 | 기본값 | 설명 |
| --- | --- | --- |
| `SENSING_CAIDENTIA_BASE_URL` | `http://127.0.0.1:<port>/mock/caidentia/api` | 케이던시아 API 주소. **실제 시스템이 나오면 이것만 바꾼다** (기본은 이 서버 안의 가짜 케이던시아) |
| `SENSING_CAIDENTIA_API_KEY` | `mock-key` | 헤더 `X-Caidentia-Key`로 보내는 키. 가짜 서버도 같은 값을 요구한다 |
| `SENSING_CAIDENTIA_ENABLED` | `true` | 스케줄러(동기화·전달) 켜기. 꺼도 화면의 "지금 동기화 / 지금 전달"은 된다 |
| `SENSING_CAIDENTIA_SYNC_CRON` | `0 0 6 * * *` | 공급사 마스터 동기화 시각 (KST) |
| `SENSING_CAIDENTIA_PUSH_ENABLED` / `SENSING_CAIDENTIA_PUSH_TICK_MS` | `true` / `300000` | HIGH·MEDIUM 회사 신호를 리스크 이벤트로 전달 (5분마다) |
| `SENSING_CAIDENTIA_MOCK_ENABLED` | `true` | 가짜 케이던시아 서버(`/mock/caidentia/api`) 띄우기. 실제 시스템을 보게 되면 끈다 |

### 회사(거래처) 마스터와 공급사 리스크 신호 (M3)

현업이 구매 SaaS 케이던시아를 함께 쓰므로, 문서 중심 위에 **회사 중심**("어느 거래처에 무슨 일")을 한 층 얹습니다. 회사(`company`: 이름·별칭·
사업자등록번호·DART 고유번호·외부 ID·태그)를 등록하면 ① 이름+별칭을 키워드로 한 키워드 감시(`회사:<이름>`, 수집 기사 대조. 네이버 키가 있으면 네이버 검색 360분)가 생겨 걸린 기사를
`document_company`(WATCH)로 연결하고, ② 보강 태그(고유명사)가 회사 이름·별칭과 **정확히**(대소문자·공백 무시) 일치하면 TAG로 연결하며
(부분 일치·유사도 매칭은 오탐 때문에 하지 않습니다), ③ 연결된 기사의 토픽이 사건·사고·인사·조직이면 `company_signal`(NEWS, MEDIUM)을 남깁니다.
④ 첫 회사를 만들 때 **점검 소스**(`source.kind = CHECK`, 설정의 `check`가 점검 종류) 네 개가 생겨 모든 회사를 주기적으로 외부 API로 확인합니다.
`nts_status`(하루 1회)는 사업자번호가 있는 회사의 국세청 사업자등록 상태를 확인해 폐업·휴업(HIGH)·미등록(MEDIUM)·과세유형 변경(LOW)을
`company_signal`(BIZ_STATUS)로 남깁니다. `dart_corp_map`(주 1회)은 DART 고유번호 전체 목록(corpCode.xml)을 받아 corp_code가 빈 회사의 이름·별칭과
공백·"(주)"·"주식회사"·"㈜"를 지우고 **정확히** 일치하는 항목이 하나(여럿이면 상장사 하나)일 때만 고유번호를 채웁니다(모호하면 로그만).
`dart_disclosure`(6시간)는 고유번호가 있는 회사마다 최근 3일 공시를 받아 문서(`document_company` API)로 저장하고, 보고서명에 든 말로
HIGH(회생절차·파산·영업정지·상장폐지·감사의견 거절·부적정·자본잠식·불성실공시·횡령·배임)·MEDIUM(소송·최대주주 변경·대표이사 변경·유상증자·합병·분할·
영업양수도·거래정지)·LOW(그 밖의 주요사항보고서·정정공시) DISCLOSURE 신호를 남깁니다(일반 정기·수시 공시는 문서만). `g2b_debarment`(하루 1회)는
조달청 나라장터 부정당업자(입찰참가자격 제한) 조회로 제재가 있으면 DEBARMENT HIGH 신호를 남깁니다. 같은 신호는 fingerprint(상태 코드+폐업일,
공시 접수번호, 제재 시작일+기관)로 중복 기록하지 않습니다. 키가 없으면(공공데이터포털 `data_go_kr`: 국세청 사업자등록 상태조회·나라장터 사용자정보
서비스 활용신청 필요, DART `dart`) 그 점검 소스는 다른 API 소스처럼 관리자 큐 `API_KEY`에서 기다립니다. 다이제스트는 기간 안의 회사 신호를 맨 앞
"거래처 신호" 절로 넣고(HIGH 먼저), 신호가 있으면 문서가 0건이어도 보냅니다. 삭제는 보관(ARCHIVED)이며 그 감시는 제외됩니다.

### 챗봇 (M5 1·2단계, 2026-10-19)

화면 "챗봇"(`#/chat`)에서 물어보거나 요청하면 LLM이 말을 **도구 호출**로 바꾸고, 조회·등록·권한·중복 판정은 화면과 같은 서비스가 합니다
(LLM은 결과를 말로 풀 뿐). 답의 `[1]` 같은 번호는 아래 근거 목록의 원문 링크(이슈는 이슈 화면)로 이어집니다. 대화·도구 호출·도구 결과·근거는
모두 DB(`chat_session`·`chat_message`·`chat_citation`, V14)에 남고, 대화는 **만든 사람만** 봅니다 (관리자도 남의 대화는 404).
문서 검색은 2단계부터 낱말 일치와 의미 유사도를 합친 하이브리드입니다 (아래 "의미 검색").

| 도구 | 하는 일 | 부르는 코드 |
| --- | --- | --- |
| `search_documents` | 하이브리드 검색(낱말 모두 포함 + 뜻이 가까운 문서, RRF 관련도순)·기간(`days`·`since`)·토픽·감시로 문서 찾기, 최대 20건. 문서마다 `match`(낱말·의미·낱말+의미). 임베딩이 없으면 전문 검색(최신순) | `HybridSearch.search` |
| `list_issues` | 최근 주요 이슈 (언론사 많은 순, 기본 최근 3일·2곳 이상) | `IssueService.list` |
| `list_watches` | 내 키워드 감시 (관리자는 전체) | `WatchService.list` |
| `create_watch` | 키워드 감시 만들기 — 화면과 같은 규칙 (VIEWER는 승인 대기), 등록 요청 채널 `CHAT` | `WatchService.create(..., Channel.CHAT)` |
| `register_source` | 사이트 주소 등록 — 중복·권한 판정, ONBOARD 작업 trigger `CHAT` | `RegistrationService.register(..., Channel.CHAT)` |
| `get_source_status` | 등록·감시 요청 진행 상태와 소스 상태. API 키 대기면 발급 안내(등록 메시지)와 관리자 'API 키' 화면 링크 | `RegistrationService` + 소스 상태 |

- **사실은 도구 결과에서만**: 시스템 프롬프트가 도구 결과에 없는 기사·숫자·상태를 지어내지 말고, 등록·수집 상태는 결과 문장 그대로 전하게 합니다.
- **언론 기사는 제목·링크로만**: 언론 기사와 AI 봇을 막은 사이트(`ai_restricted`)의 문서는 제목·분류만 LLM에 보내고, 기사 내용을 요약·추측하지 않게 합니다.
  공공기관·기타 문서는 저장된 요약(300자까지)을 보냅니다. URL은 LLM에 보내지 않고 화면이 번호에 붙입니다 (LLM이 주소를 지어낼 수 없다).
- **근거**: 답이 쓴 `[n]`만 `chat_citation`에 제목·링크 스냅숏으로 남깁니다 (번호를 안 쓰면 그 턴 도구 결과를 근거로). 도구 없이 답한 후속 질문
  ("2번은 어느 언론사야?")의 번호는 앞 답의 근거를 이어 씁니다. 번호는 답마다 1부터입니다.
- **한 턴의 도구 왕복은 4번까지**(`sensing.chat.max-tool-rounds`), 넘으면 도구 없이(`tool_choice: none`) 지금까지의 결과로 답하게 합니다.
  LLM에는 지난 6턴(`sensing.chat.history-turns`)을 도구 호출·결과와 함께 다시 보여 줍니다 (Gemini 3의 `thought_signature`도 그대로 돌려준다).
- **LLM 오류는 대화를 깨지 않습니다**: 429(무료 한도 — 하루 한도인지, 몇 초 뒤 다시 할지 구분)·5xx(한 번 재시도)·시간 초과·키 오류·미설정은 알기 쉬운
  문장의 답(주의 색)으로 저장되고, 그 턴은 다음 질문 때 LLM에 다시 보내지 않습니다. 같은 대화에 답을 기다리는 중 또 보내면 409.
- **API 키는 채팅으로 받지 않습니다**: 키가 필요하면 발급처와 관리자 'API 키' 화면을 안내만 합니다. 채팅에 붙여 넣은 키처럼 보이는 값(`sk-`·`AIza` 접두어,
  16진수 32자 이상, `id:secret`, 32자 이상 영숫자 덩어리)은 저장·LLM 전송 전에 가립니다 (대화가 영구 저장되고 외부 LLM에도 가므로 — docs/decisions.md).

백엔드 환경 변수 — 워커와 같은 이름이라 한 곳에서 관리합니다. 로컬은 `scripts/dev/run-backend.ps1`이 `scripts/dev/worker.local.env`의
`SENSING_LLM_*`·`ANTHROPIC_*` 값만 읽습니다 (이미 설정된 환경 변수가 우선, 키 값은 찍지 않고 `key=set`만 표시).

| 변수 | 기본값 | 설명 |
| --- | --- | --- |
| `SENSING_LLM_PROVIDER` | `none` | `none`(챗봇은 "LLM이 설정되지 않았습니다"로 답) \| `openai`(OpenAI 호환 `chat/completions` + tools: Gemini·vLLM·Ollama) \| `anthropic`(Messages API + tools) |
| `SENSING_LLM_BASE_URL` / `SENSING_LLM_MODEL` / `SENSING_LLM_API_KEY` | (없음) | `openai`: 주소·모델 필수(없으면 미설정으로 둔다), 키는 `Authorization: Bearer` |
| `ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL` | (없음) / `https://api.anthropic.com` | `anthropic`: 키 필수, 모델은 `SENSING_LLM_MODEL`(기본 `claude-opus-5-5`) |
| `SENSING_LLM_TIMEOUT` | `60` | 호출 한 번의 제한 시간(초) |
| `SENSING_LLM_MAX_TOKENS` | `0` | 답 최대 토큰 (0: `openai`는 서버 기본, `anthropic`은 4096) |

추가 의존성 없이 `java.net.http`로 부릅니다 (`io.sensinghub.llm`). 화면은 `GET /api/v1/chat/status`로 설정 여부와 모델, 문서 검색 방식
(`search`: "낱말+의미 (모델/차원)" 또는 "낱말")을 보여 줍니다.

**실측 (2026-10-19, Gemini `gemini-3.5-flash-lite` 무료 등급, 운영 DB를 복사한 sensing_test, LLM 호출 17번)** — "오늘 많이 보도된 이슈"(이슈 10개, 제목과
언론사 수·기사 수 근거), "최근 3일 반도체 기사"(10건, 제목·언론사·날짜·분류), "1번 기사 내용 요약해 줘"(→ 저작권 원칙상 제목·언론사·날짜·분류만
안내, 내용은 추측하지 않음), "2번은 어느 언론사?"(도구 없이 앞 답 근거로), "dart.fss.or.kr 수집해 줘"(등록 접수 문장 그대로), 진행 상태, 감시 목록,
붙여 넣은 DART 키(가린 뒤 'API 키' 화면 안내)가 모두 기대대로 동작했습니다. 한 턴(LLM 호출 1~2번)의 입력은 약 2천~8천 토큰(도구 정의·지난 대화·검색
결과 포함)이고, 서버에서 답까지 1~3초였습니다. Gemini는 도구 호출마다 `extra_content.google.thought_signature`를 붙였고(Google 안내상 Gemini 3는
이것을 돌려주지 않으면 도구 결과를 담은 요청을 거절한다), `ToolCall.providerData`로 보관해 돌려준 후속 턴이 모두 성공했습니다. 처음엔 도구 이름
("`get_source_status` 도구로 확인")을 말하거나 등록 답에 엉뚱한 `[1]`을 붙여 프롬프트·도구 안내문을 보강했습니다.

### 의미 검색 (M5 챗봇 2단계, 2026-10-19)

전문 검색은 검색어 낱말이 제목·본문에 그대로 있어야 찾습니다. "배드민턴"으로는 "안세영, 덴마크오픈 우승"을, "가정폭력 피해자 보호 장치"로는
"스마트워치 받았지만…전남편에 피습"을 못 찾습니다. 그래서 문서 조각을 **임베딩**(뜻을 숫자 벡터로)해 두고 챗봇의 `search_documents`가
**낱말 일치와 의미 유사도를 함께** 봅니다.

1. **워커**(`embed.py`, 큐가 빌 때 색인 다음 단계): 전문 검색 색인이 만든 조각(`chunk`)을 OpenAI 호환 `/embeddings`로 보내 `chunk.embedding`
   (pgvector `VECTOR(1024)`)을 채우고, 어떤 모델의 벡터인지 `chunk.embed_model`('모델/차원', V15)에 남깁니다. **언론 기사는 제목만**(색인 조각이 원래
   제목 하나), **공공기관·기타 문서는 제목 + 본문 조각**(본문 조각 앞에 제목을 붙여 보냄)이고, AI 봇을 막은 사이트(`ai_restricted`)는 본문 조각을
   외부로 보내지 않고 제목만 임베딩합니다. 호출이 실패하면 아무것도 쓰지 않고 쉬었다가(60초부터 두 배씩, 최대 30분) 다시 하며, 모델을 바꾸면
   모든 조각이 다시 대상이 됩니다. 문서가 다시 색인되면(본문 변경) 새 조각만 다시 임베딩합니다.
2. **백엔드**(`HybridSearch`): 질의를 같은 설정으로 임베딩해 `embed_model`이 같은 조각과 코사인 유사도를 재고(문서마다 가장 가까운 조각,
   하한 0.58), 전문 검색 결과(발행일 최신순)와 **RRF**(문서마다 두 목록의 1/(60+순위) 합)로 합쳐 관련도순으로 줍니다. 두 목록에 다 있는 문서가 맨 앞,
   낱말만·뜻만 맞는 문서가 뒤를 잇습니다. 기간·토픽·감시 조건은 두 쪽에 똑같이 걸립니다.
3. **챗봇**: 도구 결과의 문서마다 `match`(낱말·의미·낱말+의미)를 붙이고, '의미'뿐인 문서가 있으면 "질문과 실제로 관련 있는 것만 쓰라"는 안내를 함께
   보냅니다. LLM에 보내는 내용(근거 번호·제목·분류, 언론 기사 요약 없음, URL 없음)은 1단계와 같습니다.

**임베딩이 없으면 그대로 전문 검색**입니다: 미설정(`SENSING_EMBED_PROVIDER=none`, 기본), 그 모델로 임베딩된 조각이 아직 없음(질의를 임베딩하지 않아
호출 한도를 아낌), 질의 임베딩 실패(429·5xx·시간 초과·차원 불일치 — 그 뒤 60초는 부르지 않음) 모두 1단계와 같은 전문 검색 결과(최신순)를 줍니다.
화면의 문서 검색(`/api/v1/documents?q=`)은 지금처럼 전문 검색만 합니다.

| 변수 (워커·백엔드 공통) | 기본값 | 설명 |
| --- | --- | --- |
| `SENSING_EMBED_PROVIDER` | `none` | `none`(전문 검색만) \| `openai`(OpenAI 호환 `/embeddings`: Gemini OpenAI 호환 주소·OpenAI·vLLM·Ollama) |
| `SENSING_EMBED_MODEL` | (없음) | 필수. 예: `gemini-embedding-001`(실측), `text-embedding-3-small`, `bge-m3` |
| `SENSING_EMBED_BASE_URL` / `SENSING_EMBED_API_KEY` | (없음) | 주소를 비우면 `SENSING_LLM_BASE_URL`·`SENSING_LLM_API_KEY`를 함께 쓴다 (Gemini처럼 같은 키). 주소를 따로 주면 키도 따로 준다 — LLM 키를 다른 서버에 보내지 않는다 |
| `SENSING_EMBED_DIMENSIONS` | `1024` | `chunk.embedding` 열 크기라 1024여야 한다. `dimensions`를 보내 줄여 받고(Gemini·OpenAI), 모르는 서버가 400을 내면 빼고 다시 보낸다(그때는 1024차원 모델이어야 함) |

| 변수 (워커) | 기본값 | 설명 |
| --- | --- | --- |
| `SENSING_EMBED_BATCH` / `SENSING_EMBED_PER_MINUTE` | `20` / `80` | 한 번에 보내는 조각 수, 분당 보내는 조각 수 상한(0이면 없음) |
| `SENSING_EMBED_TIMEOUT` / `SENSING_EMBED_BACKOFF` | `30` / `60` | 묶음 호출 제한 시간(초), 실패 뒤 쉬는 시간(초, 이어지면 두 배씩 최대 30분) |

| 변수 (백엔드) | 기본값 | 설명 |
| --- | --- | --- |
| `SENSING_EMBED_QUERY_TIMEOUT` | `10` | 질의 임베딩 제한 시간(초). 넘으면 전문 검색만으로 답한다 |
| `SENSING_EMBED_MIN_SIMILARITY` | `0.58` | 의미 후보의 코사인 유사도 하한 (아래 실측으로 정함, 모델을 바꾸면 다시 맞춘다). 후보 수(50)·RRF k(60)는 `sensing.search.*` |

이미 쌓인 조각은 유휴 시간에 조금씩 채워지고, 한 번에 하려면 `sensing-embed-backfill [--limit N]`(분당 상한을 지킨다)입니다.
로컬은 `scripts/dev/worker.local.env`에 `SENSING_EMBED_*`를 넣으면 워커와 `run-backend.ps1`이 함께 읽습니다.

**실측 (2026-10-19, Gemini OpenAI 호환 주소, 실제 호출 9번 — 모델 목록 1·임베딩 성공 6·한도 초과 2)** — ① `gemini-embedding-001`은 `dimensions: 1024`를 받아
1024차원(정규화 안 된 MRL 절단 벡터, 코사인에는 무관)을 주고, 응답의 첫 항목은 `index`(0)가 빠져 온다(두 클라이언트 모두 순서로 처리). 모델 목록에는
`gemini-embedding-2`도 있다. ② **무료 등급은 묶음 안의 글 하나를 요청 하나로 세어 분당 100개**(`EmbedContentRequestsPerMinute…FreeTier`)라
75개 묶음 한 번이 429였다 — 기본값을 20개 묶음·분당 80개로 잡은 이유다. 하루 한도도 있어 운영 DB의 조각 9,096개를 무료 등급으로 한 번에 채우기는
어렵고(유휴 시간에 조금씩), 실제 운영은 유료 등급이나 사내 임베딩 모델(bge-m3 등, 1024차원)을 권한다. ③ 운영 DB의 최근 기사 제목 60건(읽기만)과
질의 7개로 품질을 보았다: "배드민턴" → 안세영 우승 기사 2건(0.59·0.61), "가정폭력 피해자 보호 장치" → 스마트워치 피습 기사 4건(0.62~0.64),
"AI 반도체 인프라 투자" → GPU·데이터센터·반도체 기사 4건(0.61~0.63), "프로야구 포스트시즌" → 준PO·KS·구단 인사 3건(0.59~0.66), "개인정보 유출" →
할리스 유출(0.70) — 모두 낱말이 겹치지 않거나 일부만 겹친다. 관련 없는 기사는 대부분 0.56 이하(질의별 중앙값 0.47~0.50, 가장 높은 것이 "기상 악화
피해" → 축제 식중독 피해 0.579)여서 하한을 0.58로 두었다. 표본이 작으므로 운영 데이터가 임베딩되면 다시 본다. ④ 워커(`OpenAiCompatEmbedder`)가 만든
제목 벡터와 백엔드(`OpenAiCompatEmbeddingClient`)가 만든 질의 벡터가 같은 공간인지 확인했다: "배드민턴" ↔ 안세영 우승 0.609(③과 같은 값), ↔ 마을버스
요금 인상 0.511.

### API 맛보기

PoC 인증은 `X-User` 헤더의 login_id로 합니다 (`admin`, `registrar`, `viewer` 기본 계정). SSO 연동 시 `CurrentUserResolver`만 교체합니다.

```bash
# 사이트 등록 (기사 주소를 넣어도 도메인으로 등록)
curl -X POST localhost:8080/api/v1/registrations -H 'X-User: registrar' \
     -H 'Content-Type: application/json' -d '{"url":"https://news.example.com/news/articleView.html?idxno=1"}'

# 내 등록 요청과 진행 단계
curl localhost:8080/api/v1/registrations/mine -H 'X-User: registrar'

# 사이트 일괄 등록 (REGISTRAR 이상, 최대 500건. JSON 배열 또는 text/plain 한 줄에 하나·# 뒤는 메모. 한 건의 오류가 다른 건을 막지 않는다) → batchId
curl -X POST localhost:8080/api/v1/registrations/bulk -H 'X-User: registrar' -H 'Content-Type: text/plain' --data-binary @docs/eval-sites.txt
curl localhost:8080/api/v1/registrations/batches/<batchId> -H 'X-User: registrar'   # 몇 분 뒤: 묶음의 completed·review·rejected·byStatus·byStep
curl localhost:8080/api/v1/ops/overview -H 'X-User: admin'   # 관리자 운영 지표: 소스 상태별·처리 대기 큐·작업 큐 적체·24시간 결과·수집량·실패 코드·처리량(demandRatio>1이면 워커 부족). /ops/sources/health 는 조용히 멈춘 소스 순

# 키워드 감시 등록 (수집 기사 대조. 네이버 키가 있으면 네이버 검색도 60분 주기로. VIEWER는 승인 대기) · 내 감시 · 키워드 수정(설정 새 버전)
curl -X POST localhost:8080/api/v1/watches -H 'X-User: registrar' -H 'Content-Type: application/json' -d '{"name":"자사 보도","keywords":["메타넷","메타넷글로벌"],"exclude":["채용"]}'
curl localhost:8080/api/v1/watches -H 'X-User: registrar'   # PUT /api/v1/watches/1 -d '{"keywords":["메타넷","신사업"]}'

# 수집 문서 (요약·분류·감시 검색어 포함, 발행일 내림차순). since는 수집 시각 기준, topic·keyword·source_id로 거르고 limit 기본 50·최대 200
curl 'localhost:8080/api/v1/documents?since=2026-10-15T00:00:00Z&topic=정책·규제&keyword=메타넷' -H 'X-User: viewer'
curl localhost:8080/api/v1/documents/1 -H 'X-User: viewer'      # 본문·첨부 포함. GET /api/v1/documents/topics?since=... 는 토픽별 건수

# 일간 다이제스트 구독 (매일 hourKst시 KST에 지난 다이제스트 이후 새 문서를 감시별·토픽별로 묶어 메일 또는 웹훅으로) · 내 구독 · 미리보기·즉시 전송
curl -X POST localhost:8080/api/v1/digests -H 'X-User: viewer' -H 'Content-Type: application/json' -d '{"name":"아침 보고","channel":"EMAIL","target":"me@example.com","hourKst":8,"filters":{"watchIds":[1],"topics":["정책·규제","입찰·공고"]}}'
curl localhost:8080/api/v1/digests -H 'X-User: viewer'          # PUT /api/v1/digests/1 -d '{"channel":"WEBHOOK","target":"https://hooks.slack.com/services/..."}', DELETE /api/v1/digests/1
curl -X POST localhost:8080/api/v1/digests/1/preview -H 'X-User: viewer'   # text·html·documentCount. /send-now 는 바로 보내고 digest_run에 기록, /runs 는 보낸 기록

# 회사(거래처) 마스터 (REGISTRAR 이상. 회사 감시 + 점검 소스가 함께 생긴다) · 목록(신호 수·최근 신호·연결 문서 수)과 상세 · 회사 신호(심각도 → 시각 내림차순)
curl -X POST localhost:8080/api/v1/companies -H 'X-User: registrar' -H 'Content-Type: application/json' -d '{"name":"나부품","aliases":["NABU"],"bizNo":"123-45-67890","corpCode":"00123456","externalId":"CAI-001","tags":["1차 공급사"]}'
curl localhost:8080/api/v1/companies -H 'X-User: viewer'            # GET /api/v1/companies/1 (최근 신호·문서 20건, 종류별 최근 1건 latestByKind), PUT /api/v1/companies/1, DELETE 는 보관. POST /api/v1/companies/import 는 JSON 배열 또는 CSV(name,aliases(;구분),biz_no,...) upsert
curl 'localhost:8080/api/v1/company-signals?since=2026-10-15&severity=HIGH' -H 'X-User: viewer'   # kind=BIZ_STATUS|NEWS|... · GET /api/v1/companies/1/signals?since=

# 챗봇 (VIEWER 이상, 대화는 만든 사람만): 설정 여부 · 대화 만들기 · 질문(답이 올 때까지 기다린다, LLM 오류는 error=true인 답) · 대화 불러오기 · 목록 · 지우기
curl localhost:8080/api/v1/chat/status -H 'X-User: viewer'          # {"configured":true,"provider":"openai gemini-3.5-flash-lite","maxToolRounds":4}
curl -X POST localhost:8080/api/v1/chat/sessions -H 'X-User: viewer' -H 'Content-Type: application/json' -d '{}'
curl -X POST localhost:8080/api/v1/chat/sessions/1/messages -H 'X-User: viewer' -H 'Content-Type: application/json' -d '{"content":"오늘 많이 보도된 이슈 알려 줘"}'
curl localhost:8080/api/v1/chat/sessions/1 -H 'X-User: viewer'      # 질문과 답, 답마다 citations(refNo·title·url·sourceLabel)·tools. GET /api/v1/chat/sessions 목록, DELETE /api/v1/chat/sessions/1

# 관리자: 소스 목록·상세·조치
curl localhost:8080/api/v1/sources -H 'X-User: admin'
curl localhost:8080/api/v1/sources/1 -H 'X-User: admin'
curl -X POST localhost:8080/api/v1/sources/1/actions/recheck -H 'X-User: admin'   # pause|resume|run|recheck|rediscover|exclude
curl -X POST localhost:8080/api/v1/sources/1/configs/2/activate -H 'X-User: admin'  # 설정 버전 롤백
curl -X PUT localhost:8080/api/v1/sources/1 -H 'X-User: admin' -H 'Content-Type: application/json' -d '{"intervalMinutes":120}'   # 사람이 정한 주기는 잠겨(intervalLocked) 워커의 자동 조정이 덮지 않는다

# 관리자: 처리 대기 큐 (승인 대기 → API_KEY → REVIEW → BROKEN → BLOCKED, 오래 기다린 순). kind로 거를 수 있다
curl localhost:8080/api/v1/review-queue -H 'X-User: admin'
curl localhost:8080/api/v1/review-queue/counts -H 'X-User: admin'
curl -X POST localhost:8080/api/v1/registrations/3/reject -H 'X-User: admin'      -H 'Content-Type: application/json' -d '{"reason":"업무와 관련 없는 사이트"}'

# 관리자: 공개 API 키 등록 (암호화 저장 후 이 키를 기다리던 소스가 자동으로 다시 진행된다) · 조회(비밀 없음) · 삭제
curl -X POST localhost:8080/api/v1/api-credentials -H 'X-User: admin' -H 'Content-Type: application/json' -d '{"provider":"dart","secret":"<발급받은 키>","label":"공시 API"}'
curl -X POST localhost:8080/api/v1/api-credentials -H 'X-User: admin' -H 'Content-Type: application/json' -d '{"provider":"naver_news","secret":"<client_id>:<client_secret>","label":"네이버 검색"}'
curl -X POST localhost:8080/api/v1/api-credentials -H 'X-User: admin' -H 'Content-Type: application/json' -d '{"provider":"data_go_kr","secret":"<공공데이터포털 서비스키>","label":"공공데이터포털"}'
curl localhost:8080/api/v1/api-credentials -H 'X-User: admin'   # DELETE /api/v1/api-credentials/dart
```

## 폴더

```
backend/   Spring Boot 3 (Java 17), Flyway 스키마: src/main/resources/db/migration
  io/sensinghub/llm/   LLM 클라이언트 (OpenAI 호환 · Claude · 미설정), 질의 임베딩 클라이언트, 오류 분류 (챗봇용)
  io/sensinghub/chat/  챗봇: 도구(ChatTools), 대화·도구 루프·근거(ChatService), 시스템 프롬프트, 키 가리기
  io/sensinghub/document/  문서 조회·전문 검색(DocumentService), 하이브리드 검색(HybridSearch: 전문 + 의미, RRF)
worker/    Python 수집 워커
  src/sensing_worker/
    fetcher.py    수집 하네스 HTTP 계층: 간격·robots·재시도·차단 분류·내부망 차단
    discovery.py  자동 탐색: RSS → 사이트맵 → 링크 패턴 (CMS 패턴 라이브러리)
    recipe.py     LLM 레시피: 홈·샘플 페이지 → 허브·서명·셀렉터 JSON → 검증 → 품질 검사
    llm.py        LLM 클라이언트 (Claude · OpenAI 호환 · 없음), JSON 꺼내기·스키마 검증·재요청
    extract.py    기사 추출: og/article/JSON-LD 메타 + 본문 + 첨부 링크 (설정 selectors 우선)
    policy.py     사이트 유형 판정, 기본값, 품질 검사
    apis/         공개 API 소스: 카탈로그(도메인 → 제공자), 커넥터 형식, DART 공시 목록·네이버 뉴스 검색·국세청 사업자 상태 커넥터
    company.py    회사 레이어 규칙: 태그 → 회사 정확 일치, 회사 신호(BIZ_STATUS·NEWS)의 심각도·fingerprint
    jobs.py       ONBOARD / CRAWL 작업 (API 소스는 크롤링 대신 커넥터로, 키워드 감시는 document_watch 기록, CHECK 소스는 회사 상태 점검)
    db.py         작업 큐(SKIP LOCKED·lease), 문서 저장·버전, 회사 연결·신호
    indexer.py    전문 검색 색인(조각·낱말)과 이슈 묶기 (textindex.py·issues.py)
    embed.py      의미 검색 임베딩: 색인 조각 → OpenAI 호환 /embeddings → chunk.embedding (분당 상한·실패 시 쉬기)
  tests/          단위 테스트 + 가짜 사이트로 DB 흐름 통합 테스트
docs/      설계 메모 — 수집 원리는 docs/how-collection-works.md (사이트를 등록하면 기사를 어떻게 찾아 가져오나)
```

## 테스트

```bash
# worker: 단위 테스트 + (DB가 있으면) 통합 테스트
cd worker && SENSING_TEST_DSN=postgresql://sensing:sensing@localhost:5432/sensing_test pytest -q

# backend: 단위 테스트 + (DB가 있으면) 통합 테스트
cd backend && SENSING_IT_DB_URL=jdbc:postgresql://localhost:5432/sensing_it ./gradlew test
```

GitHub Actions(`.github/workflows/ci.yml`)가 push마다 두 모듈을 pgvector DB와 함께 검증합니다.
챗봇·LLM·임베딩 테스트는 가짜 LLM HTTP 서버(`MockLlmServer`, 워커는 `httpx.MockTransport`·`FakeEmbedder`)만 부르므로 CI에서 실제 LLM·임베딩 호출이 없습니다.
통합 테스트는 클래스마다 Spring 컨텍스트(연결 풀)를 캐시하므로 테스트에서는 풀을 4개로 줄입니다(`backend/src/test/resources/config/application.properties`) —
기본 10개면 IT 9개만으로 PostgreSQL 기본 한도(100)에 닿고, 같은 DB 서버에 로컬 백엔드·워커가 붙어 있으면 "too many clients"로 실패합니다.

## 진행 현황과 다음 단계

| 단계 | 내용 | 상태 |
| --- | --- | --- |
| M0 골격·환경 | 스키마, 등록 API, 스케줄러, 작업 큐, 워커 골격 | 완료 |
| M1 하네스·탐색 | 자동 탐색 고도화, 도메인 단위 간격(워커 간 공유), 이상 감지, 관리자 처리 대기 API, LLM 레시피 생성 | 완료 |
| M2 외부 API·키워드 감시 | API 카탈로그·자격 증명 저장소·API 수집기(뉴스 검색·DART·공공데이터포털), 키워드 감시, 수집 시점 요약·분류, 일간 다이제스트 알림, 최소 화면(등록·내 요청·관리자 큐) | 완료 (2026-10-18, 최소 화면 포함) |
| M3 회사 레이어·공급사 리스크 소스 | 회사(거래처) 마스터와 별칭 매칭, 문서→회사 연결, 리스크 신호 분류(재무·제재·사고·경영 변동), 공공 API 커넥터(국세청 사업자 상태, 조달청 부정당업자, DART 기업개황·재무), 회사별 다이제스트, 케이던시아 연동(마스터 동기화·이벤트 전달 — 목업으로 형태 완료, 실제 API는 SDS 확인 뒤) | 거의 완료 — 남은 것: DART 재무(FINANCIAL), 회사별 다이제스트 화면, 실제 케이던시아 API(SDS 확인 뒤) |
| M4 동적·첨부 | Playwright 동적 수집, 첨부 다운로드·HWP/PDF 텍스트 추출, 원문 삭제(GONE) 감지, (합의 시) 회사 계정 로그인 세션 | 다음 |
| M5 검색·챗봇 | 인덱싱 작업(청크·Kiwi·임베딩), 하이브리드 검색, 중복 보도 묶기, 챗봇 tool calling(`search_documents`·`list_issues`·`list_watches`·`create_watch`·`register_source`·`get_source_status`), 대화 저장 | 진행 중 — 된 것: 전문 검색 색인(Kiwi·tsvector)과 이슈 묶기·화면, 챗봇 1단계(도구 호출·대화 저장·근거 링크·화면), 챗봇 2단계 의미 검색(조각 임베딩·하이브리드 RRF, 2026-10-19). 남은 것: 대화 요약·답 피드백, 운영 데이터 임베딩(유료 등급 또는 사내 임베딩 모델) |
| M6 관리자·운영 | 레시피 수정·미리보기, 설정 백필, 감사 로그 화면, 운영 지표 | |

로드맵은 현업 요청서(대외협력·커뮤니케이션 대상 외부정보 센싱)에 맞춰 2026-10-16에 다시 짰다. 요건 대응표와
안티봇 범위 합의안은 [docs/requirements-mapping.md](docs/requirements-mapping.md)에 있다. 핵심 변경: 언론 보도·공시는
크롤링보다 공개 API를 우선하고, 두 팀의 업무(키워드로 보고 아침에 받아 보기)에 맞춰 키워드 감시·다이제스트를 챗봇보다 앞에 둔다.
같은 날 현업이 삼성SDS 구매·공급망관리 SaaS **케이던시아(Caidentia)** 를 함께 쓴다는 것이 확인되어, 고객(두 팀)과 엔진은
그대로 두고 **데이터 소스를 공급사 리스크 쪽으로 넓히는** M3을 동적 수집보다 앞에 두었다. 문서 중심(오늘 들어온 문서)에
회사 중심(거래처별 외부 신호)을 한 층 얹는 것이며, 자세한 소스 목록은 요건 문서의 "공급사 리스크 소스" 절에 있다.

### 어디까지 왔고 무엇이 남았나 (2026-10-19)

**된 것** — URL 하나(또는 수백 개 일괄)로 사이트가 자동 등록되고 주기 수집된다. 공개 API(DART·공공데이터포털, 네이버 뉴스는 키가 있을 때)와 키워드 감시(기본은 수집 기사 대조),
수집 시점 요약·분류, 일간 다이제스트, 거래처 마스터와 위험 신호(국세청·DART 공시·조달청), 케이던시아 연동(목업), 링크 모음 페이지의
언론사 자동 확장, 운영 지표, 그리고 이 모든 것을 다루는 브라우저 화면. 실측: 표본 10곳 중 9곳 자동 등록(10-19 재측정 10/10),
주간지 8곳 중 6곳 자동 등록(REVIEW였던 한겨레21·주간한국은 10-19 규칙 보강 뒤 자동 등록), 뉴스스탠드 → 언론사 50곳 자동 등록.
LLM 요약·분류는 10-19 Gemini 무료 등급(`gemini-3.5-flash-lite`)으로 문서 7,967건 전체를 실패 없이 처리(위 "워커 환경 변수 (LLM 레시피)").
수집 원리는 [docs/how-collection-works.md](docs/how-collection-works.md).

**진행 중 (10-19)** — 각각 별도 브랜치에서 작업하고 끝나면 main에 합친다.

- 과거 기사 채우기(백필): 현업 필수 요구로 확인. 사이트·기간(기본 최근 3개월)을 정하면 보관 사이트맵 → 목록 페이지 역순으로
  등록 이전 기사를 평소 수집이 쉬는 틈에 천천히 채운다 (`docs/how-collection-works.md` "입구 파일이 보여 주는 범위와 과거 기사").
- 사이트 하나에 기사 종류 여러 개: korea.kr이 보도자료만 모이는 문제(정책뉴스·브리핑·연설문 누락). 종류별 품질 검사·켜고 끄기,
  특정 목록 주소로 등록하면 그 종류만, 이미 있는 사이트에 다른 목록 주소를 넣으면 종류 추가.
- 챗봇: 1단계 도구 호출형 챗봇(문서 검색·이슈·감시·사이트 등록·상태), 대화 저장, 근거 링크, 챗봇 화면. 2단계 의미 검색 — 워커가 색인 조각을
  임베딩하고 `search_documents`가 낱말 일치와 의미 유사도를 RRF로 합친다 (임베딩이 없거나 실패하면 전문 검색 그대로).

**남은 것 (순서대로)**

1. M3 마무리: DART 재무 신호(FINANCIAL), 회사별 다이제스트 구독 화면, 실제 케이던시아 API 연결(SDS가 API를 주면 base-url·매핑 교체).
2. 관리자 확인으로 가는 사이트 줄이기: ~~한겨레21·주간한국~~ 10-19 일반 규칙으로 해결(비표준 발행일 메타·구분자 없는 날짜, 꼬리 날짜 무시,
   본문을 품은 `<header>` 보존, `[부고]` 단신 표본 제외 — `docs/how-collection-works.md` 4절). 남은 것: 규칙이 못 잡는 다음 사례가
   나오면 LLM 레시피로 붙이는 실측(Gemini 무료 키로 가능).
3. M4 동적·첨부: Playwright 동적 수집, HWP/PDF 첨부 텍스트 추출(공공기관 보도자료 본문), 원문 삭제 감지, (합의 시) 회사 계정 로그인 수집.
4. M5 검색·챗봇: 대화 요약(`chat_session.summary`)·답 피드백(`chat_feedback`), 운영 데이터 임베딩(무료 등급은 분당 100개·하루 한도라 유료 등급 또는
   사내 임베딩 모델 결정 필요), 화면 문서 검색에도 하이브리드를 쓸지 (전문 검색 색인과 이슈 묶기, 챗봇 1단계, 2단계 의미 검색은 2026-10-19 완료).
5. M6 관리자·운영: 레시피 수정·미리보기 화면, 설정 백필, 감사 로그 화면. (워커 하트비트 기반 지표는 10-19 완료: `worker_heartbeat`,
   운영 지표의 워커 수 = 최근 2분 안 신호, 워커 목록)
6. 작은 것: 네이버 API 쿼터 추적, Teams 웹훅 형식, favicon.

**코드 밖에서 결정돼야 하는 것** — 안티봇 범위 합의안 서명, 언론 기사 본문 저장 범위(법무), 사내 이용을 위한 뉴스 저작권 이용 계약
여부(한국언론진흥재단), 정부 사이트 본문 저장 기준(공공누리 표시 글만 본문 저장할지 — 정책브리핑은 저작권 보호를 이유로 2026-07-01 RSS 중단),
과거 기사가 필요한 기간·사이트, 키워드 감시 초기 목록과 알림 채널,
공개 API 키 발급 담당자, 케이던시아 API·데이터 반출 보안 검토, 운영 담당자. 자세한 목록은 요건 문서 끝의 체크리스트.

M1에서 한 일:

- [x] 도메인 단위 요청 간격을 워커 여러 대가 공유 (`domain_throttle` 슬롯 예약, `SENSING_SHARED_THROTTLE`)
- [x] 소스별 평소 수집량 대비 급감(`ANOMALY_DROP`) 감지와 2회 연속 시 BROKEN 전환 (기준: 같은 설정 버전의 최근 정상 실행 평균 후보 수, 30% 미만)
- [x] 실패 증거(응답 HTML 일부) 파일 저장과 `evidence_path` 기록 (`SENSING_EVIDENCE_DIR`/날짜/, 30일 보관)
- [x] 관리자용 처리 대기 큐 API (`/api/v1/review-queue`: 승인 대기·REVIEW·BROKEN·BLOCKED를 우선순위순으로)
- [x] 실제 사이트 10곳으로 자동 탐색 성공률 측정 도구 (`sensing-discovery-eval`, 표본 `docs/eval-sites.txt`)
- [x] 자동 탐색 성공률 목표 8/10 달성 — 2026-10-16 **9/10** (첫 측정 6/10, `docs/eval-result.json`)
  - 남은 1곳 daily.hankooki.com은 봇 확인 화면(`BOT_CHALLENGE`) → 원칙대로 우회하지 않음
  - 6 → 9로 올린 변경: 홈이 스크립트로 넘기는 사이트 따라가기, 다른 호스트로 넘어가면 그곳을 사이트로 보기,
    영숫자 식별자(`ATCL6fd477428`) 패턴화, 사이트맵 `lastmod` 최신순, 메뉴 이름(보도자료·공지)으로 게시판 찾기,
    후보를 유력한 순서로 품질 검사해 첫 합격 채택, 목록 행의 날짜를 상세에 넘기기, 게시판 제목·등록일·스크립트 첨부 추출,
    자기 하위 페이지로 링크하는 목록 페이지는 기사로 보지 않기, 공공기관은 본문 대신 첨부가 있어도 통과
- [x] 등록 시 LLM 레시피 생성 (`SENSING_LLM_PROVIDER`: Claude 또는 OpenAI 호환 API, 스키마·샘플 품질 검사를 통과한 것만 설정으로 저장, 실패·장애 시 휴리스틱)
- [x] 현업 샘플 기사가 등록된 패턴에 잡히는지 측정 — `docs/eval-sites.txt`에 "사이트 [샘플기사]"로 적으면 결과 표에 `샘플` 열(`sample_match`)이 붙는다.
  섹션 조각 병합(`/news/economy/{n}`+`/news/society/{n}` → `/news/{*}/{n}`)과 숫자 없는 슬러그(`/{slug}/`) 패턴화로
  2026-10-16 매일경제·EE Times·Tom's Hardware 샘플 **3/3 적중** (`docs/eval-samples.txt`, `docs/eval-samples-result.json`), 기존 표본 9/10 유지

M2에서 한 일:

- [x] API 소스 기반·DART: API 카탈로그(`worker/src/sensing_worker/apis/`), 자격 증명 저장소(`api_credential`, pgcrypto 암호화,
  `/api/v1/api-credentials`), 관리자 큐 `API_KEY` 종류, DART 공시 목록 커넥터. dart.fss.or.kr을 등록하면 크롤링 없이
  "공시 API 키 발급 필요"로 큐에 가고, 관리자가 키를 넣으면 검증 → `method: API` 설정 → 첫 수집 → 6시간 주기 수집이 자동으로 이어진다
- [x] 키워드 감시·네이버 뉴스 검색 API: `source.kind = WATCH`(V5), `POST /api/v1/watches`(이름·키워드·제외어, VIEWER는 승인 대기),
  `PUT`으로 키워드 수정 = 설정 새 버전, 네이버 뉴스 검색 커넥터(`apis/naver_news.py`: 검색어별 `sort=date` 최대 3페이지, 제목 태그 제거,
  제목·링크·발행일만), `document_watch`에 감시·검색어 기록 (같은 기사가 여러 감시에 걸려도 문서는 하나). news.naver.com을 사이트로
  등록하면 `KEYWORDS_REQUIRED`로 안내
- [x] 키워드 감시의 기본을 수집 기사 대조로 (2026-10-19, V13): 네이버 검색 API 신규 발급 중단으로 키가 없으면 감시를 만들 때 ONBOARD 없이
  바로 ACTIVE(`next_run_at` NULL, config `"naver": false`)로 두고 워커가 수집 기사마다 대조한다. 키가 있으면 네이버 검색도 함께, 키를 나중에 넣거나
  지우면 감시 모드를 따라 바꾼다. V13이 키 대기(REVIEW `API_KEY_*`)로 멈춘 감시를 대조 모드로 옮겨 관리자 키 큐에서 뺐다
- [x] 수집 시점 요약·분류: 워커 보강 단계(`enrich.py`, 큐가 비었을 때 묶음 10건을 LLM 한 번에, `SENSING_ENRICH_*`)가
  `document_enrichment`(V6)에 토픽·태그·요약을 두고, `GET /api/v1/documents`(since·topic·keyword·source_id 필터, 상세, 토픽별 건수)로
  읽는다. 언론 기사는 제목만 분류하고 요약을 저장하지 않으며, LLM 장애·검증 실패는 수집에 번지지 않는다
- [x] 일간 다이제스트 알림: 구독(`digest_subscription`, V7, `/api/v1/digests`: 감시·소스·토픽 필터, `hourKst`, EMAIL 또는 WEBHOOK)을
  스케줄러가 매일 그 시각(KST)에 처리해 지난 다이제스트 이후 새로 수집된 문서를 감시별(검색어별) → 토픽별 → 분류 전으로 묶어
  메일(HTML, `SENSING_MAIL_*`) 또는 웹훅(`{"text":...}`)으로 보낸다. 미리보기·즉시 전송 API, 보낸 본문과 결과(`SENT`·`EMPTY`·`FAILED`)는
  `digest_run`에 남고, 구독 하나의 실패는 다른 구독을 막지 않는다
- [x] 최소 화면: 빌드 도구·CDN 없는 정적 HTML·JS(`backend/src/main/resources/static/`, 해시 라우팅, 상단 사용자 선택 → `X-User`,
  `GET /api/v1/me`로 권한 확인해 관리자 메뉴 숨김). 등록·내 요청(일괄 등록 묶음 진행 포함)·문서·거래처·키워드 감시·다이제스트(미리보기·즉시 전송)와
  관리자 처리 대기 큐(항목별 가능한 조치만)·소스(건강 상태·상세·설정 버전·주기 잠금)·운영 지표·API 키
- [ ] 네이버 API 쿼터 추적, Teams 등 다른 웹훅 형식

M3에서 한 일:

- [x] 회사 마스터·문서→회사 연결·국세청 사업자 상태 신호: `company`·`document_company`·`company_signal`(V8), `/api/v1/companies`(등록·import upsert·
  목록·상세·수정·보관)와 `/api/v1/company-signals`, 회사 감시(`회사:<이름>`, 360분)로 걸린 기사와 보강 태그 정확 일치로 연결, 사건·사고·인사·조직
  기사는 NEWS 신호, 점검 소스(`kind = CHECK`, 하루 1회)가 공공데이터포털(`data_go_kr`) 국세청 사업자등록 상태조회로 폐업·휴업·미등록·과세유형
  변경을 BIZ_STATUS 신호로 기록(fingerprint로 중복 없음), 다이제스트 맨 앞 "거래처 신호" 절과 `companyIds` 필터
- [x] DART 고유번호 매핑·회사별 공시 신호·조달청 부정당업자 신호: 점검 소스에 점검 종류를 더해(`check`: `dart_corp_map` 주 1회 corpCode.xml ZIP으로
  corp_code 자동 채움, `dart_disclosure` 6시간마다 회사별 list.json → 문서(`document_company` API) + 보고서명 규칙으로 DISCLOSURE HIGH/MEDIUM/LOW,
  `g2b_debarment` 하루 1회 나라장터 사용자정보 서비스 `getUnptRsttCorpInfo02`로 사업자번호별 조회 → DEBARMENT HIGH) 처음 회사를 만들 때 네 점검
  소스를 함께 만들고, 상세 API에 종류별 최근 신호(`latestByKind`). 조달청 API의 응답 항목명은 포털에 공개돼 있지 않아 커넥터 config `fields`로
  바꿀 수 있다 (`worker/src/sensing_worker/apis/g2b_debarment.py` 참고)
- [x] 사이트 일괄 등록·대량 소스 운영 지표·주기 자동 조정: `POST /api/v1/registrations/bulk`(JSON 또는 text/plain, 최대 500건, 묶음 안 중복은 첫 건만,
  형식 오류도 INVALID로 같은 묶음에 남김, `registration_request.batch_id` V9)와 `GET /api/v1/registrations/batches/{batchId}` 진행 요약, 워커 큐가 ONBOARD 셋마다
  CRAWL 하나를 집는 3:1 교대(`SENSING_ONBOARD_RATIO`)로 일괄 등록이 주기 수집을 막지 않음, `GET /api/v1/ops/overview`(소스 상태별·처리 대기 큐·작업 큐 적체·워커 수·
  24시간 결과·수집량 상위 10·실패 코드 상위 10·처리량 `demandRatio`)와 `GET /api/v1/ops/sources/health`(마지막 성공이 오래된 순, `overdue`), 워커가 최근 10회 주기
  수집의 새 문서 평균으로 주기를 두 배씩 늘리거나(최대 1440분) 기본값으로 되돌림(`SENSING_ADAPTIVE_INTERVAL`, 변경은 로그와 `crawl_job.note`에),
  `PUT /api/v1/sources/{id}`로 사람이 정한 주기는 `interval_locked`로 잠겨 자동 조정이 덮지 않음
- [x] 케이던시아 연동(목업): 실제 API가 아직 없어 백엔드 안에 가짜 케이던시아(`/mock/caidentia/api`: 공급사 마스터 `GET/PUT suppliers`, 리스크 이벤트
  `POST/GET risk-events`, 헤더 `X-Caidentia-Key` 인증 흉내, 가공의 공급사 9곳 메모리 씨앗, `SENSING_CAIDENTIA_MOCK_ENABLED`)를 두고, 연동 코드
  (`io.sensinghub.caidentia`: `CaidentiaClient` 인터페이스 + RestClient 구현, 10초 제한)는 그것을 외부 시스템처럼 HTTP로 부른다. 공급사 마스터 동기화
  (`CaidentiaSyncService`, 매일 06:00 KST `SENSING_CAIDENTIA_SYNC_CRON`)는 `external_id`(= supplierId) → 사업자번호 순으로 회사를 맞춰 생성·갱신(보관됐으면 복구)
  하고 INACTIVE 공급사는 보관하며, 결과를 `caidentia_sync_run`(V10: fetched·created·updated·archived·unchanged·error)에 남긴다. 리스크 이벤트 전달
  (`CaidentiaPushService`, 5분마다)은 케이던시아 공급사인 회사의 HIGH·MEDIUM 신호를 `{supplierId, companyName, kind, severity, title, observedAt, detail}`로
  보내고 `company_signal.pushed_at`을 찍는다 (실패는 다음 틱에 재시도). 관리자 API `/api/v1/integrations/caidentia`(상태·`sync-now`·`push-now`·데모용
  `received-events`)와 화면. **실제 케이던시아가 나오면 `SENSING_CAIDENTIA_BASE_URL`·`SENSING_CAIDENTIA_API_KEY`와 `RestCaidentiaClient`의 필드 매핑만 바꾼다**
- [x] 링크 모음(포털) 페이지 등록 시 언론사 자동 확장: 자동 탐색이 실패한 홈이 다른 언론사로 가는 링크 모음(뉴스스탠드 등, `worker/src/sensing_worker/portal.py`:
  외부 언론사 도메인 10곳 이상이고 외부 링크가 자기 기사형 링크보다 많을 때)이면 REVIEW로 보내지 않고 가리키는 언론사를 요청자 이름으로 등록
  (백엔드 등록과 같은 행, 이미 열린 소스·상한 `SENSING_PORTAL_MAX_OUTLETS`=50 초과는 건너뜀, 묶음 `batch_id = portal:<소스 id>`), 모음 페이지 소스는
  `EXCLUDED/PORTAL_EXPANDED`, 요청은 COMPLETED(메시지에 묶음 id, 화면 "언론사 N곳 진행 보기" → `GET /registrations/batches/portal:<id>`)
- [ ] DART 기업개황·재무(FINANCIAL), 회사별 다이제스트 구독 화면, 케이던시아 실제 API 연결(SDS 확인 뒤)

M5에서 한 일:

- [x] 전문 검색 인덱스(Kiwi·tsvector)와 이슈 묶기: 워커가 큐가 빌 때(LLM과 무관, `SENSING_INDEX_BATCH`=200) 문서를 조각(`chunk`: 제목 seq 0,
  본문 800자 안팎 최대 20조각, 언론 기사는 제목만)으로 나눠 미리 뽑은 낱말을 `to_tsvector('simple', ...)`로 넣는다(`textindex.py`, 선택 의존성 `nlp`의
  Kiwi 형태소 분석, 없으면 조사를 떼는 정규식, `index_version` `fts-kiwi-v1`/`fts-regex-v1`). 색인 직후 같은 사건 기사를 제목 낱말 겹침으로
  이슈에 묶는다(`issues.py`, `issue`·`issue_document` V11, 72시간 창, 겹침 계수 0.4·겹친 낱말 2개 이상, 2개뿐이면 0.66). `GET /api/v1/documents?q=`는
  색인 낱말 접두 일치(모든 낱말, 조사 뗌, 색인 전 문서는 부분 일치), `GET /api/v1/issues`(언론사 수 → 기사 수 순, `since`·`min_docs`·`min_sources`·`q`)와
  상세, 화면 "이슈"(기간·언론사 수 칩, 검색, 펼쳐 보는 소속 기사)와 문서 화면의 "이슈 #id (N건)" 링크. 이미 쌓인 문서는 `sensing-index-backfill`로 한 번에.
  기준값은 운영 DB 제목 2,998건(53개 언론사)으로 보정했다 → 언론사 2곳 이상 이슈 287개, 3곳 이상 122개, 5곳 이상 47개 ([docs/decisions.md](docs/decisions.md))
- [x] 챗봇 1단계 (2026-10-19): 백엔드 LLM 클라이언트(`io.sensinghub.llm`: OpenAI 호환 `chat/completions` + tools, Claude Messages API + tools,
  `java.net.http`, 워커와 같은 `SENSING_LLM_*`·`ANTHROPIC_*`, 429·5xx·시간 초과를 사용자 문장으로), 챗봇 서비스·API(`/api/v1/chat/sessions`, 한 턴 도구 왕복 4번,
  모든 단계 `chat_message` 저장, 답의 `[n]` → `chat_citation`, 세션 소유자만), 도구 6개(문서 검색·이슈·감시 조회/생성·사이트 등록·진행 상태 — 화면과 같은 서비스),
  API 키는 채팅으로 받지 않고 붙여 넣은 키는 가림, 화면 "챗봇"(대화 목록·생각 중·근거 링크, 좁은 화면). 가짜 LLM 서버로 도구 루프·권한·저장·인용·429 통합 테스트
- [x] 챗봇 2단계 의미 검색 (2026-10-19): 워커 임베딩 단계(`embed.py`: 색인 조각 → OpenAI 호환 `/embeddings`, 언론 기사는 제목만·공공 문서는 제목+본문 조각,
  AI 봇 차단 사이트는 제목만, `chunk.embed_model`로 모델 구분(V15), 분당 상한·실패 시 쉬기, `sensing-embed-backfill`), 백엔드 질의 임베딩 클라이언트
  (`LlmHttp`와 같은 오류 분류)와 하이브리드 검색(`HybridSearch`: 전문 검색 + 코사인 유사도 하한 0.58, RRF k=60, 미설정·조각 없음·실패면 전문 검색),
  `search_documents`가 하이브리드를 쓰고 문서마다 `match` 표시. 가짜 임베딩 서버로 단위·통합 테스트, Gemini 실측(제목 60건·질의 7개)
- [ ] 대화 요약·답 피드백, 운영 데이터 임베딩 (유료 등급 또는 사내 임베딩 모델)

## 원칙

- robots.txt 준수, 식별 가능한 User-Agent, 도메인당 최소 2초 간격
- 차단 신호(403·429·봇 확인·로그인)는 재시도하지 않고 즉시 중단 → 관리자만 재점검
- 언론사는 기본 저장 범위를 제목·링크·발행일로 제한 (저작권)
- robots.txt에서 AI 봇을 막은 사이트는 `ai_restricted`로 표시하고, 챗봇은 그 사이트 문서의 요약을 LLM에 보내지 않는다 (제목·링크만).
  의미 검색 임베딩도 그 사이트는 제목만 외부 임베딩 API에 보낸다
