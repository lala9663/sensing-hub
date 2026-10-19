# 수집 중인 사이트의 과거 기사 가능 범위 (2026-10-19 조사)

과거 기사 채우기(백필)를 어느 사이트에서 얼마나 거슬러 할 수 있는지 보려고, 수집 중인 사이트 52곳(ACTIVE SITE)의
`robots.txt` → 사이트맵(색인이면 하위 파일 이름, 이름에 날짜가 없으면 첫·마지막·번호 최소·최대 하위 파일)을 읽어 가장 오래된 날짜를 봤다.
도메인당 2초 간격, 읽기만 했다. **표본 조사**라 실제로는 더 오래된 글이 있을 수 있고, 사이트맵에 있어도 옛 주소가 사라졌을 수 있다.
조사 스크립트는 저장소 밖(작업 메모)에 두었다. 같은 조사를 다시 하려면 이 표의 방법대로 하면 된다.

## 요약

| 분류 | 사이트 수 | 과거 기사 채우기 방법 |
| --- | --- | --- |
| 10년 이상 보관 사이트맵 | 15 | 보관 사이트맵 (1순위) |
| 1~3년 보관 사이트맵 | 10 | 보관 사이트맵, 그 이전은 목록 페이지 역순 또는 뉴스 DB |
| 1년 미만 | 1 | 같음 |
| 최근 글만 (뉴스 사이트맵 이틀치 또는 RSS뿐) | 22 | 목록 페이지 역순 (사이트마다 가능 여부 확인 필요) 또는 뉴스 DB |
| 사이트맵 없음 | 4 | 목록 페이지 역순 |

## 10년 이상 (15곳)

| 사이트 | 가장 오래된 날짜 | 비고 |
| --- | --- | --- |
| sisajournal.com 시사저널 | 1990-10 | 하위 파일 423 |
| imaeil.com 매일신문 | 1993-05 | 연도별 파일(1993.xml, 2004.xml …) 51개, 표본 4개에 13만 건 |
| donga.com 동아일보 | 1996-09 | 두 달 단위 `.xml.gz` (2026-07~08 파일 = 21,096건) |
| seoul.co.kr 서울신문 | 2000-01 | 하위 파일 646 |
| dt.co.kr 디지털타임스 | 2001-01 | 연도별 26개 |
| pressian.com 프레시안 | 2001-08 | 하위 파일 304 |
| weekly.donga.com 주간동아 | 2002-10 | 5년 단위 `.xml.gz` |
| mbn.mk.co.kr MBN | 2005-12 | 하위 파일 5,291 |
| hankookilbo.com 한국일보 | 2010-01 | 하위 파일 5,935 |
| news.kbs.co.kr KBS | 2010-01 | 하위 파일 6,137 |
| edaily.co.kr 이데일리 | 2010-06 | 하위 파일 11,952 |
| ichannela.com 채널A | 2011-11 | 하위 파일 181 |
| yonhapnewstv.co.kr 연합뉴스TV | 2014-03 | 날짜별 색인(datemap) |
| mk.co.kr 매일경제 | 2016-10 | 하위 파일 7,302 |
| koreajoongangdaily.joins.com | 2017-07 | 하위 파일 295 |

## 1~3년 (10곳)과 1년 미만 (1곳)

| 사이트 | 가장 오래된 날짜 |
| --- | --- |
| newsis.com 뉴시스 | 2023-02 |
| joongang.co.kr 중앙일보 | 2024-01 |
| koreaherald.com | 2024-12 (일별 파일) |
| asiatoday.co.kr | 2025-01 |
| munhwa.com 문화일보 | 2025-01 |
| news.jtbc.joins.com JTBC | 2025-01 (일별 파일) |
| dailian.co.kr 데일리안 | 2025-03 |
| mydaily.co.kr | 2025-07 |
| hankyung.com 한국경제 | 2025-10 (일별 파일 366개 = 최근 1년) |
| korea.kr 정책브리핑 | 2025-10 (종류별 파일, 정책뉴스 3,445 · 보도자료 6,986 등) |
| mt.co.kr 머니투데이 | 2026-01 (1년 미만) |

## 최근 글만 (22곳)

biz.chosun.com, monthly.chosun.com, weekly.chosun.com, yna.co.kr(연합뉴스), ytn.co.kr, hani.co.kr(한겨레), khan.co.kr(경향), weekly.khan.co.kr,
segye.com, sedaily.com, fnnews.com, heraldbiz.com, economist.co.kr, isplus.joins.com, nocutnews.co.kr, ohmynews.com, mediatoday.co.kr,
newdaily.co.kr, sisain.co.kr, jbnews.com, bloter.net, zdnet.co.kr.
뉴스 사이트맵(최근 이틀)이나 RSS만 있다. 과거 기사는 목록 페이지를 페이지 번호·날짜로 거꾸로 넘겨야 하고, 몇 쪽까지 열리는지는 사이트마다 다르다.

## 사이트맵 없음 (4곳)

etnews.com(전자신문), osen.mt.co.kr, wowtv.co.kr(조사 중 응답 시간 초과), news.suwon.go.kr(수원시). 목록 페이지 역순만 가능하다.

## 시사점

- 주요 일간지·방송 15곳은 10년 이상 거슬러 올라갈 수 있다. 다만 사이트 하나 10년이면 약 100만 건, 2초 간격으로 한 달 가까이 걸린다
  (`docs/how-collection-works.md` "입구 파일이 보여 주는 범위와 과거 기사").
- 연합뉴스·한겨레·경향·조선비즈처럼 사이트맵이 최근 것뿐인 곳이 많아, 언론사 전체를 몇 년 치 고르게 채우려면 뉴스 DB(빅카인즈 등) 계약이 현실적이다.
- 과거 기사 채우기는 보관 사이트맵이 있는 곳부터, 최근 기간 묶음부터 시작하는 것이 효율적이다.
