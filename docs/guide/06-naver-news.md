# 네이버 뉴스

메뉴 **네이버 뉴스** (`#/naver`). 감시와 무관하게 아무 검색어로나 네이버 뉴스를 찾아보는 화면입니다. 권한은 VIEWER 이상입니다.
네이버 결과는 **표시 전용**입니다 — 화면에 보여 주기만 하고 저장·요약·챗봇·구독 메일에 쓰지 않습니다.
같은 표시 전용 원칙으로 키워드 감시 화면의 "네이버 실시간"과 챗봇의 `네이버 - 검색어`도 동작합니다.

## 이 탭에서 하는 일

- 검색창에 검색어를 넣고 Enter(또는 "검색"). 정렬은 **최신순**(`sort=date`, 기본)·**정확도순**(`sim`).
- 처음 들어오면 빈 검색창과 **주요 키워드** 빠른 검색 칩이 나옵니다 — 칩을 누르면 그 칩의 검색어로 찾습니다(관리자에게는 "주요 키워드 편집" 링크).
- 결과마다 제목(누르면 언론사 원문이 새 탭에서 열림), 언론사(원문 주소의 도메인 — 등록된 사이트면 그 이름), 발행 시각, 요약 문장(네이버 description의
  `<b>` 태그를 지우고 엔티티를 푼 것, 검색 낱말은 굵게), **네이버에서 보기** 링크. 결과 총수와 20건씩 **더 보기**(네이버 API가 허용하는 1,000번째까지).
- 검색어·정렬은 주소에 남습니다(`#/naver?q=반도체%20수출&sort=sim`). 새로 고침하거나 주소를 공유하면 같은 검색을 다시 불러옵니다(결과는 그때 새로 부름).
- 화면에 "NAVER 검색 결과" 출처와 "저장하지 않습니다" 안내가 붙습니다.
- 키가 없거나(`NAVER_KEY_REQUIRED`) INVALID이거나 모드가 `off`(`NAVER_OFF`)면 메뉴는 보이지만 화면은 안내만 보여 줍니다(관리자에게는 API 키 화면 링크).

### 감시 화면의 네이버 실시간

키워드 감시 화면([05-watches.md](05-watches.md))의 감시 행에서 **"네이버 실시간"** 을 누르면 백엔드가 그 감시 키워드(칩으로 바꿔 가며)로 네이버 최신 뉴스
(날짜순 20건, "더 보기")를 대신 불러와 **제목·언론사·시각·원문/네이버 링크**만 보여 줍니다(출처 "NAVER 검색 결과", 요약 문구는 보여 주지 않음,
감시 제외어가 제목에 있으면 숨김). 공용 감시는 누구나, 내 감시는 만든 사람이, ADMIN은 전체를 볼 수 있습니다.
키가 없거나 INVALID이거나 모드가 `off`면 이 단추는 숨겨집니다. 감시의 "문서" 수는 늘지 않아야 정상입니다.

### 주요 키워드 (빠른 검색 칩)

네이버 뉴스 탭의 빠른 검색 칩과 챗봇 시작 화면의 칩에 쓰는 **짧은 목록**입니다. 키워드 감시(`source` WATCH)·회사 감시와는 따로 두며, 바꿔도 감시에는
영향이 없습니다. 기본값(V18 `featured_keyword`)은 사용자가 정한 순서 그대로 9개입니다:
**반도체 · 메타넷 · 메타넷 글로벌 · 삼성전자 · DS · DX · DSR · 테슬라 · 하이닉스**(영문은 대문자로 표시).

- 칩마다 **칩 이름**(`label`, 30자까지)과 **검색어**(`query`, 100자까지, 비우면 이름과 같음)가 있습니다. 칩을 누르면 검색어로 찾고, 챗봇에 칩 이름을
  그대로 쳐도(대소문자 무시) 검색어로 찾습니다.
- 관리자 메뉴 **주요 키워드**(`#/admin/keywords`): 표에서 이름·검색어를 고치고 ▲▼로 순서를 바꾸고 추가·삭제한 뒤 **저장**하면 목록 전체가 그 순서로
  바뀝니다(20개까지, 같은 이름은 대소문자만 달라도 거절). 바뀌기 전·뒤 목록이 감사 로그(`FEATURED_KEYWORDS_UPDATE`, target `featured_keyword`)에 남습니다.
- 예전에는 키워드 감시의 키워드 전체를 칩으로 썼는데 관리자에게는 감시 12개·키워드 22개(목업 회사 키워드 포함)가 다 보여 너무 많아 바꿨습니다.

## 어떻게 동작하나

- 네이버 개발자센터 검색 API는 2026-07-31 신규 발급이 끝나(기존 키 2027-06-30 지원 종료) 네이버클라우드 **NAVER API HUB**로 연결합니다
  (`GET https://naverapihub.apigw.ntruss.com/search/v1/news?query=..&display=..&start=..&sort=date&format=json`, 헤더 `X-NCP-APIGW-API-KEY-ID`(Client ID)·
  `X-NCP-APIGW-API-KEY`(Client Secret), HTTPS만). 요청·응답 형식은 [../naver-news-api.md](../naver-news-api.md).
- 2026-09-07 시행 개정 약관이 검색 결과의 AI 입력·학습, 복사·저장·캐싱, 제3자 제공, 광고 수익화를 금지하므로 기본 사용 방식은 **표시 전용**
  (`SENSING_NAVER_MODE=display`)입니다.
  - 결과는 `document`·`document_watch`·임베딩·요약·구독 메일·챗봇 어디에도 넣지 않고, 서버 메모리 캐시도 두지 않으며 응답은 `Cache-Control: no-store`입니다.
  - DB에 남는 것은 키 상태(`api_credential.status`)와, 네이버 뉴스 탭 검색의 **감사 로그**(`audit_log` action `NAVER_SEARCH`: 사용자·검색어·정렬·start·display,
    결과·건수는 남기지 않음)뿐입니다.
  - 키를 넣어도 감시에 저장 수집을 붙이지 않고(`attachedWatches` 0), 워커도 네이버 모드 감시를 수집하지 않습니다(백엔드·워커 이중 차단).
- **저장·분석**(`store`, **법무 확인 뒤에만**): 예전 동작입니다. 쓸 수 있는 키가 있으면 감시를 만들 때 네이버 검색도 함께 붙여(화면 "네이버 검색 함께")
  60분(회사 감시 360분)마다 검색어별 최신 기사 제목·링크·발행일을 `document`에 저장합니다(커넥터 `apis/naver_news.py`: 검색어별 `sort=date` 최대 3페이지,
  제목 태그 제거). 키를 나중에 넣으면 대조 모드 감시에 네이버 검색을 붙이고(`attachedWatches`), 키를 지우면 다시 대조 모드로 되돌립니다.
  백엔드와 워커에 같은 값을 줘야 합니다. 전환 절차는 [../naver-news-api.md](../naver-news-api.md) 6절.
- **남용 방지**: 사용자마다 초당 2회(`SENSING_NAVER_SEARCH_PER_SECOND`), 한 번에 최대 50건(기본 20건), 검색어 100자까지. 너무 빠르면
  429 `NAVER_TOO_FAST`(네이버 하루 한도 초과는 429 `NAVER_RATE_LIMITED`로 따로 알림).
- **오류는 알기 쉬운 문장으로**: 401·403 → 키 문제(키를 INVALID로 표시, "Client ID·Client Secret, Application에 '검색' 선택 확인"),
  429 → 하루 허용량 초과, 400 `SE01`~`SE04`·`SE06`/404 `SE05` → 요청·주소 오류(어느 값이 틀렸는지), 500 `SE99` → 네이버 서버 오류.
- **한도**: 검색 묶음 하루 25,000회(월 775,000회 표기도 있음), 키당 50 RPS, 현재 한시 무료(종량제, 유료 전환은 사전 공지).
- 결정 배경은 [../decisions.md](../decisions.md).

### 키를 넣고 확인하는 순서

키 값은 채팅·파일·커밋에 붙이지 않고 관리자 화면에만 넣습니다.

1. 네이버클라우드 콘솔 → Services → Application Service → **NAVER API HUB** → 이용 신청 → **Application 등록**에서 API 선택에 **검색**을 고르고
   저장한 뒤, 인증 정보의 **Client ID**·**Client Secret**을 확인합니다 (결제 수단 등록 필요).
2. sensing-hub 관리자 화면 → **API 키** → 제공자 `naver_news — 네이버 뉴스 검색 (NAVER API HUB)` → Client ID·Client Secret 두 칸에 넣고 저장합니다
   (서버에는 `Client ID:Client Secret` 한 문자열로 암호화 저장).
3. 저장하면 화면이 바로 **시험 호출**(검색어 "뉴스" 1건)을 합니다. 상태가 `VALID`면 성공, `INVALID`면 "마지막 오류"의 안내(Client ID·Secret, Application의 검색 선택)를
   확인합니다. 등록된 키 목록의 **시험 호출** 버튼으로 다시 확인할 수 있습니다.
4. 왼쪽 메뉴 **네이버 뉴스**에서 검색하거나, **키워드 감시** 화면에서 감시 행의 **네이버 실시간**을 눌러 최신 뉴스가 보이는지 확인합니다.
   감시의 "문서" 수는 늘지 않아야 정상입니다(표시 전용).

## 설정

| 변수 | 기본값 | 설명 |
| --- | --- | --- |
| `SENSING_NAVER_MODE` | `display` | 백엔드·워커 공통(같은 값). `display` 표시 전용(결과를 저장·요약·챗봇·구독 메일에 쓰지 않고 네이버 뉴스 탭·감시 화면에서 실시간으로 보여 주기만), `store` 저장·분석(**법무 확인 뒤에만**, 감시에 네이버 주기 수집을 붙여 `document`에 저장), `off` 쓰지 않음. 모르는 값은 `display` |
| `SENSING_NAVER_API` | `hub` | 백엔드·워커 공통. `hub` = NAVER API HUB(`https://naverapihub.apigw.ntruss.com/search/v1/news`, 헤더 `X-NCP-APIGW-API-KEY-ID`/`X-NCP-APIGW-API-KEY`), `developers` = 예전 개발자센터(`openapi.naver.com/v1/search/news.json`, 2027-06-30 지원 종료) |
| `SENSING_NAVER_SEARCH_URL` | (없음) | 백엔드·워커. 검색 주소만 바꾼다(테스트·사내 프록시). 헤더는 `SENSING_NAVER_API`를 따른다 |
| `SENSING_NAVER_SEARCH_PER_SECOND` | `2` | 백엔드. 네이버 뉴스 탭 검색의 사용자별 초당 허용 횟수(남용 방지, 넘으면 429 `NAVER_TOO_FAST`) |
| `SENSING_NAVER_DAILY_LIMIT` | `20000` | 워커(`store` 모드). 네이버 검색 API 일 호출 상한(공식 25,000). 지금은 호출 수를 로그에 남기는 기준이고 정확한 쿼터 추적은 후속 |

키 자체는 환경 변수가 아니라 관리자 API 키 화면에서 넣습니다(`SENSING_SECRET_KEY`로 암호화, [09-admin.md](09-admin.md#api-키)).

## API

```bash
# 네이버 뉴스 탭 검색 (감시와 무관, 요약 포함, 사용자별 초당 2회)
curl 'http://127.0.0.1:8080/api/v1/naver/news?query=%EB%B0%98%EB%8F%84%EC%B2%B4&sort=sim&start=1&display=20' -H 'X-User: viewer'
# → {query, sort, total, start, display, items[{title, description, press, publishedAt, originalUrl, naverUrl}], nextStart, maxStart, fetchedAt, sourceLabel, mode}

# 감시 실시간 보기 (표시 전용: 저장하지 않음, Cache-Control no-store). 공용 감시는 누구나, 내 감시, ADMIN은 전체
curl 'http://127.0.0.1:8080/api/v1/watches/1/naver-news?keyword=%EB%A9%94%ED%83%80%EB%84%B7&display=20&start=1' -H 'X-User: registrar'

# 사용 방식·키 상태
curl http://127.0.0.1:8080/api/v1/naver/status -H 'X-User: registrar'

# 주요 키워드: 조회(VIEWER 이상) → {items[{label, query}], updatedAt} · 목록 전체 바꾸기(ADMIN)
curl http://127.0.0.1:8080/api/v1/featured-keywords -H 'X-User: viewer'
# 보낸 items가 새 목록 전체가 된다(순서 그대로). items가 없으면 400(빈 본문으로 목록이 지워지지 않게), 빈 배열은 "칩 없음"
curl -X PUT http://127.0.0.1:8080/api/v1/featured-keywords -H 'X-User: admin' -H 'Content-Type: application/json' \
     -d '{"items":[{"label":"반도체","query":"반도체"},{"label":"DS","query":"삼성전자 DS"}, ...]}'

# 관리자: 네이버 키 등록과 시험 호출 (검색 1건, 상태 VALID/INVALID)
curl -X POST http://127.0.0.1:8080/api/v1/api-credentials -H 'X-User: admin' -H 'Content-Type: application/json' \
     -d '{"provider":"naver_news","secret":"<Client ID>:<Client Secret>","label":"NAVER API HUB"}'
curl -X POST http://127.0.0.1:8080/api/v1/api-credentials/naver_news/verify -H 'X-User: admin'
```

## 한계와 주의

- 표시 전용이므로 네이버 결과는 문서 목록·검색·이슈·구독 메일·챗봇 답변에 들어가지 않고, 키워드 감시의 문서 수도 늘지 않습니다.
  이것들은 직접 수집한 언론사 기사로 동작합니다.
- 저장·분석(`store`)은 법무가 개정 약관을 확인한 뒤에만 켭니다. 확인할 항목은 [../naver-news-api.md](../naver-news-api.md) 7절.
- **짧은 영문**: DS·DX·DSR은 단독으로 찾으면 다른 뜻의 기사(닌텐도 DS, 대출 규제 DSR 등)가 섞입니다. 기본값은 요청대로 이름과 같은 검색어로
  두었고, 필요하면 관리자 화면에서 이름은 그대로 두고 검색어만 `삼성전자 DS`처럼 바꾸면 됩니다.
- 네이버 API 호출 수(쿼터)는 아직 정확히 추적하지 않습니다.
