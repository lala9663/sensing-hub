# robots.txt 재점검 (2026-10-21)

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
- 회귀 확인: 평가 목록 13곳(`docs/eval-sites.txt`·`docs/eval-samples.txt`)을 새 코드와 변경 전 코드(HEAD)로 차례로 돌려 비교했다.
  13곳 모두 방식·대표 패턴·허브·기사 종류(켜짐/꺼짐)가 같다(12곳 OK). www.eetimes.com은 두 코드 모두
  `UNEXPECTED: AttributeError: 'list' object has no attribute 'strip'`로 실패한다 — 이번 변경과 무관한 기존 문제다.

## 운영 DB에 쓰는 명령 (준비만, 실행하지 않음)

순서가 중요하다. **예전 워커로 다시 탐색하면 다시 RSS를 고른다**(예전 판정은 `/xml/`을 허용으로 본다). 새 워커를 배포한 뒤에 한다.
새 워커가 배포되고 다시 탐색 전에 주기 수집이 돌면, 소스 28은 허브 5개가 모두 `ROBOTS_DISALLOWED`로 거절돼 그 실행이 실패로 남는다
(요청은 보내지 않는다. 상태는 ACTIVE 그대로, `consecutive_failures`만 오른다).

1. (선택, 지금) 배포 전까지 서울신문 수집 멈추기 — 지금 운영 워커는 막힌 피드를 한 시간마다 읽고 있다.

   ```bash
   curl -s -X POST -H "X-User: admin" http://127.0.0.1:8080/api/v1/sources/28/actions/pause
   ```

2. 이 브랜치를 main에 병합하고 운영 워커를 다시 띄운 뒤, 서울신문 다시 탐색 (ONBOARD 작업 → 설정 v2, ACTIVE·PAUSED 어느 쪽에서도 된다.
   성공하면 ACTIVE로 돌아온다).

   ```bash
   curl -s -X POST -H "X-User: admin" http://127.0.0.1:8080/api/v1/sources/28/actions/rediscover
   ```

   백엔드가 떠 있지 않을 때의 같은 작업(감사 로그는 남지 않는다):

   ```sql
   INSERT INTO crawl_job (source_id, job_type, config_version_id, trigger)
   SELECT id, 'ONBOARD', active_config_version_id, 'MANUAL' FROM source WHERE id = 28
   ON CONFLICT (source_id) WHERE status IN ('QUEUED', 'RUNNING') DO NOTHING;
   ```

3. 확인 (읽기만):

   ```sql
   SELECT version, status, config->>'method' AS method, config->'hubs' AS hubs,
          config->'article_pattern'->>'signature' AS signature, change_note
   FROM source_config_version WHERE source_id = 28 ORDER BY version;
   -- 기대: v2 ACTIVE, SITEMAP, ["https://www.seoul.co.kr/sitemap/sitemap_latestArticle"], /news/{**}/{n}/{n}/{n}/{n}
   SELECT count(*) FILTER (WHERE url ~ '^https?://[^/]+/news/[^/]+/[^/]+/\d+/') AS sub_category,
          count(*) FILTER (WHERE url ~ '^https?://[^/]+/news/[^/]+/\d+/') AS short
   FROM document WHERE source_id = 28 AND collected_at > now() - interval '1 day';
   ```

   새 설정은 한 실행에 기사 40건(`SENSING_MAX_ARTICLES`)까지, 사이트맵 최신 120건 안에서 고른다. 그보다 오래된 작은 분류 기사(오늘
   확인한 누락 322건 중 일부)는 평소 수집으로는 다시 오지 않으니, 필요하면 소스 상세에서 과거 기사 채우기(BACKFILL)를 건다.

4. (선택) 상태 표시만 맞추기 — 다시 탐색하지 않는 9곳의 `site.robots_status`를 새 방식 값으로. 수집 동작은 바뀌지 않는다.

   ```sql
   UPDATE site SET robots_status = 'ALLOWED'
   WHERE id IN (SELECT site_id FROM source WHERE id IN (1, 19, 43, 51, 52, 59, 60, 64)) AND robots_status = 'PARTIAL';
   UPDATE site SET robots_status = 'PARTIAL'
   WHERE id IN (SELECT site_id FROM source WHERE id = 56) AND robots_status = 'ALLOWED';
   ```
