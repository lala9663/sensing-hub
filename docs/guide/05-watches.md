# 키워드 감시

메뉴 **키워드 감시** (`#/watches`). 현업(대외협력·커뮤니케이션)은 사이트가 아니라 키워드(자사·경쟁사·이슈) 단위로 보도를 봅니다.
키워드 묶음을 등록하면 **등록된 언론사에서 수집한 기사** 가운데 그 말이 나온 기사를 감시별로 모읍니다.

## 이 탭에서 하는 일

- **새 감시**: 이름·키워드·제외어(쉼표 또는 줄바꿈으로 나눔). 제목·본문에 키워드가 나오면 걸리고, 제외어가 제목에 있으면 뺍니다.
  감시를 만든 뒤 새로 수집되는 기사부터 대조합니다. VIEWER가 만들면 관리자 승인 뒤 진행됩니다.
- **감시 목록**: 공용 감시를 먼저 만든 순서로, 그다음 내 감시를 보여 주고 행마다 "공용"/"내 감시"를 표시합니다(관리자에게는 남의 감시도 "개인"으로).
  - 키워드 칩을 누르면 그 키워드로 걸린 기사, 문서 수를 누르면 이 감시에 걸린 기사 전체를 문서 화면(`#/documents?watch=`)에서 봅니다.
  - 키워드·제외어 수정(설정 새 버전). 공용 감시는 관리자만 고칩니다.
  - **네이버 실시간**: 네이버 키가 있을 때 감시 키워드로 네이버 최신 뉴스를 표시 전용으로 봅니다(출처 "NAVER 검색 결과") — [06-naver-news.md](06-naver-news.md#감시-화면의-네이버-실시간).
  - 관리자: 행의 **제외**, 목록 위 **제외된 감시 보기**, 제외된 행의 **다시 켜기**, 소스 상세 링크.
- 챗봇에 "내 감시 목록"·"메타넷 감시 만들어 줘"라고 해도 됩니다(`list_watches`·`create_watch`, [08-chatbot.md](08-chatbot.md)).

## 어떻게 동작하나

- **수집 기사 대조 모드(기본)**: 워커가 기사(그리고 DART 공시 같은 API 문서)를 새로 저장하거나 고칠 때마다 제외되지 않은 모든 감시의 키워드와 대조해,
  어느 감시의 어느 검색어에 걸렸는지 `document_watch`에 남깁니다(같은 기사가 여러 감시에 걸려도 문서는 하나, 감시당 처음 걸린 검색어 하나).
  구독 메일이 이 표를 읽습니다. 회사 감시면 그 회사에도 연결됩니다([04-companies.md](04-companies.md)).
- 감시는 만들자마자 `ACTIVE`(화면 "수집 기사 대조 중")가 되지만 스스로 수집하지 않으므로 `next_run_at`이 비어 있어 스케줄러가 작업을 넣지 않고,
  관리자 키 큐에도 쌓이지 않습니다. 그래서 **커버리지는 등록 언론사 수가 정합니다** — 감시를 만들었다고 새로 긁는 곳이 생기지 않습니다.
- **대조 규칙**: 대소문자·공백 차이를 무시하고, 띄어쓰기가 다르면 다른 말입니다. 영문·숫자로 시작하거나 끝나는 키워드는 그 끝 바로 바깥이
  라틴 문자·숫자가 아니어야 걸립니다("DS"는 "삼성전자 DS부문"·"DS투자증권"·"(DS)"에 걸리고 "applauds"·"MDS테크"·"GADS"·"DSR"에는 안 걸림).
  한글 쪽 끝은 조사 때문에 부분 문자열입니다("반도체"는 "반도체가"에 걸림). 제외어도 같은 규칙이고, 네이버 실시간의 제외어도 같습니다.
  예시 표는 [../how-collection-works.md](../how-collection-works.md)의 "수집 뒤: 키워드 감시 대조".
- **저장 구조**: 감시 하나는 `source` 한 행(`kind = WATCH`, `watch_name`)이고 키워드는 설정 버전
  config(`{"method":"API","provider":"naver_news","naver":false,"keywords":[...],"exclude":[...]}`, `naver`가 없으면 V13 이전 감시로 네이버 모드)에 둡니다.
  그래서 키워드 수정은 설정 새 버전 활성화이고, 롤백·일시정지·제외 같은 소스 조치를 그대로 씁니다. V13 마이그레이션이 키 없이 멈춰 있던 감시
  (REVIEW `API_KEY_REQUIRED`/`API_KEY_INVALID`)를 대조 모드로 옮겼습니다.
- **네이버 저장·분석 모드**(`SENSING_NAVER_MODE=store`, 법무 확인 뒤에만): 키가 있으면 감시에 네이버 검색 수집이 함께 붙습니다(화면 "네이버 검색 함께",
  60분·회사 감시 360분). 기본(`display`)에서는 붙지 않습니다 — [06-naver-news.md](06-naver-news.md).

### 공용 감시와 보는 범위

관리자(ADMIN 역할 사용자)가 만든 감시는 **공용**입니다. 모든 사용자의 목록에 "공용"으로 보이고, 키워드 칩(키워드별 기사)·문서 수 링크·"네이버 실시간"을
누구나 쓸 수 있지만 수정·제외는 관리자만 합니다(공용 감시를 다른 사람이 `PUT`하면 403). 일반 사용자가 만든 감시는 만든 사람과 관리자만 봅니다
(남이 보면 404). 목록(`GET /api/v1/watches`) 응답 행마다 `shared`·`mine`·`editable`이 붙고, 단건 조회와 네이버 실시간도 같은 규칙입니다.
관리자가 만든 회사 감시(`회사:<이름>`)도 공용입니다.

지금 공용 감시는 네이버 뉴스 탭 주요 키워드와 같은 9개(반도체, 메타넷, 메타넷 글로벌, 삼성전자, DS, DX, DSR, 테슬라, 하이닉스)이고,
예전 감시 12개(자사 보도·반도체 동향·회사 감시 9개·개인 감시 1개)는 제외해 두었습니다.

### 제외와 다시 켜기

감시를 끄는 것은 관리자의 소스 조치 "제외"(`POST /api/v1/sources/{id}/actions/exclude`, 감사 로그 `SOURCE_EXCLUDE`)이고, 감시 행의 "제외" 버튼도 이것을 부릅니다.
제외한 감시는 목록에서 빠지고 워커도 대조하지 않지만, 이미 걸린 기사(`document_watch`)와 단건 조회는 남습니다.

관리자는 **"제외된 감시 보기"**(`?include_excluded=true`, 관리자가 아니면 무시)로 함께 보고, 제외된 행의 **"다시 켜기"**
(`POST /api/v1/watches/{id}/reactivate`, ADMIN만, 감사 로그 `WATCH_REACTIVATED`)로 되돌립니다. 다시 켜면 만들 때와 같은 모드 규칙을 따릅니다:
대조 모드는 `ACTIVE` + `next_run_at` NULL(작업 없음), 네이버 저장·분석 모드(store + 키)는 `ACTIVE`로 두고 ONBOARD(MANUAL)를 넣어 워커가 키를 확인합니다.
마지막 등록 요청에는 "관리자가 감시를 다시 켰습니다." 결과를 적습니다. 같은 이름의 감시가 이미 켜져 있거나 보관(ARCHIVED)한 회사의 감시면 409입니다.
관리자 소스 화면(`#/admin/sources/{id}`)의 제외된 감시에도 같은 "다시 켜기"가 있습니다.

### 이미 쌓인 기사 대조와 다시 판정

감시는 새로 수집되는 기사부터 대조합니다. 이미 쌓인 기사는 한 번 대조할 수 있습니다:

```bash
SENSING_DB_DSN=postgresql://sensing:sensing@127.0.0.1:5433/sensing python -m sensing_worker.backfill_watch --days 30
```

영문 단어 경계 규칙(2026-10-20) 전에 남은 기록은 다시 판정해 정리합니다. 먼저 `--dry-run`으로 지울 건수와 예시를 보고 실행합니다:

```bash
SENSING_DB_DSN=postgresql://sensing:sensing@127.0.0.1:5433/sensing python -m sensing_worker.backfill_watch --recheck --days 30 --dry-run
SENSING_DB_DSN=postgresql://sensing:sensing@127.0.0.1:5433/sensing python -m sensing_worker.backfill_watch --recheck --days 30
```

검색어가 저장된 제목·본문에 단어 속으로만 있던 기록을 지우고(회사 감시면 그 회사 연결 `document_company` WATCH와 그 연결로 생긴 NEWS 신호도),
같은 감시의 다른 키워드가 걸리면 검색어만 고치고, 새로 걸리는 것은 더합니다(트랜잭션 하나, 다시 돌려도 바뀌는 것 없음). 언론 기사는 본문을 저장하지
않아 본문에서만 걸린 기록은 근거가 없어 두며, `--drop-unverifiable`을 붙이면 그중 영문 키워드 기록도 지웁니다(본문에 제대로 된 검색어가 있던
기록도 함께 지워질 수 있음). 네이버에서 받은 문서와 제외된 감시의 기록은 보지 않습니다. 건수 추정은
[../how-collection-works.md](../how-collection-works.md)의 "수집 뒤: 키워드 감시 대조".

## 설정

감시 자체의 환경 변수는 없습니다. 네이버 검색을 감시에 붙일지는 `SENSING_NAVER_MODE`(기본 `display` = 붙이지 않음)가 정합니다 —
[06-naver-news.md](06-naver-news.md#설정).

## API

```bash
# 키워드 감시 등록 (수집 기사 대조. SENSING_NAVER_MODE=store이고 네이버 키가 있으면 네이버 검색도 60분 주기로. VIEWER는 승인 대기)
curl -X POST http://127.0.0.1:8080/api/v1/watches -H 'X-User: registrar' -H 'Content-Type: application/json' \
     -d '{"name":"자사 보도","keywords":["메타넷","메타넷글로벌"],"exclude":["채용"]}'
# 공용 + 내 감시 (행마다 shared·mine·editable) · 단건
curl http://127.0.0.1:8080/api/v1/watches -H 'X-User: registrar'
curl http://127.0.0.1:8080/api/v1/watches/1 -H 'X-User: registrar'
# 키워드 수정 = 설정 새 버전
curl -X PUT http://127.0.0.1:8080/api/v1/watches/1 -H 'X-User: registrar' -H 'Content-Type: application/json' -d '{"keywords":["메타넷","신사업"]}'
# 관리자: 제외된 감시까지 보기 · 다시 켜기 (제외는 POST /api/v1/sources/1/actions/exclude)
curl 'http://127.0.0.1:8080/api/v1/watches?include_excluded=true' -H 'X-User: admin'
curl -X POST http://127.0.0.1:8080/api/v1/watches/1/reactivate -H 'X-User: admin'
# 이 감시에 걸린 기사
curl 'http://127.0.0.1:8080/api/v1/documents?watch_id=1' -H 'X-User: registrar'
```

네이버 실시간(`GET /api/v1/watches/{id}/naver-news`)은 [06-naver-news.md](06-naver-news.md#api)에 있습니다.

## 한계와 주의

- 등록된 언론사에서 수집한 기사만 대조합니다. 보고 싶은 매체가 없으면 먼저 사이트를 등록해야 합니다([01-registrations.md](01-registrations.md)).
- 언론 기사는 본문을 저장하지 않으므로, 수집 때 본문에서만 걸린 기사는 나중에 근거를 다시 확인할 수 없습니다.
- 짧은 영문 키워드(DS·DX·DSR)는 단어 경계 규칙으로도 다른 뜻의 기사가 섞일 수 있습니다. "삼성전자 DS"처럼 더 긴 키워드를 쓰는 편이 정확합니다.
- 네이버 뉴스 웹은 robots.txt가 전체 차단이라 크롤링하지 않습니다. `news.naver.com`을 사이트처럼 등록하면 "키워드 감시로 등록하세요"(`KEYWORDS_REQUIRED`)로 안내합니다.
