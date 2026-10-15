# sensing-hub

사이트 주소만 등록하면 기사·보도자료를 **자동으로 찾아 주기적으로 수집**하고, 수집한 자료를 **챗봇에 자연어로 물어** 근거 링크와 함께 답을 받는 외부정보 수집 허브입니다. 현재 단계는 PoC 골격(M0)입니다.

- 사용자는 **URL 입력 한 번**만 합니다. 사전 점검 → 자동 탐색 → 품질 검사 → 기본값 적용 → 첫 수집은 시스템이 자동으로 처리합니다.
- 자동으로 판단할 수 없는 사이트는 사용자에게 다시 묻지 않고 **관리자 처리 대기(REVIEW)** 로 보냅니다.
- CAPTCHA·봇 차단·로그인 사이트는 **우회하지 않습니다.** 실패 사유로 분류하고 멈춥니다.

## 구조

```mermaid
flowchart LR
    U[사용자 / 관리자] -->|화면·챗봇| B[Spring Boot<br/>backend · Java 21]
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

필요: JDK 21, Python 3.11+, PostgreSQL 16 + pgvector

```bash
# DB
createdb sensing   # 사용자/비밀번호 sensing/sensing 기준 (application.yml 참고)

# backend (Flyway가 스키마를 만든다)
cd backend && ./gradlew bootRun

# worker
cd worker && pip install -e ".[dev,extract]"
SENSING_DB_DSN=postgresql://sensing:sensing@localhost:5432/sensing sensing-worker
```

사내망처럼 프록시로만 외부에 나가는 환경이면 워커에 `HTTPS_PROXY`를 지정합니다.

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
```

## 폴더

```
backend/   Spring Boot 3 (Java 21), Flyway 스키마: src/main/resources/db/migration
worker/    Python 수집 워커
  src/sensing_worker/
    fetcher.py    수집 하네스 HTTP 계층: 간격·robots·재시도·차단 분류·내부망 차단
    discovery.py  자동 탐색: RSS → 사이트맵 → 링크 패턴 (CMS 패턴 라이브러리)
    extract.py    기사 추출: og/article/JSON-LD 메타 + 본문 + 첨부 링크
    policy.py     사이트 유형 판정, 기본값, 품질 검사
    jobs.py       ONBOARD / CRAWL 작업
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
| M1 하네스·탐색 | 자동 탐색 고도화, 도메인 단위 간격(워커 간 공유), 이상 감지, 관리자 처리 대기 API | 다음 |
| M2 동적·첨부 | Playwright 동적 수집, 첨부 다운로드·HWP/PDF 텍스트 추출, 원문 삭제(GONE) 감지 | |
| M3 검색·챗봇 | 인덱싱 작업(청크·Kiwi·임베딩), 하이브리드 검색, 챗봇 모달·tool calling, 대화 저장 | |
| M4 관리자·운영 | 관리자 화면, 설정 수정·미리보기·백필, 알림, 감사 로그 화면 | |

M1에서 바로 할 일:

- [x] 도메인 단위 요청 간격을 워커 여러 대가 공유 (`domain_throttle` 슬롯 예약, `SENSING_SHARED_THROTTLE`)
- [x] 소스별 평소 수집량 대비 급감(`ANOMALY_DROP`) 감지와 2회 연속 시 BROKEN 전환 (기준: 같은 설정 버전의 최근 정상 실행 평균 후보 수, 30% 미만)
- [ ] 실패 증거(응답 HTML 일부) 파일 저장과 `evidence_path` 기록
- [ ] 관리자용 처리 대기 큐 API (`/api/v1/review-queue`)
- [ ] 실제 사이트 10곳으로 자동 탐색 성공률 측정 (목표 8곳)

## 원칙

- robots.txt 준수, 식별 가능한 User-Agent, 도메인당 최소 2초 간격
- 차단 신호(403·429·봇 확인·로그인)는 재시도하지 않고 즉시 중단 → 관리자만 재점검
- 언론사는 기본 저장 범위를 제목·링크·발행일로 제한 (저작권)
- robots.txt에서 AI 봇을 막은 사이트는 `ai_restricted`로 표시해 챗봇 답변 근거에서 제외할 수 있게 함
