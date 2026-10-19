# 네이버 뉴스 검색 API (2026-10-19 기준)

네이버 뉴스 검색을 이 시스템에서 어떻게 쓰는지, 무엇을 확인했고 무엇이 남았는지 한곳에 모은 문서다.
설정 변수 표는 [README](../README.md)의 "네이버 뉴스 검색", 결정 기록은 [decisions.md](decisions.md)에도 있다.

## 1. 한눈에

| 항목 | 상태 |
| --- | --- |
| 제공 경로 | **NAVER API HUB**(네이버클라우드). 2026-06-25 오픈, 뉴스 검색이 이곳으로 이관됐다 |
| 예전 개발자센터 | 2026-07-31부터 신규 발급 중단, 기존 키는 2027-06-30까지 |
| 요금 | 종량제지만 2026-10 현재 한시적 무료. 유료 전환은 사전 공지 |
| 한도 | 검색 묶음(뉴스·블로그·카페 등) 합쳐 월 775,000회, 키당 초당 50회. 넘으면 HTTP 429 |
| 우리 키 | 2026-10-19 관리자 화면에 등록, 시험 호출 VALID |
| 우리 사용 방식 | **표시 전용**(`SENSING_NAVER_MODE=display`). 법무 확인 전까지 유지하기로 결정(2026-10-19) |

## 2. 키 발급

1. 네이버클라우드 콘솔 → Services → Application Service → **NAVER API HUB**.
2. **이용 신청**(결제 수단 필요).
3. **Application 등록** → 쓸 API로 **검색** 선택.
4. 등록하면 인증 정보가 자동 생성된다. Application 정보에서 **Client ID**와 **Client Secret**을 확인한다.
5. 이 시스템의 관리자 화면 → **API 키** → 제공자 `naver_news` → Client ID·Client Secret 두 칸에 넣고 저장. 저장 직후 시험 호출 결과(VALID·INVALID)가 표시된다.

키는 채팅·메신저·커밋에 붙이지 않는다. 한 번 노출됐으면 콘솔에서 Client Secret을 다시 발급한다.

## 3. 요청과 응답 (2026-10-19 실제 호출로 확인)

### 요청

GET 요청이라 본문(body)은 없다. 검색 조건은 주소 뒤 쿼리로, 키는 헤더로 보낸다. HTTPS만 된다(HTTP는 403).

```
GET https://naverapihub.apigw.ntruss.com/search/v1/news?query=반도체&display=2&start=1&sort=date&format=json
X-NCP-APIGW-API-KEY-ID: <Client ID>
X-NCP-APIGW-API-KEY: <Client Secret>
```

| 파라미터 | 필수 | 값 |
| --- | --- | --- |
| `query` | 예 | 검색어 (UTF-8, URL 인코딩) |
| `display` | 아니오 | 한 번에 받을 수, 1~100 (기본 10) |
| `start` | 아니오 | 시작 위치, 1~1000 (기본 1) |
| `sort` | 아니오 | `sim` 정확도순(기본), `date` 최신순 |
| `format` | 아니오 | `json`(기본), `xml` |

예전 개발자센터는 주소 `https://openapi.naver.com/v1/search/news.json`, 헤더 `X-Naver-Client-Id`·`X-Naver-Client-Secret`였다.
`SENSING_NAVER_API=developers`로 고를 수 있다.

### 응답

```json
{
  "lastBuildDate": "Mon, 19 Oct 2026 22:28:28 +0900",
  "total": 3871813,
  "start": 1,
  "display": 2,
  "items": [
    {
      "title": "(기사 제목)",
      "originallink": "https://www.munhwa.com/article/11624612?ref=naver",
      "link": "https://n.news.naver.com/mnews/article/021/0002826231?sid=100",
      "description": "(기사 앞부분 두세 문장 … <b>반도체</b> …) ... ",
      "pubDate": "Mon, 19 Oct 2026 22:26:00 +0900"
    }
  ]
}
```

| 필드 | 내용 |
| --- | --- |
| `lastBuildDate` | 결과를 만든 시각 |
| `total` | 검색어에 걸린 전체 기사 수 ("반도체"는 약 387만 건) |
| `start`, `display` | 요청한 시작 위치와 받은 수 |
| `items[].title` | 기사 제목. 검색어에 `<b>` 태그, 따옴표 등은 `&quot;` 같은 HTML 기호로 온다 |
| `items[].originallink` | 언론사 원문 주소. `?ref=naver` 같은 추적 값이 붙기도 한다 |
| `items[].link` | 네이버 뉴스에 있으면 네이버 주소, 없으면 원문 주소와 같다 |
| `items[].description` | 기사 앞부분 두세 문장. `<b>` 태그가 있고 "..."로 끝난다 |
| `items[].pubDate` | 발행 시각(RFC 1123, +0900) |

오지 않는 것: **언론사 이름**(원문 주소 도메인으로 알아낸다 — 등록된 사이트면 그 이름, 아니면 도메인), 본문, 사진, 분야.

### 오류

| HTTP | 코드 | 뜻 |
| --- | --- | --- |
| 400 | SE01 | 요청 형식 오류 (https·필수 `query` 확인) |
| 400 | SE02 / SE03 / SE04 | `display` / `start` / `sort` 값이 범위 밖 |
| 400 | SE06 | 검색어 인코딩 오류 (UTF-8) |
| 404 | SE05 | 없는 검색 API 주소 |
| 401·403 | — | 키 문제(잘못된 Client ID·Secret, Application에 "검색" 미선택). 이 시스템은 키를 INVALID로 표시 |
| 429 | — | 한도 초과 |
| 500 | SE99 | 네이버 서버 오류 |

## 4. 약관 (2026-09-07 시행 개정)

보도(한국데이터경제신문, 조선비즈 인용)에 따르면 아래가 금지다. **약관 원문은 아직 직접 확인하지 못했다.**

- 검색 결과를 AI에 입력하는 것, AI 학습·개선·평가·노출에 쓰는 것
- 검색 결과를 복사·저장·캐싱하는 것
- 검색 결과를 제3자에게 제공·판매하는 것, 광고로 수익화하는 것

이 시스템의 기본 기능(문서 저장, 이슈 묶기, 다이제스트 메일, Gemini 분류·요약, 챗봇 근거, 임베딩)은 거의 모두 여기에 걸린다.
그래서 기본을 표시 전용으로 두었다.

## 5. 이 시스템에서의 사용

### 사용 방식 (`SENSING_NAVER_MODE`, 백엔드·워커 같은 값)

| 값 | 동작 |
| --- | --- |
| `display` (기본) | 화면에 실시간으로 보여 주기만 한다. DB·요약(LLM)·임베딩·챗봇·다이제스트에 넣지 않고 캐시도 두지 않는다(`Cache-Control: no-store`). 워커는 네이버를 부르지 않는다 |
| `store` | 법무 확인 뒤에만 켠다. 키워드 감시에 네이버 검색 수집이 붙어 60분(회사 감시 360분)마다 받아 저장하고, 문서·이슈·다이제스트·챗봇에 들어간다 |
| `off` | 네이버를 쓰지 않는다. 관련 버튼·탭은 안내만 보인다 |

### 화면과 API (표시 전용 기준)

| 어디 | 무엇 |
| --- | --- |
| 왼쪽 메뉴 **네이버 뉴스** (`#/naver?q=`) | 검색창, 최신순·정확도순, 20건씩 더 보기(최대 1000번째까지), 언론사·시각·요약 문장, 원문·네이버 링크, 출처 "NAVER 검색 결과", 빠른 검색 칩 |
| 키워드 감시 화면 **네이버 실시간** | 감시 키워드로 네이버 최신 뉴스 보기 |
| 관리자 **API 키** | `naver_news` Client ID·Client Secret 입력, 시험 호출 |
| `GET /api/v1/naver/status` | 사용 방식·키 상태 |
| `GET /api/v1/naver/news?query&display&start&sort` | 네이버 뉴스 탭 검색. 사용자마다 초당 2회(`SENSING_NAVER_SEARCH_PER_SECOND`), 검색어만 감사 로그에 남기고 결과는 남기지 않는다 |
| `GET /api/v1/watches/{id}/naver-news` | 감시 실시간 보기 |
| `POST /api/v1/api-credentials/naver_news/verify` | 키 시험 호출 |

**작업 중 (2026-10-19):** 빠른 검색 칩을 관리자가 정하는 주요 키워드 9개(반도체, 메타넷, 메타넷 글로벌, 삼성전자, DS, DX, DSR, 테슬라, 하이닉스)로
줄이고, 챗봇에서 "네이버 - 검색어"로 입력하면 네이버 결과만 대화창에 표시 전용 카드로 보여 준다(대화 기록에 저장하지 않고 AI에 넣지 않음).
DS·DX·DSR처럼 짧은 영문은 단독 검색 시 엉뚱한 기사(닌텐도 DS, 대출 규제 DSR)가 섞이므로 칩마다 실제 검색어("삼성전자 DS" 등)를 따로 둘 수 있게 한다.

### 표시 전용에서 안 되는 것

네이버 결과는 문서 목록·검색·이슈·다이제스트·챗봇 답변에 들어가지 않고, 키워드 감시의 문서 수도 늘지 않는다.
이것들은 언론사 52곳을 직접 수집한 기사로 동작한다(`docs/how-collection-works.md`).

## 6. 결정과 전환 절차

- 2026-10-19 결정: 표시 전용으로 두고, **법무가 개정 약관을 확인한 뒤에만** `store`로 바꾼다.
- `store` 전환 순서:
  1. `scripts/dev/worker.local.env`(운영은 백엔드·워커 환경 변수)에 `SENSING_NAVER_MODE=store`.
  2. 백엔드·워커 재시작.
  3. 관리자 API 키 화면에서 `naver_news` 키를 한 번 더 저장 — 저장할 때 기존 키워드 감시에 네이버 수집이 붙는다. 새로 만드는 감시는 처음부터 붙는다.

## 7. 법무에 확인할 것

- [ ] 개정 약관 원문: 표시 전용(저장 없이 검색 결과를 그대로 보여 주기)이 허용되는지, 출처 표기 형식 요구가 있는지
- [ ] 사내 업무용 저장·요약·챗봇 근거(`store`)가 가능한지, 별도 계약이 있는지
- [ ] 검색어 감사 로그(결과 없이 검색어만)를 남겨도 되는지

## 8. 대안

- **직접 수집 + 키워드 대조**(지금 주 경로): 언론사 RSS·사이트맵·목록을 robots와 간격을 지켜 읽고, 언론 기사는 제목·링크만 저장한다.
- **뉴스 DB 계약**(한국언론진흥재단 빅카인즈 등): 여러 언론사의 긴 기간, 저작권 정리가 함께 필요할 때.

## 출처

- [NAVER API HUB 개요 (네이버클라우드 가이드)](https://guide.ncloud-docs.com/docs/apihub-overview)
- [NAVER API HUB 뉴스 검색 API 참조](https://api.ncloud-docs.com/docs/naver-api-hub-search-news)
- [NAVER API HUB API 가이드 개요](https://api.ncloud-docs.com/docs/naver-api-hub-overview)
- [네이버 오픈API가 API HUB로 옮겨갔다 — 이관 정리 (키워드 랩)](https://keyword-lab-delta.vercel.app/guide/naver-api-hub)
- [네이버, 9월부터 검색 API 'AI 활용 금지' (한국데이터경제신문)](https://www.dataeconomy.co.kr/news/articleView.html?idxno=42307)
