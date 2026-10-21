# robots.txt 재점검과 기사 주소 누락 점검 (2026-10-21)

## 왜 다시 봤나

워커는 robots.txt 판정에 파이썬 기본 `urllib.robotparser`를 썼다. 이 파서는 그룹 안에서 **위에서부터 처음 맞는 규칙**을 쓰고
와일드카드(`*`)·끝 표시(`$`)를 모른다. 서울신문 robots.txt(아래)는 `Allow:/`를 맨 앞에 두었기 때문에 모든 경로가 첫 줄에서
허용으로 끝나고 아래의 `Disallow`가 하나도 적용되지 않았다. 그래서 서울신문(소스 28)은 사이트가 막은 `/xml/rss/google_*.xml`
피드 5개를 허브로 등록해 한 시간마다 읽고 있었다.

```
User-agent: *
Allow:/
Disallow : /SpecialEdition/dev/
Disallow : /xml/
Disallow : /members/

Sitemap: https://www.seoul.co.kr/sitemap/sitemap_latestArticle
...
```

판정을 RFC 9309 방식(가장 길게 맞는 규칙, 같은 길이면 Allow, `*`·`$` 지원)으로 바꾸고(`worker/src/sensing_worker/robots.py`,
자세한 규칙은 [how-collection-works.md](how-collection-works.md) 1절), 운영 중인 사이트 소스가 새 판정에서 막히는지 다시 봤다.

## 점검 방법

```bash
cd worker
PYTHONPATH=src python -m sensing_worker.robots_recheck --dsn postgresql://sensing:sensing@127.0.0.1:5433/sensing --samples 5 --out result.md
```

- 운영 DB는 **읽기만** 한다 (연결을 `read_only`로 연다). ACTIVE 사이트 소스(`kind SITE`) 50개.
- 허브: 활성 설정의 켜진 기사 종류 허브. 기사 표본: 소스마다 최근 문서 주소 5건.
- robots.txt는 사이트마다 한 번 받는다. 등록 주소(`http://seoul.co.kr`)가 실제 수집 호스트(`https://www.seoul.co.kr`)로 넘어가는
  사이트가 많아, 같은 사이트의 허브·기사 호스트가 있으면 그 robots.txt로 사이트 상태를 본다. 허브·기사가 다른 호스트(`rss.donga.com`,
  `cdn.sisain.co.kr` 등)에 있으면 그 호스트의 robots.txt도 받는다 (RFC 9309: robots.txt는 scheme·호스트·포트마다 따로다).
  간격은 워커와 같다(PoliteFetcher, 도메인당 2초). 이번 실행은 호스트 65곳, 요청 71회(리다이렉트 포함).
- "예전 판정과 다름"은 같은 robots.txt를 예전 파서(`urllib.robotparser`)로 읽었을 때와 허용·금지가 다른 주소 수다.

## 결과

점검 시각 2026-10-21 12:02 +0900.

| 소스 | 사이트 | 방식 | 설정 | robots (DB → 새 판정) | Crawl-delay | 허브 금지 | 기사 표본 금지 | 예전 판정과 다름 | 비고 |
|---:|---|---|---:|---|---:|---|---|---:|---|
| 1 | korea.kr | SITEMAP | v2 | PARTIAL → ALLOWED | - | 0/6 | 0/5 | 0 |  |
| 3 | yna.co.kr | SITEMAP | v1 | PARTIAL → PARTIAL | - | 0/1 | 0/5 | 0 |  |
| 18 | jbnews.com | RSS | v1 | PARTIAL → PARTIAL | - | 0/1 | 0/5 | 0 | 다른 호스트 robots도 확인: www.jbnews.com |
| 19 | etnews.com | STATIC | v1 | PARTIAL → ALLOWED | - | 0/16 | 0/5 | 0 |  |
| 21 | mt.co.kr | SITEMAP | v1 | PARTIAL → PARTIAL | - | 0/1 | 0/5 | 0 |  |
| 22 | dt.co.kr | RSS | v1 | PARTIAL → PARTIAL | - | 0/5 | 0/5 | 0 |  |
| 24 | ohmynews.com | STATIC | v1 | PARTIAL → PARTIAL | - | 0/16 | 0/5 | 0 | 다른 호스트 robots도 확인: star.ohmynews.com |
| 25 | sisain.co.kr | RSS | v1 | PARTIAL → PARTIAL | - | 0/5 | 0/5 | 0 | 다른 호스트 robots도 확인: cdn.sisain.co.kr |
| 28 | seoul.co.kr | RSS | v1 | ALLOWED → PARTIAL | - | **5/5** (`Disallow: /xml/`) | 0/5 | **5** |  |
| 29 | news.kbs.co.kr | SITEMAP | v1 | PARTIAL → PARTIAL | - | 0/1 | 0/5 | 0 |  |
| 30 | imaeil.com | RSS | v1 | PARTIAL → PARTIAL | - | 0/1 | 0/5 | 0 |  |
| 33 | asiatoday.co.kr | SITEMAP | v1 | PARTIAL → PARTIAL | - | 0/1 | 0/5 | 0 |  |
| 34 | isplus.joins.com | RSS | v1 | PARTIAL → PARTIAL | 5 | 0/1 | 0/5 | 0 | 다른 호스트 robots도 확인: isplus.com |
| 36 | hankookilbo.com | SITEMAP | v1 | PARTIAL → PARTIAL | - | 0/1 | 0/5 | 0 |  |
| 37 | edaily.co.kr | RSS | v1 | PARTIAL → PARTIAL | - | 0/1 | 0/5 | 0 | 다른 호스트 robots도 확인: rss.edaily.co.kr |
| 38 | pressian.com | SITEMAP | v1 | PARTIAL → PARTIAL | - | 0/1 | 0/5 | 0 |  |
| 39 | bloter.net | RSS | v1 | PARTIAL → PARTIAL | - | 0/1 | 0/5 | 0 | 다른 호스트 robots도 확인: cdn.bloter.net |
| 40 | fnnews.com | RSS | v1 | PARTIAL → PARTIAL | - | 0/5 | 0/5 | 0 |  |
| 41 | khan.co.kr | SITEMAP | v1 | PARTIAL → PARTIAL | - | 0/1 | 0/5 | 0 |  |
| 42 | sedaily.com | RSS | v1 | PARTIAL → PARTIAL | - | 0/5 | 0/5 | 0 |  |
| 43 | segye.com | RSS | v1 | PARTIAL → ALLOWED | - | 0/5 | 0/5 | 0 |  |
| 44 | nocutnews.co.kr | RSS | v1 | PARTIAL → PARTIAL | - | 0/5 | 0/5 | 0 | 다른 호스트 robots도 확인: rss.nocutnews.co.kr |
| 46 | mydaily.co.kr | RSS | v1 | PARTIAL → PARTIAL | - | 0/1 | 0/5 | 0 |  |
| 47 | joongang.co.kr | SITEMAP | v1 | PARTIAL → PARTIAL | - | 0/1 | 0/5 | 0 |  |
| 48 | hani.co.kr | RSS | v1 | PARTIAL → PARTIAL | - | 0/1 | 0/5 | 0 |  |
| 49 | donga.com | RSS | v1 | PARTIAL → PARTIAL | - | 0/5 | 0/5 | 0 | 다른 호스트 robots도 확인: rss.donga.com |
| 51 | ichannela.com | SITEMAP | v1 | PARTIAL → ALLOWED | - | 0/1 | 0/5 | 0 |  |
| 52 | mk.co.kr | RSS | v1 | PARTIAL → ALLOWED | - | 0/5 | 0/5 | 0 |  |
| 53 | munhwa.com | SITEMAP | v1 | PARTIAL → PARTIAL | - | 0/1 | 0/5 | 0 |  |
| 54 | hankyung.com | RSS | v1 | PARTIAL → PARTIAL | - | 0/5 | 0/5 | 0 |  |
| 55 | biz.chosun.com | SITEMAP | v1 | PARTIAL → PARTIAL | - | 0/1 | 0/5 | 0 |  |
| 56 | zdnet.co.kr | RSS | v1 | ALLOWED → PARTIAL | - | 0/1 | 0/5 | 0 |  |
| 57 | koreaherald.com | RSS | v1 | PARTIAL → PARTIAL | - | 0/1 | 0/5 | 0 |  |
| 58 | newdaily.co.kr | SITEMAP | v1 | PARTIAL → PARTIAL | - | 0/1 | 0/5 | 0 |  |
| 59 | koreajoongangdaily.joins.com | SITEMAP | v1 | PARTIAL → ALLOWED | - | 0/1 | 0/5 | 0 | 다른 호스트 robots도 확인: www.koreajoongangdaily.com |
| 60 | osen.mt.co.kr | STATIC | v1 | PARTIAL → ALLOWED | - | 0/1 | 0/5 | 0 | 다른 호스트 robots도 확인: www.osen.co.kr |
| 61 | mediatoday.co.kr | SITEMAP | v1 | PARTIAL → PARTIAL | - | 0/1 | 0/5 | 0 |  |
| 62 | heraldbiz.com | RSS | v1 | PARTIAL → PARTIAL | - | 0/1 | 0/5 | 0 | 다른 호스트 robots도 확인: biz.heraldcorp.com |
| 63 | ytn.co.kr | SITEMAP | v1 | PARTIAL → PARTIAL | - | 0/1 | 0/5 | 0 |  |
| 64 | mbn.mk.co.kr | RSS | v1 | PARTIAL → ALLOWED | - | 0/1 | 0/5 | 0 | 다른 호스트 robots도 확인: www.mbn.co.kr |
| 65 | yonhapnewstv.co.kr | STATIC | v1 | PARTIAL → PARTIAL | - | 0/7 | 0/5 | 0 |  |
| 66 | dailian.co.kr | STATIC | v1 | PARTIAL → PARTIAL | - | 0/16 | 0/5 | 0 |  |
| 67 | newsis.com | SITEMAP | v1 | PARTIAL → PARTIAL | - | 0/1 | 0/5 | 0 |  |
| 68 | weekly.chosun.com | RSS | v1 | PARTIAL → PARTIAL | - | 0/1 | 0/5 | 0 | 다른 호스트 robots도 확인: weekly.chosun.com |
| 69 | weekly.donga.com | RSS | v1 | PARTIAL → PARTIAL | - | 0/1 | 0/5 | 0 |  |
| 70 | sisajournal.com | RSS | v1 | PARTIAL → PARTIAL | - | 0/1 | 0/5 | 0 | 다른 호스트 robots도 확인: cdn.sisajournal.com |
| 73 | monthly.chosun.com | RSS | v1 | PARTIAL → PARTIAL | - | 0/1 | 0/5 | 0 | 다른 호스트 robots도 확인: cdn.monthly.chosun.com |
| 74 | economist.co.kr | RSS | v1 | PARTIAL → PARTIAL | 5 | 0/1 | 0/5 | 0 |  |
| 76 | news.suwon.go.kr | STATIC | v1 | PARTIAL → PARTIAL | - | 0/5 | 0/5 | 0 |  |
| 88 | motir.go.kr | STATIC | v1 | PARTIAL → PARTIAL | - | 0/14 | 0/5 | 0 |  |

(68 weekly.chosun.com의 "다른 호스트"는 같은 호스트의 다른 scheme(http/https)이다.)

## 해석

- **새 판정으로 금지되는 곳은 서울신문(소스 28) 하나**다. 허브 5개(`/xml/rss/google_*.xml`)가 모두 `Disallow: /xml/`에 걸린다.
  기사 표본은 50개 소스 모두 허용이다. 예전 판정과 허용·금지가 갈린 주소도 서울신문 허브 5개뿐이다.
- **robots 상태가 바뀐 10곳은 판정이 아니라 상태 계산 방식 때문이다.** 예전에는 robots.txt 글 어딘가에 `disallow:`라는 글자가
  있으면 PARTIAL이었다. 새 방식은 **우리에게 적용되는 그룹**에 값이 있는 `Disallow`가 있을 때만 PARTIAL이다.
  - PARTIAL → ALLOWED (8곳: korea.kr·etnews·segye·ichannela·mk·koreajoongangdaily·osen·mbn): 다른 봇 그룹에만 Disallow가 있거나
    (korea.kr은 Googlebot 그룹뿐이고 `*` 그룹은 `Allow: /`와 빈 `Disallow:`), 주석 처리된 줄뿐이다(etnews `#Disallow: /tools`).
  - ALLOWED → PARTIAL (2곳: seoul·zdnet): `Disallow : /xml/`처럼 콜론 앞에 공백이 있어 글자 검색에 걸리지 않았다.
    지디넷도 서울신문과 같은 꼴(`Allow: /`가 맨 앞, `Disallow : /Include2/user/`, `Disallow : /Contents/`)이라 예전 파서에서는
    이 Disallow도 무시됐다. 다만 지디넷의 허브(`/feed/…`)와 기사(`/view/?no=…`)는 이 경로가 아니어서 수집에는 영향이 없었다.
  - DB의 `site.robots_status`는 등록(사전 점검) 때 값이라 다음 다시 탐색 때 새 방식으로 바뀐다. 표시용이고 수집 판단에는 쓰지 않는다.
- **Crawl-delay 5초**를 적은 곳이 2곳(isplus.com, economist.co.kr) 있다. 워커는 예전과 같이 Crawl-delay를 읽기만 하고 간격은
  도메인당 2초를 쓴다(이번 변경에서 바꾸지 않음). 따를지는 후속 결정으로 남긴다.

## 서울신문 다시 탐색 (DB 없이, 2026-10-21)

`probe_site` + `jobs.discover()`를 운영과 같은 설정으로 실행했다(DB·LLM 없이, 요청 14회 26초).

| | 지금 운영 (설정 v1) | 새 워커로 다시 탐색 |
|---|---|---|
| 방식 | RSS | SITEMAP |
| 허브 | `/xml/rss/google_{flash_seoul,top,politics,economy,society}.xml` (robots 금지) | `/sitemap/sitemap_latestArticle` (robots 허용, 723건) |
| 기사 패턴 | `/news/{*}/{n}/{n}/{n}/{n}` = `^/news/[^/]+/\d+/\d+/\d+/\d+$` | `/news/{**}/{n}/{n}/{n}/{n}` = `^/news/[^/]+(?:/[^/]+){0,2}/\d+/\d+/\d+/\d+$` |
| 기사 종류 | 1개 | 1개 (켜짐, 723건, 샘플 5건 중 5건 통과) |

- 홈이 알리는 피드 5개는 robots 검사에서 걸러져 **한 번도 요청하지 않았다**(탐색 메모 `ROBOTS_DISALLOWED`). 사이트맵 단계로 넘어갔다.
- 두 주소 모양이 모두 켜진 종류에 맞는다: `/news/politics/2026/10/21/…`, `/news/economy/industry/2026/10/21/…`,
  `/news/international/topic-global/2026/10/21/…`, 3단인 `/news/sport/golf/iceberggolf/2026/10/21/…`.
- 처음 실측(깊이 병합 전)에서는 종류가 깊이마다 갈렸다: `news`(`/news/{*}/…`, 368건) · `news (news)`(`/news/{*}/{*}/…`, 328건) ·
  `sport`(`/news/sport/golf/iceberggolf/…`, 8건). 종류가 둘 이상이면 문서 `section`에 종류 이름이 들어가 기사 자체의 분류(정치·경제…)가
  `news`·`news (news)`로 덮이므로, 깊이가 다른 서명을 `{**}`(경로 조각 1~3개)로 합치는 병합을 더했다(how-collection-works.md 3절).
## 기사 주소 누락 점검 (2026-10-21)

같은 날 다른 세션이 ACTIVE 사이트 소스 50곳을 읽기 전용으로 점검했다(운영 DB SELECT, robots가 허용한 사이트맵 또는 허브 한두 건).
공개 목록의 기사 주소 가운데 켜진 기사 종류(패턴)에 맞지 않는 것:

| 소스 | 사이트 | 지금 설정 | 비교 목록 | 누락 | 빠진 모양 |
|---:|---|---|---|---:|---|
| 49 | 동아일보 | RSS v1 `/news/Society/article/all/{n}/{n}/{n}` | 허브 RSS 122건 | **99 (81%)** | Politics 42, Economy 35, It 9, Inter 6 … (사회면만 수집, 저장 272건 모두 Society) |
| 28 | 서울신문 | RSS v1 `/news/{*}/{n}/{n}/{n}/{n}` | 사이트맵 721건 | **353 (49%)** | `/news/economy/industry/…` 등 작은 분류 |
| 54 | 한국경제 | RSS v1 `/article/{n}` | 사이트맵 latest-article 897건 | **412 (46%)** | `/article/202610214628g` (끝에 영문자, 저장 0건) |
| 76 | 수원시 소식 | STATIC v1 `/?mode=*&p={n}&reqIdx={n}&viewMode=*` | 허브 링크 96개 | **30** | `?p=35&viewMode=view&reqIdx=…` (mode 없는 상세) |
| 48 | 한겨레 | RSS v1 `/arti/{*}/{*}/{n}.html` | 사이트맵 250건 | 3 | `/arti/sports/sportstemp/asiangame/{n}.html` (3단) |
| 55 | 조선비즈 | SITEMAP v1 `/{*}/{*}/{n}/{n}/{n}/{id}/` | 뉴스 사이트맵 100건 | 4 | `/policy/politics/president_office/…`(3단), 숫자 없는 Arc ID(`LOEDJZEKQNHLDOXRKAGNXNVDYY`) |

누락이 아닌 것: MBN(사이트맵은 새 형식이지만 RSS 옛 형식으로 하루 104건 수집 중), 데일리안·주간동아(사이트맵 주소 꼴만 다름),
산업부·전자신문·오마이뉴스·연합뉴스TV·뉴시스·OSEN·아시아투데이(맞지 않은 것은 안내·목록·회사 소개 같은 기사 아닌 페이지).
수원시 허브의 나머지 50개(`/?p=58` 메뉴 35, `?p=22&mode=blog` 목록 14 등)도 기사가 아니다.

공통 원인은 **등록할 때 본 주소들로 패턴을 하나 고정하면 다른 주소 모양을 조용히 놓친다**는 것이다. 동아일보는 섹션 병합(`{*}`)과
기사 종류 기능 전에 등록된 설정이라 다시 탐색만 하면 되고, 나머지는 탐색 규칙을 고쳤다(how-collection-works.md 3절).

### 고친 탐색으로 다시 탐색 (DB 없이)

`sensing_worker.evaluate`(사전 점검 + `jobs.discover()`, DB·LLM 없이, 간격 2초)로 6곳을 다시 탐색했다. 위 표의 빠진 모양 예시 주소가
모두 켜진 종류에 맞는다.

| 소스 | 사이트 | 방식 | 새 대표 패턴 | 품질 검사 | 고친 규칙 |
|---:|---|---|---|---|---|
| 49 | 동아일보 | RSS | `/news/{*}/article/all/{n}/{n}/{n}` | 5건 중 4건 | 없음 (고치기 전 코드로도 같은 결과 — 설정 v1이 오래됐을 뿐) |
| 28 | 서울신문 | SITEMAP | `/news/{**}/{n}/{n}/{n}/{n}` | 5건 중 5건 | robots 판정, 섹션 깊이 병합 |
| 54 | 한국경제 | RSS | `/article/{id}` | 5건 중 5건 | 넓은 서명이 받는 횟수 (`{id}` ⊇ `{n}`) |
| 76 | 수원시 소식 | STATIC | `/?p={n}&reqIdx={n}&viewMode=*` (72건, 고치기 전 46건) | 5건 중 5건 | 넓은 서명이 받는 횟수 (mode 없는 쿼리) |
| 48 | 한겨레 | RSS | `/arti/{**}/{n}.html` | 5건 중 5건 | 두 단 병합을 1~3단으로 |
| 55 | 조선비즈 | SITEMAP | `/{**}/{n}/{n}/{n}/{id}/` | 5건 중 5건 | 대문자 Arc ID, 날짜+ID 꼬리의 앞머리 없는 `{**}` |

회귀 확인: 평가 목록 13곳(`docs/eval-sites.txt`·`docs/eval-samples.txt`)을 새 코드와 변경 전 코드(HEAD)로 차례로 돌려 비교했다.
한국경제만 `/article/{n}` → `/article/{id}`로 바뀌고 나머지 12곳은 방식·대표 패턴·허브·기사 종류(켜짐/꺼짐)가 같다.
중간 시도에서 쿼리 키 흡수를 모든 서명에 적용했더니 산업부의 `공지사항`(`/kor/article/{id}/{n}/view?mno=`) 종류가
`보도·참고자료`에 합쳐져, 키 수가 다른 흡수는 글을 쿼리로 가리키는 서명에만 하도록 좁혔다(그 뒤 산업부는 변경 전과 같음).
www.eetimes.com은 두 코드 모두 `UNEXPECTED: AttributeError: 'list' object has no attribute 'strip'`로 실패한다 — 이번 변경과 무관한 기존 문제다.

### 누락 점검 지표 (pattern_check, V21)

워커가 주기 수집 중 소스마다 하루 한 번(설정 버전이 바뀌면 바로) 위 점검과 같은 비율을 재서 `pattern_check`에 남기고, 관리자
운영 지표 화면의 "기사 주소 누락 점검"이 소스별 마지막 점검을 보여 준다. 누락 20% 이상이면서 5건 이상이면 "재탐색 후보"다.
비교 목록은 그 실행에서 연 허브 + robots가 허용한 사이트맵 하나(요청 최대 3회)이고, HTML 목록의 메뉴 링크는 켜진 종류와 가까운
것만 센다. 위 표의 수치로 넣어 본 화면에서 동아일보·한국경제·수원시가 후보, 다시 탐색한 서울신문은 "다음 수집에서 다시 잼",
한겨레(1%)는 후보가 아니었다. 운영 수치는 새 워커·백엔드를 배포한 뒤 첫 주기 수집부터 쌓인다.

## 운영 DB에 쓰는 명령 (준비만, 실행하지 않음)

**순서가 중요하다.**

1. 백엔드를 먼저 배포해 V21(`pattern_check`)을 적용한다. 워커가 먼저 떠도 수집은 돌지만 누락 점검은 건너뛴다.
   V20은 회사 관계 브랜치(`claude/upbeat-swirles-f07998`)가 쓰므로, 그 브랜치를 먼저 병합하거나 같이 병합한다. 이 브랜치만 먼저 병합해
   V21이 적용된 DB에는 Flyway가 V20을 "적용 안 된 낮은 버전"으로 보고 시작을 거부한다(`spring.flyway.out-of-order=true`로 한 번 풀 수 있다).
2. 워커를 다시 띄운다. **예전 워커로 다시 탐색하면 예전 규칙으로 같은 설정을 다시 만든다**(서울신문은 다시 막힌 RSS를 고른다).
   새 워커가 뜨고 다시 탐색 전에 주기 수집이 돌면, 서울신문(28)은 허브 5개가 모두 `ROBOTS_DISALLOWED`로 거절돼 그 실행이 실패로
   남는다(요청은 보내지 않는다. 상태는 ACTIVE 그대로, `consecutive_failures`만 오른다).

(선택, 지금) 배포 전까지 서울신문 수집 멈추기 — 지금 운영 워커는 막힌 피드를 한 시간마다 읽고 있다.

```bash
curl -s -X POST -H "X-User: admin" http://127.0.0.1:8080/api/v1/sources/28/actions/pause
```

3. 수원시 소식(76): 다시 탐색 **전에** 저장된 주소에서 `mode`를 뺀다. 새 패턴은 `mode`를 필수 키에서 빼므로 정규 주소가
   `?p=…&reqIdx=…&viewMode=…`가 되고, 그대로 두면 허브에 아직 보이는 기사(약 40건)를 새 주소로 한 번 더 저장한다. 2026-10-21 기준
   저장 51건 모두 `mode=blog`가 붙어 있고 `mode` 없는 주소는 0건이다(겹칠 행 없음). `url_hash`는 워커와 같은 sha256(주소)이다.

   ```bash
   curl -s -X POST -H "X-User: admin" http://127.0.0.1:8080/api/v1/sources/76/actions/pause
   ```

   ```sql
   BEGIN;
   WITH fixed AS (
       SELECT id, regexp_replace(url, '([?&])mode=[^&]*&', '\1') AS url FROM document
       WHERE source_id = 76 AND url ~ '[?&]mode=[^&]*&')
   UPDATE document d SET url = f.url, url_hash = encode(sha256(convert_to(f.url, 'UTF8')), 'hex')
   FROM fixed f WHERE d.id = f.id;
   -- 기대: UPDATE 51. 겹침 확인 (0이어야 한다)
   SELECT count(*) - count(DISTINCT url_hash) FROM document WHERE source_id = 76;
   COMMIT;
   ```

4. 다시 탐색 (ONBOARD 작업 → 설정 새 버전. ACTIVE·PAUSED 어느 쪽에서도 되고, 성공하면 ACTIVE로 돌아온다):

   ```bash
   for id in 28 49 54 76 48 55; do curl -s -X POST -H "X-User: admin" http://127.0.0.1:8080/api/v1/sources/$id/actions/rediscover; echo; done
   ```

   백엔드가 떠 있지 않을 때의 같은 작업(감사 로그는 남지 않는다):

   ```sql
   INSERT INTO crawl_job (source_id, job_type, config_version_id, trigger)
   SELECT id, 'ONBOARD', active_config_version_id, 'MANUAL' FROM source WHERE id IN (28, 49, 54, 76, 48, 55)
   ON CONFLICT (source_id) WHERE status IN ('QUEUED', 'RUNNING') DO NOTHING;
   ```

5. 확인 (읽기만):

   ```sql
   SELECT s.id, t.domain, s.status, v.version, v.config->>'method' AS method, v.config->'article_pattern'->>'signature' AS signature,
          v.change_note
   FROM source s JOIN site t ON t.id = s.site_id JOIN source_config_version v ON v.id = s.active_config_version_id
   WHERE s.id IN (28, 49, 54, 76, 48, 55) ORDER BY s.id;
   -- 기대 (DB 없이 다시 탐색한 결과): 28 SITEMAP /news/{**}/{n}/{n}/{n}/{n} · 49 RSS /news/{*}/article/all/{n}/{n}/{n}
   --   · 54 RSS /article/{id} · 76 STATIC /?p={n}&reqIdx={n}&viewMode=* · 48 RSS /arti/{**}/{n}.html · 55 SITEMAP /{**}/{n}/{n}/{n}/{id}/
   SELECT source_id, checked_at, article_count, unmatched_count, unmatched_ratio, top_unmatched
   FROM pattern_check WHERE source_id IN (28, 49, 54, 76, 48, 55) ORDER BY source_id, checked_at DESC;
   -- 다시 탐색 뒤 첫 주기 수집에서 새 설정으로 다시 잰다 (누락 비율이 0에 가까워야 한다)
   ```

   새 설정은 한 실행에 기사 40건(`SENSING_MAX_ARTICLES`)까지, 허브(사이트맵이면 최신 120건) 안에서 고른다. 그보다 오래된 누락 기사
   (서울신문 작은 분류 322건, 한국경제 영숫자 번호, 동아일보 사회면 밖 등)는 평소 수집으로는 다시 오지 않으니, 필요하면 소스 상세에서
   과거 기사 채우기(BACKFILL)를 건다.

6. (선택) 상태 표시만 맞추기 — 다시 탐색하지 않는 9곳의 `site.robots_status`를 새 방식 값으로. 수집 동작은 바뀌지 않는다.

   ```sql
   UPDATE site SET robots_status = 'ALLOWED'
   WHERE id IN (SELECT site_id FROM source WHERE id IN (1, 19, 43, 51, 52, 59, 60, 64)) AND robots_status = 'PARTIAL';
   UPDATE site SET robots_status = 'PARTIAL'
   WHERE id IN (SELECT site_id FROM source WHERE id = 56) AND robots_status = 'ALLOWED';
   ```
