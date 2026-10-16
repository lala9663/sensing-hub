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

### API 맛보기

PoC 인증은 `X-User` 헤더의 login_id로 합니다 (`admin`, `registrar`, `viewer` 기본 계정). SSO 연동 시 `CurrentUserResolver`만 교체합니다.

```bash
# 사이트 등록 (기사 주소를 넣어도 도메인으로 등록)
curl -X POST localhost:8080/api/v1/registrations -H 'X-User: registrar' \
     -H 'Content-Type: application/json' -d '{"url":"https://news.example.com/news/articleView.html?idxno=1"}'

# 내 등록 요청과 진행 단계
curl localhost:8080/api/v1/registrations/mine -H 'X-User: registrar'

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
    apis/         공개 API 소스: 카탈로그(도메인 → 제공자), 커넥터 형식, DART 공시 목록 커넥터
    jobs.py       ONBOARD / CRAWL 작업 (API 소스는 크롤링 대신 커넥터로)
    db.py         작업 큐(SKIP LOCKED·lease), 문서 저장·버전
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
| M3 동적·첨부 | Playwright 동적 수집, 첨부 다운로드·HWP/PDF 텍스트 추출, 원문 삭제(GONE) 감지, (합의 시) 회사 계정 로그인 세션 | |
| M4 검색·챗봇 | 인덱싱 작업(청크·Kiwi·임베딩), 하이브리드 검색, 중복 보도 묶기, 챗봇 모달·tool calling(`register_source`·`get_source_status`·`submit_api_key`), 대화 저장 | |
| M5 관리자·운영 | 레시피 수정·미리보기, 설정 백필, 감사 로그 화면, 운영 지표 | |

로드맵은 현업 요청서(대외협력·커뮤니케이션 대상 외부정보 센싱)에 맞춰 2026-10-16에 다시 짰다. 요건 대응표와
안티봇 범위 합의안은 [docs/requirements-mapping.md](docs/requirements-mapping.md)에 있다. 핵심 변경: 언론 보도·공시는
크롤링보다 공개 API를 우선하고, 두 팀의 업무(키워드로 보고 아침에 받아 보기)에 맞춰 키워드 감시·다이제스트를 챗봇보다 앞에 둔다.

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

M2에서 한 일:

- [x] API 소스 기반·DART: API 카탈로그(`worker/src/sensing_worker/apis/`), 자격 증명 저장소(`api_credential`, pgcrypto 암호화,
  `/api/v1/api-credentials`), 관리자 큐 `API_KEY` 종류, DART 공시 목록 커넥터. dart.fss.or.kr을 등록하면 크롤링 없이
  "공시 API 키 발급 필요"로 큐에 가고, 관리자가 키를 넣으면 검증 → `method: API` 설정 → 첫 수집 → 6시간 주기 수집이 자동으로 이어진다
- [ ] 네이버 뉴스 검색·공공데이터포털 커넥터, 키워드 감시, 수집 시점 요약·분류, 일간 다이제스트, 최소 화면

## 원칙

- robots.txt 준수, 식별 가능한 User-Agent, 도메인당 최소 2초 간격
- 차단 신호(403·429·봇 확인·로그인)는 재시도하지 않고 즉시 중단 → 관리자만 재점검
- 언론사는 기본 저장 범위를 제목·링크·발행일로 제한 (저작권)
- robots.txt에서 AI 봇을 막은 사이트는 `ai_restricted`로 표시해 챗봇 답변 근거에서 제외할 수 있게 함
