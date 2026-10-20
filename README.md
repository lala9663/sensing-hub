# sensing-hub

사이트 주소만 등록하면 언론사·공공기관의 기사·보도자료를 **자동으로 찾아 주기적으로 수집**하고, 그 위에서 키워드 감시·이슈 묶기·
거래처 위험 신호·매일 아침 구독 메일·**챗봇 질의**(근거 링크와 함께)를 제공하는 사내 외부정보 센싱 허브(PoC)입니다.
사용자는 URL 입력 한 번만 하고, 자동으로 판단할 수 없는 사이트는 관리자 처리 대기로 보냅니다.
CAPTCHA·봇 차단·로그인 사이트는 **우회하지 않습니다.**

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

- **backend (Java)**: 시스템 본체. 등록·관리자 API, 스케줄러(구독 발송·케이던시아 연동), 권한, 화면, 챗봇(도구 호출형, 하이브리드 문서 검색).
- **worker (Python)**: 교체 가능한 작업자. 사전 점검, 자동 탐색, 기사 추출, 요약·분류, 전문 검색 색인·이슈 묶기, 의미 검색 임베딩.
- 둘은 DB 작업 큐(`crawl_job`)로만 연결됩니다. 메시지 브로커는 쓰지 않습니다.

```
backend/   Spring Boot 3 (Java 17), Flyway 스키마: src/main/resources/db/migration, 화면: src/main/resources/static/
  io/sensinghub/llm/   LLM 클라이언트 (OpenAI 호환 · Claude · 미설정), 질의 임베딩 클라이언트, 오류 분류 (챗봇용)
  io/sensinghub/chat/  챗봇: 도구(ChatTools), 대화·도구 루프·근거(ChatService), 시스템 프롬프트, 키 가리기
  io/sensinghub/document/  문서 조회·전문 검색(DocumentService), 하이브리드 검색(HybridSearch: 전문 + 의미, RRF)
worker/    Python 수집 워커
  src/sensing_worker/
    fetcher.py    수집 하네스 HTTP 계층: 간격·robots·재시도·차단 분류·내부망 차단
    discovery.py  자동 탐색: RSS → 사이트맵 → 링크 패턴 (CMS 패턴 라이브러리), 사이트 하나의 기사 종류 여러 개
    recipe.py     LLM 레시피: 홈·샘플 페이지 → 허브·서명·셀렉터 JSON → 검증 → 품질 검사
    llm.py        LLM 클라이언트 (Claude · OpenAI 호환 · 없음), JSON 꺼내기·스키마 검증·재요청
    extract.py    기사 추출: og/article/JSON-LD 메타 + 본문 + 첨부 링크 (설정 selectors 우선)
    policy.py     사이트 유형 판정, 기본값, 품질 검사
    apis/         공개 API 소스: 카탈로그(도메인 → 제공자), 커넥터 형식, DART 공시 목록·네이버 뉴스 검색·국세청 사업자 상태 커넥터
    company.py    회사 레이어 규칙: 태그 → 회사 정확 일치, 회사 신호(BIZ_STATUS·NEWS)의 심각도·fingerprint
    jobs.py       ONBOARD / CRAWL 작업 (API 소스는 크롤링 대신 커넥터로, 키워드 감시는 document_watch 기록, CHECK 소스는 회사 상태 점검)
    backfill.py   과거 기사 채우기(BACKFILL 작업 하나): 기간 묶음(최근 것부터)·보관 사이트맵 고르기(번호 묶음 이분 탐색)·목록 역순·양보·취소·진행 위치
    db.py         작업 큐(SKIP LOCKED·lease), 문서 저장·버전, 회사 연결·신호
    indexer.py    전문 검색 색인(조각·낱말)과 이슈 묶기 (textindex.py·issues.py)
    embed.py      의미 검색 임베딩: 색인 조각 → OpenAI 호환 /embeddings → chunk.embedding (분당 상한·실패 시 쉬기)
  tests/          단위 테스트 + 가짜 사이트로 DB 흐름 통합 테스트
docs/      기능 안내(docs/guide/)와 설계 메모 — 수집 원리는 docs/how-collection-works.md
```

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
SENSING_DB_DSN=postgresql://sensing:sensing@127.0.0.1:5432/sensing sensing-worker
```

- 사내망처럼 프록시로만 외부에 나가는 환경이면 워커에 `HTTPS_PROXY`를 지정합니다.
- 화면은 브라우저에서 http://127.0.0.1:8080 입니다. 상단에서 사용자(admin · registrar · viewer)를 고릅니다(PoC 인증, `X-User` 헤더).
- LLM(레시피·요약·챗봇)은 선택입니다. 키는 git에 올라가지 않는 `scripts/dev/worker.local.env`에 두면 `run-worker.ps1`·`run-backend.ps1`이 읽습니다 —
  [docs/guide/llm.md](docs/guide/llm.md).

## 기능 안내

화면 왼쪽 메뉴 순서대로 탭마다 한 문서입니다 (목차: [docs/guide/](docs/guide/README.md)).

| 메뉴 | 문서 | 한 줄 소개 |
| --- | --- | --- |
| 등록·내 요청 | [01-registrations.md](docs/guide/01-registrations.md) | URL 하나(또는 수백 개)로 사이트 자동 등록, 진행 보기, 링크 모음 페이지의 언론사 자동 확장 |
| 문서 | [02-documents.md](docs/guide/02-documents.md) | 수집 문서 찾기(기간·토픽·전문 검색·사이트·기사 종류), LLM 분류·요약 |
| 이슈 | [03-issues.md](docs/guide/03-issues.md) | 같은 사건을 다룬 여러 언론사 기사 묶음 |
| 거래처 | [04-companies.md](docs/guide/04-companies.md) | 거래처 마스터, 위험 신호(국세청·DART 공시·조달청·보도), 케이던시아 연동(목업) |
| 키워드 감시 | [05-watches.md](docs/guide/05-watches.md) | 키워드로 수집 기사 대조, 공용 감시, 제외·다시 켜기 |
| 네이버 뉴스 | [06-naver-news.md](docs/guide/06-naver-news.md) | 네이버 뉴스 검색(표시 전용), 감시의 네이버 실시간, 주요 키워드 칩 |
| 구독 | [07-subscriptions.md](docs/guide/07-subscriptions.md) | 구독(일간 다이제스트): 매일 정한 시각에 메일·웹훅으로 새 기사 보고 |
| 챗봇 | [08-chatbot.md](docs/guide/08-chatbot.md) | 도구 호출형 챗봇, 근거 링크, 키워드 바로 검색, 의미 검색 |
| 관리자 | [09-admin.md](docs/guide/09-admin.md) | 처리 대기 큐, 소스(과거 기사 채우기), 운영 지표, API 키 |
| (공통) | [llm.md](docs/guide/llm.md) | LLM 연결 설정, Gemini 무료 등급 실측, 하루 한도 나눠 쓰기 |

더 깊은 내용: [수집 원리](docs/how-collection-works.md) · [네이버 뉴스 API](docs/naver-news-api.md) · [설계 결정](docs/decisions.md) ·
[현업 요건 대응](docs/requirements-mapping.md) · [과거 기사 가능 범위 조사](docs/archive-sitemap-survey.md) · [진행 기록](docs/progress.md)

## 테스트

```bash
# worker: 단위 테스트 + (DB가 있으면) 통합 테스트
cd worker && SENSING_TEST_DSN=postgresql://sensing:sensing@127.0.0.1:5432/sensing_test pytest -q

# backend: 단위 테스트 + (DB가 있으면) 통합 테스트
cd backend && SENSING_IT_DB_URL=jdbc:postgresql://127.0.0.1:5432/sensing_it ./gradlew test
```

- GitHub Actions(`.github/workflows/ci.yml`)가 push마다 두 모듈을 pgvector DB와 함께 검증합니다.
- 챗봇·LLM·임베딩 테스트는 가짜 LLM HTTP 서버(`MockLlmServer`, 워커는 `httpx.MockTransport`·`FakeEmbedder`)만 부르므로 CI에서 실제 LLM·임베딩 호출이 없습니다.
- 통합 테스트는 클래스마다 Spring 컨텍스트(연결 풀)를 캐시하므로 테스트에서는 풀을 4개로 줄입니다(`backend/src/test/resources/config/application.properties`) —
  기본 10개면 IT 9개만으로 PostgreSQL 기본 한도(100)에 닿고, 같은 DB 서버에 로컬 백엔드·워커가 붙어 있으면 "too many clients"로 실패합니다.
- 챗봇 "키워드만" 판정은 서버·화면이 같은 사례 파일로 확인합니다: `node scripts/dev/check-chat-shortcuts.mjs`.

## 원칙

- robots.txt 준수, 식별 가능한 User-Agent, 도메인당 최소 2초 간격
- 차단 신호(403·429·봇 확인·로그인)는 재시도하지 않고 즉시 중단 → 관리자만 재점검
- 언론사는 기본 저장 범위를 제목·링크·발행일로 제한 (저작권)
- robots.txt에서 AI 봇을 막은 사이트는 `ai_restricted`로 표시하고, 챗봇은 그 사이트 문서의 요약을 LLM에 보내지 않는다 (제목·링크만).
  의미 검색 임베딩도 그 사이트는 제목만 외부 임베딩 API에 보낸다
- 네이버 검색 결과는 표시 전용 — 저장·요약·챗봇·구독 메일에 쓰지 않는다 (법무 확인 전까지)

## 진행 현황

| 단계 | 상태 |
| --- | --- |
| M0 골격·환경 | 완료 |
| M1 하네스·탐색 (자동 탐색, 도메인 간격 공유, 이상 감지, LLM 레시피) | 완료 |
| M2 외부 API·키워드 감시 (API 키·DART, 키워드 감시, 요약·분류, 구독, 화면) | 완료 (2026-10-18) |
| M3 회사 레이어·공급사 리스크 (거래처·위험 신호·케이던시아 목업) | 거의 완료 |
| M4 동적·첨부 | 다음 |
| M5 검색·챗봇 (전문 검색·이슈 묶기, 챗봇 1·2단계 의미 검색) | 진행 중 |
| M6 관리자·운영 (운영 지표·워커 하트비트는 완료) | 일부 |

**남은 것 (핵심)**

1. M3 마무리: DART 재무 신호(FINANCIAL), 회사별 구독 화면, 실제 케이던시아 API 연결(SDS가 API를 주면 주소·매핑만 교체).
2. M4: Playwright 동적 수집, HWP/PDF 첨부 텍스트 추출(공공기관 보도자료 본문), 원문 삭제 감지.
3. M5: 대화 요약·답 피드백, 운영 데이터 임베딩(유료 등급 또는 사내 임베딩 모델 결정).
4. M6: 레시피 수정·미리보기 화면, 설정 백필, 감사 로그 화면. 작은 것: 네이버 API 쿼터 추적, Teams 웹훅 형식.

**코드 밖에서 결정돼야 하는 것** — 안티봇 범위 합의안 서명, 언론 기사 본문 저장 범위와 네이버 저장·분석 전환(법무), 뉴스 저작권 이용 계약,
정부 사이트 본문 저장 기준, 케이던시아 API·데이터 반출 보안 검토, 운영 담당자 ([요건 문서](docs/requirements-mapping.md) 끝 체크리스트).

단계별로 한 일과 실측, 로드맵을 바꾼 배경은 [docs/progress.md](docs/progress.md)에 있습니다.
