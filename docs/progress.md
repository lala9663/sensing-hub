# 진행 기록

단계(M0~M6)별로 무엇을 했고 무엇이 남았는지 적은 기록입니다. 요약은 [README](../README.md)의 "진행 현황", 기능 설명은 [기능 안내](guide/README.md)에 있습니다.

## 단계

| 단계 | 내용 | 상태 |
| --- | --- | --- |
| M0 골격·환경 | 스키마, 등록 API, 스케줄러, 작업 큐, 워커 골격 | 완료 |
| M1 하네스·탐색 | 자동 탐색 고도화, 도메인 단위 간격(워커 간 공유), 이상 감지, 관리자 처리 대기 API, LLM 레시피 생성 | 완료 |
| M2 외부 API·키워드 감시 | API 카탈로그·자격 증명 저장소·API 수집기(뉴스 검색·DART·공공데이터포털), 키워드 감시, 수집 시점 요약·분류, 구독(일간 다이제스트) 알림, 최소 화면(등록·내 요청·관리자 큐) | 완료 (2026-10-18, 최소 화면 포함) |
| M3 회사 레이어·공급사 리스크 소스 | 회사(거래처) 마스터와 별칭 매칭, 문서→회사 연결, 리스크 신호 분류(재무·제재·사고·경영 변동), 공공 API 커넥터(국세청 사업자 상태, 조달청 부정당업자, DART 기업개황·재무), 회사별 구독, 케이던시아 연동(마스터 동기화·이벤트 전달 — 목업으로 형태 완료, 실제 API는 SDS 확인 뒤) | 거의 완료 — 남은 것: DART 재무(FINANCIAL), 회사별 구독 화면, 실제 케이던시아 API(SDS 확인 뒤) |
| M4 동적·첨부 | Playwright 동적 수집, 첨부 다운로드·HWP/PDF 텍스트 추출, 원문 삭제(GONE) 감지, (합의 시) 회사 계정 로그인 세션 | 다음 |
| M5 검색·챗봇 | 인덱싱 작업(청크·Kiwi·임베딩), 하이브리드 검색, 중복 보도 묶기, 챗봇 tool calling(`search_documents`·`list_issues`·`list_watches`·`create_watch`·`register_source`·`get_source_status`), 대화 저장 | 진행 중 — 된 것: 전문 검색 색인(Kiwi·tsvector)과 이슈 묶기·화면, 챗봇 1단계(도구 호출·대화 저장·근거 링크·화면), 챗봇 2단계 의미 검색(조각 임베딩·하이브리드 RRF, 2026-10-19). 남은 것: 대화 요약·답 피드백, 운영 데이터 임베딩(유료 등급 또는 사내 임베딩 모델) |
| M6 관리자·운영 | 레시피 수정·미리보기, 설정 백필, 감사 로그 화면, 운영 지표 | |

로드맵은 현업 요청서(대외협력·커뮤니케이션 대상 외부정보 센싱)에 맞춰 2026-10-16에 다시 짰다. 요건 대응표와
안티봇 범위 합의안은 [requirements-mapping.md](requirements-mapping.md)에 있다. 핵심 변경: 언론 보도·공시는
크롤링보다 공개 API를 우선하고, 두 팀의 업무(키워드로 보고 아침에 받아 보기)에 맞춰 키워드 감시·구독을 챗봇보다 앞에 둔다.
같은 날 현업이 삼성SDS 구매·공급망관리 SaaS **케이던시아(Caidentia)** 를 함께 쓴다는 것이 확인되어, 고객(두 팀)과 엔진은
그대로 두고 **데이터 소스를 공급사 리스크 쪽으로 넓히는** M3을 동적 수집보다 앞에 두었다. 문서 중심(오늘 들어온 문서)에
회사 중심(거래처별 외부 신호)을 한 층 얹는 것이며, 자세한 소스 목록은 요건 문서의 "공급사 리스크 소스" 절에 있다.

## 어디까지 왔고 무엇이 남았나 (2026-10-19)

**된 것** — URL 하나(또는 수백 개 일괄)로 사이트가 자동 등록되고 주기 수집된다. 공개 API(DART·공공데이터포털, 네이버 뉴스는 NAVER API HUB 키가 있을 때 네이버 뉴스 탭 검색·감시 화면 실시간 — 표시 전용)와 키워드 감시(기본은 수집 기사 대조),
수집 시점 요약·분류, 구독, 거래처 마스터와 위험 신호(국세청·DART 공시·조달청), 케이던시아 연동(목업), 링크 모음 페이지의
언론사 자동 확장, 운영 지표, 그리고 이 모든 것을 다루는 브라우저 화면. 실측: 표본 10곳 중 9곳 자동 등록(10-19 재측정 10/10),
주간지 8곳 중 6곳 자동 등록(REVIEW였던 한겨레21·주간한국은 10-19 규칙 보강 뒤 자동 등록), 뉴스스탠드 → 언론사 50곳 자동 등록.
LLM 요약·분류는 10-19 Gemini 무료 등급(`gemini-3.5-flash-lite`)으로 문서 7,967건 전체를 실패 없이 처리([guide/llm.md](guide/llm.md)).
사이트 하나에 기사 종류(상세 주소 모양) 여러 개(10-19): 정책브리핑은 보도자료만 모이던 것을 다시 탐색하면 종류 19개 중 7개(보도자료·정책뉴스·
사실은 이렇습니다·부처 브리핑·국민이 말하는 정책·연설문·청와대 브리핑)를 켜고 실행당 상한을 종류별로 나눠 수집한다(상세에 발행일이 없는 부처 브리핑은 사이트맵 lastmod로 날짜를 단다). 관리자 소스 상세에서 종류를 켜고 끈다.
등록 주소가 목록이면 그 목록의 종류만 켜고, 이미 수집 중인 도메인에 다른 목록 주소를 넣으면 중복 대신 기존 소스에 그 종류를 더하며(설정 새 버전),
공공기관 게시판은 게시판 파라미터(bbsId 등) 값마다 종류로 나눈다.
수집 원리는 [how-collection-works.md](how-collection-works.md).

**10-19에 더한 것** — 세 가지 모두 main에 합쳐 로컬에서 확인했다.

- 과거 기사 채우기(백필): 소스 상세에서 기간(기본 최근 3개월, 등록 담당자 최대 1년·관리자는 더 길게)을 정하면
  기간 묶음(기본 2개월, 보관 사이트맵 단위에 맞춤)으로 나눠 최근 묶음부터 보관 사이트맵(`.xml.gz`, 번호 묶음 이분 탐색) → 목록 페이지 역순으로
  평소 수집이 쉬는 틈에 요청 100건씩 끊어 채운다. 묶음별 상태·수가 보이고, 두 달 더로 늘리고, 양보·취소한다
  ([how-collection-works.md](how-collection-works.md) 6-1절, [guide/09-admin.md](guide/09-admin.md#과거-기사-채우기)). 실측: 동아일보 두 달 묶음 하나 = 주소 21,096개 ≈ 12~15시간, 10년이면 묶음 61개.
- 사이트 하나에 기사 종류 여러 개: korea.kr이 보도자료만 모이던 문제(정책뉴스·브리핑·연설문 누락)를 해결. 종류별 품질 검사·켜고 끄기,
  특정 목록 주소로 등록하면 그 종류만, 이미 있는 사이트에 다른 목록 주소를 넣으면 종류 추가. 다시 탐색 실측: 정책브리핑 19종류 중 7종류 켬
  (보도자료·정책뉴스·사실은 이렇습니다·부처 브리핑·국민이 말하는 정책·연설문·청와대 브리핑), 첫 실행에서 7종류 모두 새 글 수집.
- 챗봇: 1단계 도구 호출형 챗봇(문서 검색·이슈·감시·사이트 등록·상태), 대화 저장, 근거 링크, 챗봇 화면. 2단계 의미 검색 — 워커가 색인 조각을
  임베딩하고 `search_documents`가 낱말 일치와 의미 유사도를 RRF로 합친다 (임베딩이 없거나 실패하면 전문 검색 그대로).

**남은 것 (순서대로)**

1. M3 마무리: DART 재무 신호(FINANCIAL), 회사별 구독 화면, 실제 케이던시아 API 연결(SDS가 API를 주면 base-url·매핑 교체).
2. 관리자 확인으로 가는 사이트 줄이기: ~~한겨레21·주간한국~~ 10-19 일반 규칙으로 해결(비표준 발행일 메타·구분자 없는 날짜, 꼬리 날짜 무시,
   본문을 품은 `<header>` 보존, `[부고]` 단신 표본 제외 — [how-collection-works.md](how-collection-works.md) 4절). 남은 것: 규칙이 못 잡는 다음 사례가
   나오면 LLM 레시피로 붙이는 실측(Gemini 무료 키로 가능). 운영 정책브리핑 소스(id 1)는 관리자 '다시 탐색'으로 기사 종류 설정을 받아야 하고,
   부처 브리핑(`policyBriefingView`)은 상세에 발행일이 없지만 10-19부터 사이트맵 lastmod를 마지막 날짜 힌트로 써서 켜진다. 켜는 종류 상한 기본값도 6→7로 올려 청와대 브리핑까지 켠다.
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

## M1에서 한 일

- [x] 도메인 단위 요청 간격을 워커 여러 대가 공유 (`domain_throttle` 슬롯 예약, `SENSING_SHARED_THROTTLE`)
- [x] 소스별 평소 수집량 대비 급감(`ANOMALY_DROP`) 감지와 2회 연속 시 BROKEN 전환 (기준: 같은 설정 버전의 최근 정상 실행 평균 후보 수, 30% 미만)
- [x] 실패 증거(응답 HTML 일부) 파일 저장과 `evidence_path` 기록 (`SENSING_EVIDENCE_DIR`/날짜/, 30일 보관)
- [x] 관리자용 처리 대기 큐 API (`/api/v1/review-queue`: 승인 대기·REVIEW·BROKEN·BLOCKED를 우선순위순으로)
- [x] 실제 사이트 10곳으로 자동 탐색 성공률 측정 도구 (`sensing-discovery-eval`, 표본 [eval-sites.txt](eval-sites.txt))
- [x] 자동 탐색 성공률 목표 8/10 달성 — 2026-10-16 **9/10** (첫 측정 6/10, [eval-result.json](eval-result.json))
  - 남은 1곳 daily.hankooki.com은 봇 확인 화면(`BOT_CHALLENGE`) → 원칙대로 우회하지 않음
  - 6 → 9로 올린 변경: 홈이 스크립트로 넘기는 사이트 따라가기, 다른 호스트로 넘어가면 그곳을 사이트로 보기,
    영숫자 식별자(`ATCL6fd477428`) 패턴화, 사이트맵 `lastmod` 최신순, 메뉴 이름(보도자료·공지)으로 게시판 찾기,
    후보를 유력한 순서로 품질 검사해 첫 합격 채택, 목록 행의 날짜를 상세에 넘기기, 게시판 제목·등록일·스크립트 첨부 추출,
    자기 하위 페이지로 링크하는 목록 페이지는 기사로 보지 않기, 공공기관은 본문 대신 첨부가 있어도 통과
- [x] 등록 시 LLM 레시피 생성 (`SENSING_LLM_PROVIDER`: Claude 또는 OpenAI 호환 API, 스키마·샘플 품질 검사를 통과한 것만 설정으로 저장, 실패·장애 시 휴리스틱)
- [x] 현업 샘플 기사가 등록된 패턴에 잡히는지 측정 — [eval-sites.txt](eval-sites.txt)에 "사이트 [샘플기사]"로 적으면 결과 표에 `샘플` 열(`sample_match`)이 붙는다.
  섹션 조각 병합(`/news/economy/{n}`+`/news/society/{n}` → `/news/{*}/{n}`)과 숫자 없는 슬러그(`/{slug}/`) 패턴화로
  2026-10-16 매일경제·EE Times·Tom's Hardware 샘플 **3/3 적중** ([eval-samples.txt](eval-samples.txt), [eval-samples-result.json](eval-samples-result.json)), 기존 표본 9/10 유지

## M2에서 한 일

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
- [x] 네이버 뉴스 검색을 NAVER API HUB로 옮기고 기본을 표시 전용으로 (2026-10-19): 주소·헤더(`SENSING_NAVER_API` hub|developers),
  401·403·429·SE 오류 코드를 알기 쉬운 문장으로, `SENSING_NAVER_MODE`(display 기본 | store | off). 표시 전용은 감시 화면 "네이버 실시간"에서
  백엔드가 실시간으로 불러와 제목·언론사·시각·링크만 보여 주고 DB·LLM·챗봇·구독 메일에 넣지 않는다(워커도 수집하지 않는다). 관리자 화면
  Client ID·Secret 입력과 시험 호출(`POST /api/v1/api-credentials/naver_news/verify`)
- [x] 네이버 뉴스 탭 (2026-10-19): 왼쪽 메뉴 "네이버 뉴스"(`#/naver?q=..`)에서 감시와 무관한 검색어로 네이버 뉴스를 검색해 보여 준다
  (`GET /api/v1/naver/news`, 최신순·정확도순, 요약 문장, 더 보기 1,000번째까지, 내 감시 키워드 칩). 표시 전용 그대로 — 저장·캐싱·LLM 없음,
  `Cache-Control: no-store`, 감사 로그에는 검색어만. VIEWER 이상, 사용자별 초당 2회·display 50 상한. 키가 없거나 모드가 off면 안내만
- [x] 감시 목록 정리·공용 감시 (2026-10-20): 관리자가 만든 감시는 공용(누구나 읽기 전용: 목록·키워드별 기사·네이버 실시간, 수정·제외는 관리자만),
  목록은 공용 먼저 → 내 감시. 제외(EXCLUDED)한 감시는 목록에서 빼고 관리자는 "제외된 감시 보기"(`include_excluded`)와 "다시 켜기"
  (`POST /api/v1/watches/{id}/reactivate`, 모드 규칙대로, 감사 로그). 감시를 주요 키워드 9개로 바꾸고 예전 감시 12개는 제외
- [x] 수집 시점 요약·분류: 워커 보강 단계(`enrich.py`, 큐가 비었을 때 묶음 10건을 LLM 한 번에, `SENSING_ENRICH_*`)가
  `document_enrichment`(V6)에 토픽·태그·요약을 두고, `GET /api/v1/documents`(since·topic·keyword·source_id 필터, 상세, 토픽별 건수)로
  읽는다. 언론 기사는 제목만 분류하고 요약을 저장하지 않으며, LLM 장애·검증 실패는 수집에 번지지 않는다
- [x] 구독 알림: 구독(`digest_subscription`, V7, `/api/v1/digests`: 감시·소스·토픽 필터, `hourKst`, EMAIL 또는 WEBHOOK)을
  스케줄러가 매일 그 시각(KST)에 처리해 지난 발송 이후 새로 수집된 문서를 감시별(검색어별) → 토픽별 → 분류 전으로 묶어
  메일(HTML, `SENSING_MAIL_*`) 또는 웹훅(`{"text":...}`)으로 보낸다. 미리보기·즉시 전송 API, 보낸 본문과 결과(`SENT`·`EMPTY`·`FAILED`)는
  `digest_run`에 남고, 구독 하나의 실패는 다른 구독을 막지 않는다
- [x] 최소 화면: 빌드 도구·CDN 없는 정적 HTML·JS(`backend/src/main/resources/static/`, 해시 라우팅, 상단 사용자 선택 → `X-User`,
  `GET /api/v1/me`로 권한 확인해 관리자 메뉴 숨김). 등록·내 요청(일괄 등록 묶음 진행 포함)·문서·거래처·키워드 감시·구독(미리보기·즉시 전송)와
  관리자 처리 대기 큐(항목별 가능한 조치만)·소스(건강 상태·상세·설정 버전·주기 잠금)·운영 지표·API 키
- [ ] 네이버 API 쿼터 추적, Teams 등 다른 웹훅 형식

## M3에서 한 일

- [x] 회사 마스터·문서→회사 연결·국세청 사업자 상태 신호: `company`·`document_company`·`company_signal`(V8), `/api/v1/companies`(등록·import upsert·
  목록·상세·수정·보관)와 `/api/v1/company-signals`, 회사 감시(`회사:<이름>`, 360분)로 걸린 기사와 보강 태그 정확 일치로 연결, 사건·사고·인사·조직
  기사는 NEWS 신호, 점검 소스(`kind = CHECK`, 하루 1회)가 공공데이터포털(`data_go_kr`) 국세청 사업자등록 상태조회로 폐업·휴업·미등록·과세유형
  변경을 BIZ_STATUS 신호로 기록(fingerprint로 중복 없음), 구독 메일 맨 앞 "거래처 신호" 절과 `companyIds` 필터
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
- [ ] DART 기업개황·재무(FINANCIAL), 회사별 구독 화면, 케이던시아 실제 API 연결(SDS 확인 뒤)

## M5에서 한 일

- [x] 전문 검색 인덱스(Kiwi·tsvector)와 이슈 묶기: 워커가 큐가 빌 때(LLM과 무관, `SENSING_INDEX_BATCH`=200) 문서를 조각(`chunk`: 제목 seq 0,
  본문 800자 안팎 최대 20조각, 언론 기사는 제목만)으로 나눠 미리 뽑은 낱말을 `to_tsvector('simple', ...)`로 넣는다(`textindex.py`, 선택 의존성 `nlp`의
  Kiwi 형태소 분석, 없으면 조사를 떼는 정규식, `index_version` `fts-kiwi-v1`/`fts-regex-v1`). 색인 직후 같은 사건 기사를 제목 낱말 겹침으로
  이슈에 묶는다(`issues.py`, `issue`·`issue_document` V11, 72시간 창, 겹침 계수 0.4·겹친 낱말 2개 이상, 2개뿐이면 0.66). `GET /api/v1/documents?q=`는
  색인 낱말 접두 일치(모든 낱말, 조사 뗌, 색인 전 문서는 부분 일치), `GET /api/v1/issues`(언론사 수 → 기사 수 순, `since`·`min_docs`·`min_sources`·`q`)와
  상세, 화면 "이슈"(기간·언론사 수 칩, 검색, 펼쳐 보는 소속 기사)와 문서 화면의 "이슈 #id (N건)" 링크. 이미 쌓인 문서는 `sensing-index-backfill`로 한 번에.
  기준값은 운영 DB 제목 2,998건(53개 언론사)으로 보정했다 → 언론사 2곳 이상 이슈 287개, 3곳 이상 122개, 5곳 이상 47개 ([decisions.md](decisions.md))
- [x] 챗봇 1단계 (2026-10-19): 백엔드 LLM 클라이언트(`io.sensinghub.llm`: OpenAI 호환 `chat/completions` + tools, Claude Messages API + tools,
  `java.net.http`, 워커와 같은 `SENSING_LLM_*`·`ANTHROPIC_*`, 429·5xx·시간 초과를 사용자 문장으로), 챗봇 서비스·API(`/api/v1/chat/sessions`, 한 턴 도구 왕복 4번,
  모든 단계 `chat_message` 저장, 답의 `[n]` → `chat_citation`, 세션 소유자만), 도구 6개(문서 검색·이슈·감시 조회/생성·사이트 등록·진행 상태 — 화면과 같은 서비스),
  API 키는 채팅으로 받지 않고 붙여 넣은 키는 가림, 화면 "챗봇"(대화 목록·생각 중·근거 링크, 좁은 화면). 가짜 LLM 서버로 도구 루프·권한·저장·인용·429 통합 테스트
- [x] 챗봇 2단계 의미 검색 (2026-10-19): 워커 임베딩 단계(`embed.py`: 색인 조각 → OpenAI 호환 `/embeddings`, 언론 기사는 제목만·공공 문서는 제목+본문 조각,
  AI 봇 차단 사이트는 제목만, `chunk.embed_model`로 모델 구분(V15), 분당 상한·실패 시 쉬기, `sensing-embed-backfill`), 백엔드 질의 임베딩 클라이언트
  (`LlmHttp`와 같은 오류 분류)와 하이브리드 검색(`HybridSearch`: 전문 검색 + 코사인 유사도 하한 0.58, RRF k=60, 미설정·조각 없음·실패면 전문 검색),
  `search_documents`가 하이브리드를 쓰고 문서마다 `match` 표시. 가짜 임베딩 서버로 단위·통합 테스트, Gemini 실측(제목 60건·질의 7개)
- [x] 챗봇 떠 있는 대화창 (2026-10-19): 모든 화면 오른쪽 아래 버튼 → 화면 위 대화창(화면 이동에도 대화 유지, 좁은 화면은 전체), 크게 보기(전체 화면),
  새 창으로(`chat.html`), 열림 상태·사용자별 마지막 대화 기억, Esc·포커스 이동. 세 화면이 메시지 그리기·보내기 부품(`chat.js`)을 함께 쓴다 (백엔드 변경 없음)
- [ ] 대화 요약·답 피드백, 운영 데이터 임베딩 (유료 등급 또는 사내 임베딩 모델)
