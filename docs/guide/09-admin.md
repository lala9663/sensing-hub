# 관리자

왼쪽 메뉴 **관리자** 아래의 화면들입니다. ADMIN에게만 보이고, 다른 사용자가 주소로 들어오면 "관리자만 볼 수 있는 화면"으로 막습니다.

| 메뉴 | 주소 | 이 문서의 절 |
| --- | --- | --- |
| 처리 대기 큐 | `#/admin/queue` | [처리 대기 큐](#처리-대기-큐) |
| 소스 | `#/admin/sources`, `#/admin/sources/{id}` | [소스](#소스), [과거 기사 채우기](#과거-기사-채우기) |
| 운영 지표 | `#/admin/ops` | [운영 지표](#운영-지표) |
| API 키 | `#/admin/credentials` | [API 키](#api-키) |
| 주요 키워드 | `#/admin/keywords` | [06-naver-news.md의 주요 키워드](06-naver-news.md#주요-키워드-빠른-검색-칩) |
| 케이던시아 연동 | `#/admin/caidentia` | [04-companies.md의 케이던시아 연동](04-companies.md#케이던시아-연동-목업) |

## 이 탭에서 하는 일

- **처리 대기 큐**: 종류별 건수와, 항목마다 사유·메시지·대기 시간, 그 항목에 가능한 조치만(승인 / 반려 사유 / API 키 등록 / 재점검 / 다시 탐색 / 일시정지 / 제외).
- **소스**: "목록"(상태별 건수 칩으로 거름)과 "건강 상태"(마지막 성공이 오래된 순, 지연 소스 강조) 탭.
- **소스 상세**: 도메인·유형·방식(AI 봇 제한 사이트 표시)·상태 사유·활성 설정 버전·마지막 성공/다음 실행·연속 실패/이상·문서 수("문서 보기")와
  - 상태에 맞는 조치 단추(일시정지 · 재개 · 지금 수집 · 재점검 · 다시 탐색 · 제외, 제외된 감시는 다시 켜기),
  - **수집 주기** 바꾸기("저장 (잠금)" — 사람이 정한 주기는 잠겨 자동 조정이 덮지 않음, "자동 조정에 맡기기"로 풀기),
  - **기사 종류** 목록과 켜고 끄기(설정 새 버전), **최근 작업**·**최근 오류**, **설정 버전**(이전 버전 활성화 = 롤백),
  - 사이트 소스면 **과거 기사 채우기** 패널.
- **운영 지표**: 카드로 소스 상태·처리 대기 큐·작업 큐 적체·처리량(워커 부족 경고)·24시간 작업 결과·24시간 새 문서(수집량 상위)·24시간 실패 코드·워커·
  **LLM 사용량 (오늘)**. 30초마다 갱신합니다.
- **API 키**: 제공자(`dart` · `naver_news` · `data_go_kr`)·키·메모를 등록하고(naver_news는 NAVER API HUB Client ID·Client Secret 두 칸과 콘솔 경로 안내),
  저장 뒤 이 키를 기다리다 다시 진행된 소스 수를 보여 줍니다. 등록된 키 목록(비밀 값은 보이지 않음)·삭제, naver_news 시험 호출.
- **주요 키워드**·**케이던시아 연동**은 위 표의 문서에 있습니다.

## 어떻게 동작하나

### 처리 대기 큐

자동으로 판단하지 못한 일만 사람에게 옵니다. 순서는 승인 대기(VIEWER의 등록·감시) → `API_KEY`(키 발급 필요) → `REVIEW`(자동 탐색·품질 검사 실패) →
`BROKEN`(0건·급감) → `BLOCKED`(403·429·봇 확인·로그인)이고, 같은 종류 안에서는 오래 기다린 순입니다. 조치는 항목 종류마다 가능한 것만 보입니다.
차단 신호는 재시도하지 않고 즉시 멈추며 관리자만 재점검합니다.

### 소스

- **소스 상태**: `PENDING → ACTIVE ⇄ PAUSED`, `ACTIVE → BROKEN(0건·급감) → 수정 → ACTIVE`, `ACTIVE → BLOCKED(403·429·챌린지) → 관리자 재점검 → ACTIVE 또는 EXCLUDED`.
- **급감 감지**: 기사 후보 수가 평소(같은 설정 버전의 최근 정상 실행 평균)의 30% 미만이면 이상(`ANOMALY_DROP`)으로 보고, 2회 연속이면 BROKEN으로 바꿉니다.
- **실패 증거**: 실패한 응답 HTML 일부를 파일로 남기고 `evidence_path`에 적습니다(`SENSING_EVIDENCE_DIR`/날짜/, 30일 보관).
- **도메인 간격**: 같은 도메인에는 최소 2초 간격으로 요청하고, 워커 여러 대가 DB(`domain_throttle` 슬롯 예약)로 간격을 공유합니다.
- **주기 자동 조정**: 워커가 최근 10회 주기 수집의 새 문서 평균이 0.5건 미만이면 주기를 두 배로(최대 1440분), 실행당 상한의 절반을 넘으면 사이트 유형
  기본값으로 되돌립니다. 변경은 로그와 `crawl_job.note`에 남고, 사람이 정한 주기(`interval_locked`)는 건드리지 않습니다.
- **설정 버전**: 설정은 버전으로 쌓고 포인터로 전환하므로, 사이트 개편 대응 중 실수해도 이전 버전 활성화로 바로 롤백합니다. 기사 종류 켜고 끄기와
  감시 키워드 수정도 새 버전입니다.
- 수집 원리(입구 찾기·URL 서명·품질 검사·주기 수집)는 [../how-collection-works.md](../how-collection-works.md).

### 과거 기사 채우기

평소 수집은 입구에 지금 보이는 기사만 모읍니다(RSS 50건 안팎, 뉴스 사이트맵 이틀 안팎). 등록 전 기사는 소스 상세의 **과거 기사 채우기**에서
기간을 정해 요청합니다(기본 최근 3개월, ACTIVE 사이트 소스만, 소스당 하나씩). 화면은 관리자 메뉴에 있고, 등록 담당자(REGISTRAR)는 같은 기능을 API로 씁니다.
등록 담당자는 최대 1년, 관리자는 10년처럼 더 길게(1990년부터) 정할 수 있습니다.

- 기간은 **기간 묶음**(기본 2개월, 보관 사이트맵이 한 달·두 달·석 달 파일이면 그 단위 — 동아일보는 두 달 파일 하나가 묶음 하나)으로 나눠 **가장 최근 묶음부터**
  과거로 채우고, 시작일은 묶음 첫날로 맞춥니다(07-19 → 07-01). 묶음마다 상태와 수가 남아 "2026-09~10 완료 21,096건 · 2026-07~08 진행 40%"처럼 보입니다.
  끊겨도 남은 묶음부터 잇고, **두 달 더**로 묶음을 뒤로 늘립니다(끝난 채우기는 다시 엶). 취소하면 다음 기사 전에 멈춥니다.
- 워커는 묶음마다 ① 보관 사이트맵(이름의 날짜가 걸치는 파일, 번호만 다른 파일 묶음은 이분 탐색, `.xml.gz` 포함) → ② 목록 페이지를 쪽 번호로
  거꾸로(묶음 사이에 이어서) 넘기는 순서로 주소를 모으고, 평소 수집과 같은 기사 판정·추출·저장(언론사는 제목·링크·발행일만)·감시 대조를 합니다.
  글의 날짜는 주소 안 날짜·발행일을 먼저 보고 `lastmod`(나중 수정일이 섞인다)는 상한으로만 씁니다.
- 큐가 빌 때만 다음 작업(`crawl_job` BACKFILL, 요청 100건씩)을 넣고, 작업 도중에도 기사마다 확인해 평소 작업·이 소스의 주기 수집 차례에 양보합니다.
- 이미 있는 문서는 열지 않고, 새 문서의 수집 시각은 발행 시각으로 둡니다(구독 메일·"오늘" 필터에 옛 기사가 끼지 않게).
- 속도: 도메인당 2초 간격이라 동아일보 두 달 묶음 하나(주소 21,096개)가 약 12~15시간, 10년이면 묶음 61개입니다.
  원리·한계는 [../how-collection-works.md](../how-collection-works.md) 6-1절, 사이트별 가능 범위는 [../archive-sitemap-survey.md](../archive-sitemap-survey.md).

### API 키

언론 보도·공시처럼 공개 API가 있는 제공처는 크롤링하지 않습니다. 워커의 API 카탈로그(`apis/catalog.py`, 첫 항목 DART)에 있는 도메인을 등록하면 사전 점검 없이
**"API 키 발급 필요"** 로 관리자 큐(`kind=API_KEY`)에 들어가고, 관리자가 키를 넣으면 시험 호출로 검증한 뒤 `method: API` 설정으로 첫 수집과 주기 수집(6시간)을
자동으로 시작합니다. 거래처 점검 소스(국세청·조달청 `data_go_kr`, DART `dart`)도 같은 큐에서 키를 기다립니다([04-companies.md](04-companies.md)).

- 키 발급은 사람이 합니다(자동 가입·약관 동의 대행 없음).
- 키는 pgcrypto로 암호화해 `api_credential`에만 두고 응답·로그·오류 메시지·증거 파일 어디에도 남기지 않습니다. 챗봇은 키를 채팅으로 받지 않습니다.
- 네이버(`naver_news`) 키를 넣고 확인하는 순서는 [06-naver-news.md](06-naver-news.md#키를-넣고-확인하는-순서).

### 운영 지표

`GET /api/v1/ops/overview` 하나로 소스 상태별 수·처리 대기 큐·작업 큐 적체·24시간 결과·수집량 상위 10·실패 코드 상위 10·처리량(`demandRatio`>1이면 워커 부족)·
워커 목록을 그립니다. 워커는 30초마다 생존 신호(`worker_heartbeat`)를 남기고, 2분 넘게 신호가 없으면 멈춘 것으로 봅니다.
"LLM 사용량 (오늘)" 카드는 쉬는 중인 모델과 풀리는 시각, 요약 상한 대비 호출 수, 모델·용도별 호출·429·토큰을 보여 줍니다([llm.md](llm.md#llm-무료-한도-나눠-쓰기)).

## 설정

| 변수 | 기본값 | 설명 |
| --- | --- | --- |
| `SENSING_SECRET_KEY` | (없음) | 백엔드·워커 공통. API 키를 암호화·복호화하는 대칭키(`pgp_sym_encrypt`). 비우면 백엔드 자격 증명 API는 503, 워커는 API 소스를 관리자 큐로 보낸다 |
| `SENSING_MAX_API_ITEMS` | `500` | 워커. API 소스 실행 1회당 받을 항목 수 상한 |
| `SENSING_MIN_INTERVAL` / `SENSING_SHARED_THROTTLE` | `2` / `true` | 워커. 같은 도메인 요청 최소 간격(초), 그 간격을 DB로 워커끼리 공유 |
| `SENSING_ANOMALY_RATIO` / `SENSING_ANOMALY_BROKEN_AFTER` | `0.3` / `2` | 워커. 평소 대비 이 비율 미만이면 이상, 이 횟수 연속이면 BROKEN |
| `SENSING_EVIDENCE_DIR` / `SENSING_EVIDENCE_DAYS` | `./data/evidence` / `30` | 워커. 실패 증거 저장 위치와 보관 일수 |
| `SENSING_ADAPTIVE_INTERVAL` | `true` | 워커. 주기 자동 조정 (`SENSING_ADAPTIVE_WINDOW`=10회, `SENSING_ADAPTIVE_MIN_NEW`=0.5건, `SENSING_ADAPTIVE_MAX_INTERVAL`=1440분) |
| `SENSING_HEARTBEAT_SECONDS` | `30` | 워커. 생존 신호 간격 (운영 지표는 2분 안의 신호를 살아 있다고 센다) |
| `SENSING_BACKFILL_MAX_DAYS` (백엔드) | `366` | 등록 담당자가 요청할 수 있는 과거 기사 채우기 기간 길이 상한(일). 관리자는 넘길 수 있다(1990년부터) |
| `SENSING_BACKFILL_SEGMENT_MONTHS` (백엔드) | `2` | 기간 묶음 기본 길이(개월). 워커가 보관 사이트맵 단위(1~3개월 파일)를 보면 그 단위로 바꾼다 |
| `SENSING_BACKFILL_ENABLED` | `true` | 워커. 큐가 빌 때 진행 중인 채우기의 다음 작업(`crawl_job` BACKFILL)을 넣는다. 다른 작업이 하나라도 대기하면 넣지 않고, 작업 도중에도 기사마다 확인해 평소 작업·이 소스의 주기 수집 차례에 양보한다 |
| `SENSING_BACKFILL_CHUNK` | `100` | 워커. 작업 하나에 보내는 요청 수(사이트맵·목록·기사·robots 포함). 작업마다 진행 위치를 남기고 끝난다 |
| `SENSING_BACKFILL_MAX_SITEMAPS` | `60` | 워커. 채우기 하나가 읽는, 이름에 날짜가 없는 사이트맵 파일 수 상한 (날짜 있는 파일·번호 묶음 이분 탐색은 묶음에 필요한 만큼) |
| `SENSING_BACKFILL_MAX_PAGES` | `300` | 워커. 목록 페이지를 거꾸로 넘길 때 허브 하나에서 볼 쪽 수 상한 |

## API

```bash
# 소스 목록·상세·조치
curl http://127.0.0.1:8080/api/v1/sources -H 'X-User: admin'
curl http://127.0.0.1:8080/api/v1/sources/1 -H 'X-User: admin'
curl -X POST http://127.0.0.1:8080/api/v1/sources/1/actions/recheck -H 'X-User: admin'   # pause|resume|run|recheck|rediscover|exclude
curl -X POST http://127.0.0.1:8080/api/v1/sources/1/configs/2/activate -H 'X-User: admin'  # 설정 버전 롤백
# 사람이 정한 주기는 잠겨(intervalLocked) 워커의 자동 조정이 덮지 않는다. {"intervalLocked":false}로 풀기
curl -X PUT http://127.0.0.1:8080/api/v1/sources/1 -H 'X-User: admin' -H 'Content-Type: application/json' -d '{"intervalMinutes":120}'
# 기사 종류 켜고 끄기 = 설정 새 버전 (상세의 kinds·kindsEditable)
curl -X PUT http://127.0.0.1:8080/api/v1/sources/1/kinds -H 'X-User: admin' -H 'Content-Type: application/json' \
     -d '{"signature":"/news/policyNewsView.do?newsId={n}","enabled":false}'

# 처리 대기 큐 (승인 대기 → API_KEY → REVIEW → BROKEN → BLOCKED, 오래 기다린 순). kind로 거를 수 있다
curl http://127.0.0.1:8080/api/v1/review-queue -H 'X-User: admin'
curl http://127.0.0.1:8080/api/v1/review-queue/counts -H 'X-User: admin'
curl -X POST http://127.0.0.1:8080/api/v1/registrations/3/approve -H 'X-User: admin'
curl -X POST http://127.0.0.1:8080/api/v1/registrations/3/reject -H 'X-User: admin' -H 'Content-Type: application/json' -d '{"reason":"업무와 관련 없는 사이트"}'

# 운영 지표: 소스 상태별·처리 대기 큐·작업 큐 적체·24시간 결과·수집량·실패 코드·처리량(demandRatio>1이면 워커 부족)·워커·LLM 사용량
curl http://127.0.0.1:8080/api/v1/ops/overview -H 'X-User: admin'
curl http://127.0.0.1:8080/api/v1/ops/sources/health -H 'X-User: admin'   # 조용히 멈춘 소스 순 (overdue)

# 과거 기사 채우기 (REGISTRAR 이상, ACTIVE 사이트 소스만, 소스당 하나씩). 본문을 비우면 최근 3개월, 시작은 묶음 첫날로 맞춤
curl -X POST http://127.0.0.1:8080/api/v1/sources/1/backfills -H 'X-User: registrar' -H 'Content-Type: application/json' -d '{"from":"2026-07-19","to":"2026-10-19"}'
curl -X POST http://127.0.0.1:8080/api/v1/sources/1/backfills -H 'X-User: admin' -H 'Content-Type: application/json' -d '{"from":"2016-10-19"}'   # 10년 (관리자)
# 진행률: found·saved·skipped·outOfRange·failed·remaining·requests·chunks·unitMonths·segments(label·status·found·saved·percent)·message
curl http://127.0.0.1:8080/api/v1/sources/1/backfills -H 'X-User: registrar'
curl -X POST http://127.0.0.1:8080/api/v1/sources/1/backfills/1/extend -H 'X-User: registrar' -H 'Content-Type: application/json' -d '{"segments":1}'   # 두 달 더
curl -X POST http://127.0.0.1:8080/api/v1/sources/1/backfills/1/cancel -H 'X-User: registrar'

# 공개 API 키 등록 (암호화 저장 후 이 키를 기다리던 소스가 자동으로 다시 진행된다) · 조회(비밀 없음) · 삭제
curl -X POST http://127.0.0.1:8080/api/v1/api-credentials -H 'X-User: admin' -H 'Content-Type: application/json' -d '{"provider":"dart","secret":"<발급받은 키>","label":"공시 API"}'
curl -X POST http://127.0.0.1:8080/api/v1/api-credentials -H 'X-User: admin' -H 'Content-Type: application/json' -d '{"provider":"data_go_kr","secret":"<공공데이터포털 서비스키>","label":"공공데이터포털"}'
curl http://127.0.0.1:8080/api/v1/api-credentials -H 'X-User: admin'
curl -X DELETE http://127.0.0.1:8080/api/v1/api-credentials/dart -H 'X-User: admin'
```

네이버 키 등록·시험 호출 예시는 [06-naver-news.md](06-naver-news.md#api)에 있습니다.

## 한계와 주의

- 과거 기사 채우기는 느립니다(사이트 하나에 시간당 약 1,400~1,800건). 여러 언론사의 긴 기간이 필요하면 뉴스 DB 계약(빅카인즈 등)이 더 빠릅니다.
- 보관 사이트맵이 없거나 목록이 짧은 사이트는 과거 기사를 채울 수 없는 범위가 있습니다([../archive-sitemap-survey.md](../archive-sitemap-survey.md)).
- 남은 것(M6): 레시피 수정·미리보기 화면, 설정 백필, 감사 로그 화면. 네이버 API 쿼터 추적.
- 키 값은 관리자 화면(또는 API)에만 넣고 채팅·파일·커밋에 붙이지 않습니다.
