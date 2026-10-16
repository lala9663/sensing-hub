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
    B -.->|M3| L[LLM·임베딩 API]
```

- **backend (Java)**: 시스템 본체. 등록 API, 관리자 API, 스케줄러, 권한, (M3) 챗봇.
- **worker (Python)**: 교체 가능한 작업자. 사전 점검, 자동 탐색, 기사 추출, (M2) 첨부 변환, (M3) 임베딩.
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
cd worker && pip install -e ".[dev,extract,llm-anthropic]"
SENSING_DB_DSN=postgresql://sensing:sensing@localhost:5432/sensing sensing-worker
```

사내망처럼 프록시로만 외부에 나가는 환경이면 워커에 `HTTPS_PROXY`를 지정합니다.

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
```

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

현업(대외협력·커뮤니케이션)은 사이트가 아니라 키워드(자사·경쟁사·이슈) 단위로 보도를 봅니다. 키워드 묶음을 등록하면 네이버 뉴스 검색 API로
60분마다 받아 문서로 저장하고, 어느 감시의 어느 검색어에 걸렸는지 `document_watch`에 남깁니다 (다이제스트가 이 표를 읽습니다).
감시 하나는 `source` 한 행(`kind = WATCH`, `watch_name`)이고 키워드는 설정 버전 config(`{"method":"API","provider":"naver_news","keywords":[...],"exclude":[...]}`)에
두므로, 키워드 수정은 설정 새 버전 활성화이고 롤백·일시정지·제외 같은 소스 조치를 그대로 씁니다. 네이버 뉴스 웹은 robots.txt가 전체 차단이라
크롤링하지 않으며, `news.naver.com`을 사이트처럼 등록하면 "키워드 감시로 등록하세요"(`KEYWORDS_REQUIRED`)로 안내합니다.
네이버 키는 `client_id:client_secret` 한 문자열로 `/api/v1/api-credentials`에 넣고(provider `naver_news`), 언론 기사이므로 제목·링크·발행일만 저장합니다.

### 수집 시점 요약·분류 (M2)

워커는 큐에 작업이 없을 때 보강되지 않은 문서를 최신순으로 한 묶음(기본 10건) 가져와 LLM에 한 번에 보내고, 문서마다 분류
토픽(정책·규제, 입찰·공고, 지원사업, 자사, 경쟁사, 산업동향, 인사·조직, 사건·사고, 기타)과 자유 태그(최대 5개)를 `document_enrichment`에
둡니다. 공공기관·기타 문서는 본문(앞 4,000자)으로 2문장 이내 요약도 만들지만, 언론 기사는 제목만으로 분류하고 요약은 저장하지 않습니다.
수집과 분리돼 있어 LLM 장애는 수집을 막지 않고(아무것도 쓰지 않고 다음 루프에 다시), 응답 검증 실패는 `FAILED`로 남겨 24시간 뒤
다시 시도합니다. 본문이 바뀌어 문서 버전이 오르면 다시 보강 대상이 되고, 워커 여러 대는 `FOR UPDATE SKIP LOCKED`로 겹치지 않습니다.
결과는 `/api/v1/documents`(VIEWER 이상)로 읽으며 다이제스트(M2-4)의 재료입니다.

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

### 회사(거래처) 마스터와 공급사 리스크 신호 (M3)

현업이 구매 SaaS 케이던시아를 함께 쓰므로, 문서 중심 위에 **회사 중심**("어느 거래처에 무슨 일")을 한 층 얹습니다. 회사(`company`: 이름·별칭·
사업자등록번호·DART 고유번호·외부 ID·태그)를 등록하면 ① 이름+별칭을 키워드로 한 네이버 뉴스 감시(`회사:<이름>`, 360분)가 생겨 걸린 기사를
`document_company`(WATCH)로 연결하고, ② 보강 태그(고유명사)가 회사 이름·별칭과 **정확히**(대소문자·공백 무시) 일치하면 TAG로 연결하며
(부분 일치·유사도 매칭은 오탐 때문에 하지 않습니다), ③ 연결된 기사의 토픽이 사건·사고·인사·조직이면 `company_signal`(NEWS, MEDIUM)을 남깁니다.
④ 첫 회사를 만들 때 **점검 소스**(`source.kind = CHECK`, 하루 1회)가 하나 생겨 사업자번호가 있는 모든 회사의 국세청 사업자등록 상태를
공공데이터포털 API로 확인하고, 폐업·휴업(HIGH)·미등록(MEDIUM)·과세유형 변경(LOW)을 `company_signal`(BIZ_STATUS)로 남깁니다. 같은 상태는
fingerprint(상태 코드+폐업일)로 매일 중복 기록하지 않습니다. 공공데이터포털 서비스키(provider `data_go_kr`, 국세청 사업자등록 상태조회
활용신청 필요)가 없으면 점검 소스는 다른 API 소스처럼 관리자 큐 `API_KEY`에서 기다립니다. 다이제스트는 기간 안의 회사 신호를 맨 앞 "거래처 신호"
절로 넣고(HIGH 먼저), 신호가 있으면 문서가 0건이어도 보냅니다. 삭제는 보관(ARCHIVED)이며 그 감시는 제외됩니다.

### API 맛보기

PoC 인증은 `X-User` 헤더의 login_id로 합니다 (`admin`, `registrar`, `viewer` 기본 계정). SSO 연동 시 `CurrentUserResolver`만 교체합니다.

```bash
# 사이트 등록 (기사 주소를 넣어도 도메인으로 등록)
curl -X POST localhost:8080/api/v1/registrations -H 'X-User: registrar' \
     -H 'Content-Type: application/json' -d '{"url":"https://news.example.com/news/articleView.html?idxno=1"}'

# 내 등록 요청과 진행 단계
curl localhost:8080/api/v1/registrations/mine -H 'X-User: registrar'

# 키워드 감시 등록 (네이버 뉴스 검색 API, 60분 주기. VIEWER는 승인 대기) · 내 감시 · 키워드 수정(설정 새 버전)
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
curl localhost:8080/api/v1/companies -H 'X-User: viewer'            # GET /api/v1/companies/1 (최근 신호·문서 20건), PUT /api/v1/companies/1, DELETE 는 보관. POST /api/v1/companies/import 는 JSON 배열 또는 CSV(name,aliases(;구분),biz_no,...) upsert
curl 'localhost:8080/api/v1/company-signals?since=2026-10-15&severity=HIGH' -H 'X-User: viewer'   # kind=BIZ_STATUS|NEWS|... · GET /api/v1/companies/1/signals?since=

# 관리자: 소스 목록·상세·조치
curl localhost:8080/api/v1/sources -H 'X-User: admin'
curl localhost:8080/api/v1/sources/1 -H 'X-User: admin'
curl -X POST localhost:8080/api/v1/sources/1/actions/recheck -H 'X-User: admin'   # pause|resume|run|recheck|rediscover|exclude
curl -X POST localhost:8080/api/v1/sources/1/configs/2/activate -H 'X-User: admin'  # 설정 버전 롤백

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
  tests/          단위 테스트 + 가짜 사이트로 DB 흐름 통합 테스트
docs/      설계 메모
```

## 테스트

```bash
# worker: 단위 테스트 + (DB가 있으면) 통합 테스트
cd worker && SENSING_TEST_DSN=postgresql://sensing:sensing@localhost:5432/sensing_test pytest -q

# backend: 단위 테스트 + (DB가 있으면) 통합 테스트
cd backend && SENSING_IT_DB_URL=jdbc:postgresql://localhost:5432/sensing_it ./gradlew test
```

GitHub Actions(`.github/workflows/ci.yml`)가 push마다 두 모듈을 pgvector DB와 함께 검증합니다.

## 진행 현황과 다음 단계

| 단계 | 내용 | 상태 |
| --- | --- | --- |
| M0 골격·환경 | 스키마, 등록 API, 스케줄러, 작업 큐, 워커 골격 | 완료 |
| M1 하네스·탐색 | 자동 탐색 고도화, 도메인 단위 간격(워커 간 공유), 이상 감지, 관리자 처리 대기 API, LLM 레시피 생성 | 완료 (허브 한 곳 실패 내성만 남음) |
| M2 외부 API·키워드 감시 | API 카탈로그·자격 증명 저장소·API 수집기(뉴스 검색·DART·공공데이터포털), 키워드 감시, 수집 시점 요약·분류, 일간 다이제스트 알림, 최소 화면(등록·내 요청·관리자 큐) | 진행 중 |
| M3 회사 레이어·공급사 리스크 소스 | 회사(거래처) 마스터와 별칭 매칭, 문서→회사 연결, 리스크 신호 분류(재무·제재·사고·경영 변동), 공공 API 커넥터(국세청 사업자 상태, 조달청 부정당업자, DART 기업개황·재무), 회사별 다이제스트, 케이던시아 연동(마스터 동기화·이벤트 전달, SDS API 확인 뒤) | 진행 중 |
| M4 동적·첨부 | Playwright 동적 수집, 첨부 다운로드·HWP/PDF 텍스트 추출, 원문 삭제(GONE) 감지, (합의 시) 회사 계정 로그인 세션 | |
| M5 검색·챗봇 | 인덱싱 작업(청크·Kiwi·임베딩), 하이브리드 검색, 중복 보도 묶기, 챗봇 모달·tool calling(`register_source`·`get_source_status`·`submit_api_key`), 대화 저장 | |
| M6 관리자·운영 | 레시피 수정·미리보기, 설정 백필, 감사 로그 화면, 운영 지표 | |

로드맵은 현업 요청서(대외협력·커뮤니케이션 대상 외부정보 센싱)에 맞춰 2026-10-16에 다시 짰다. 요건 대응표와
안티봇 범위 합의안은 [docs/requirements-mapping.md](docs/requirements-mapping.md)에 있다. 핵심 변경: 언론 보도·공시는
크롤링보다 공개 API를 우선하고, 두 팀의 업무(키워드로 보고 아침에 받아 보기)에 맞춰 키워드 감시·다이제스트를 챗봇보다 앞에 둔다.
같은 날 현업이 삼성SDS 구매·공급망관리 SaaS **케이던시아(Caidentia)** 를 함께 쓴다는 것이 확인되어, 고객(두 팀)과 엔진은
그대로 두고 **데이터 소스를 공급사 리스크 쪽으로 넓히는** M3을 동적 수집보다 앞에 두었다. 문서 중심(오늘 들어온 문서)에
회사 중심(거래처별 외부 신호)을 한 층 얹는 것이며, 자세한 소스 목록은 요건 문서의 "공급사 리스크 소스" 절에 있다.

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
- [x] 수집 시점 요약·분류: 워커 보강 단계(`enrich.py`, 큐가 비었을 때 묶음 10건을 LLM 한 번에, `SENSING_ENRICH_*`)가
  `document_enrichment`(V6)에 토픽·태그·요약을 두고, `GET /api/v1/documents`(since·topic·keyword·source_id 필터, 상세, 토픽별 건수)로
  읽는다. 언론 기사는 제목만 분류하고 요약을 저장하지 않으며, LLM 장애·검증 실패는 수집에 번지지 않는다
- [x] 일간 다이제스트 알림: 구독(`digest_subscription`, V7, `/api/v1/digests`: 감시·소스·토픽 필터, `hourKst`, EMAIL 또는 WEBHOOK)을
  스케줄러가 매일 그 시각(KST)에 처리해 지난 다이제스트 이후 새로 수집된 문서를 감시별(검색어별) → 토픽별 → 분류 전으로 묶어
  메일(HTML, `SENSING_MAIL_*`) 또는 웹훅(`{"text":...}`)으로 보낸다. 미리보기·즉시 전송 API, 보낸 본문과 결과(`SENT`·`EMPTY`·`FAILED`)는
  `digest_run`에 남고, 구독 하나의 실패는 다른 구독을 막지 않는다
- [ ] 네이버 API 쿼터 추적, Teams 등 다른 웹훅 형식, 최소 화면

M3에서 한 일:

- [x] 회사 마스터·문서→회사 연결·국세청 사업자 상태 신호: `company`·`document_company`·`company_signal`(V8), `/api/v1/companies`(등록·import upsert·
  목록·상세·수정·보관)와 `/api/v1/company-signals`, 회사 감시(`회사:<이름>`, 360분)로 걸린 기사와 보강 태그 정확 일치로 연결, 사건·사고·인사·조직
  기사는 NEWS 신호, 점검 소스(`kind = CHECK`, 하루 1회)가 공공데이터포털(`data_go_kr`) 국세청 사업자등록 상태조회로 폐업·휴업·미등록·과세유형
  변경을 BIZ_STATUS 신호로 기록(fingerprint로 중복 없음), 다이제스트 맨 앞 "거래처 신호" 절과 `companyIds` 필터
- [ ] 조달청 부정당업자 제재(DEBARMENT), DART 기업개황·재무(FINANCIAL)와 회사별 공시(corp_code), 회사별 다이제스트 구독 화면, 케이던시아 연동

## 원칙

- robots.txt 준수, 식별 가능한 User-Agent, 도메인당 최소 2초 간격
- 차단 신호(403·429·봇 확인·로그인)는 재시도하지 않고 즉시 중단 → 관리자만 재점검
- 언론사는 기본 저장 범위를 제목·링크·발행일로 제한 (저작권)
- robots.txt에서 AI 봇을 막은 사이트는 `ai_restricted`로 표시해 챗봇 답변 근거에서 제외할 수 있게 함
