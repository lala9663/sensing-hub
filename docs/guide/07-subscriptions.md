# 구독

메뉴 **구독** (`#/digests`). **구독(일간 다이제스트)** 은 매일 정한 시각에 지난번 이후 새로 수집된 기사를 감시별·토픽별로 묶어
메일이나 Teams로 보내 주는 기능입니다. 현업은 아침에 정리된 보고를 받아 윗선에 전달하므로, 물어보는 챗봇보다 받아 보는 구독을 먼저 만들었습니다.
권한은 VIEWER 이상이고 자기 구독만 봅니다(ADMIN은 전체).

## 이 탭에서 하는 일

- **새 구독**: 이름, 채널(메일 / Teams), 대상(메일 주소 또는 Teams 웹후크 주소), 시각(KST 정시, 기본 08:00), 필터 체크박스.
  **키워드 감시 \*** 는 필수라 하나 이상 골라야 저장됩니다(하루 새 문서가 1만 건이 넘어 감시 없는 구독은 상한 200건에서 잘리기 때문).
  감시 목록에는 내 감시와 공용(관리자가 만든) 감시가 나옵니다. 토픽 · 거래처(회사 신호 포함) · 소스(관리자만)는 더 받고 싶을 때 고르는 선택 항목입니다.
- **내 구독** 목록: 이름·채널·대상·시각·필터·켜짐/꺼짐·마지막 발송. 행마다
  - **미리보기**: 지금 보낸다면 나갈 메일(HTML/텍스트 탭)과 기간·문서 수·신호 수(200건 초과면 "잘림" 표시).
  - **지금 보내기**: 시각을 기다리지 않고 바로 보내고 결과(발송·빈 결과·실패, 문서 수, 오류)를 알림으로 보여 줍니다.
  - **기록**: 보낸 기록(결과·기간·문서 수·보낸 시각·오류)과 그때 보낸 본문.
  - **수정 · 끄기/켜기 · 삭제**. 감시 없이 만든 예전 구독은 필터 칸에 "감시 선택 필요"가 붙고, 수정 창에서 감시를 고르라고 안내합니다
    (그동안에도 발송은 계속되고, 끄기/켜기는 감시 없이도 됩니다).

## 어떻게 동작하나

- 백엔드 스케줄러가 5분마다 KST 기준 현재 시가 `hourKst`이고 오늘 아직 보내지 않은 켜진 구독을 찾아,
  **지난 발송 이후(`last_sent_at`, 처음은 24시간 전) 새로 수집된(`collected_at`) 문서**를 묶어 보냅니다.
- **묶는 순서**: ① 감시별(검색어별, 구독에 감시를 골랐으면 그 감시들만) → ② 그 밖의 문서는 토픽별(정책·규제 → 입찰·공고 → 지원사업 → 자사 → 경쟁사 → 고객사 → 협력사 →
  산업동향 → 인사·조직 → 사건·사고 → 기타) → ③ 분류 전. 항목은 "제목 — 출처 도메인 · 발행일 · 링크"이고 요약이 있으면 다음 줄에 붙습니다(언론 기사는 요약 없음).
- **거래처 신호**: 필터에 거래처를 골랐거나 필터가 비어 있으면 기간 안의 회사 신호를 맨 앞 "거래처 신호" 절로 넣습니다(회사별, HIGH 먼저).
  신호가 있으면 문서가 0건이어도 보냅니다([04-companies.md](04-companies.md)).
- **필터** `{"watchIds":[],"sourceIds":[],"topics":[],"companyIds":[]}`는 하나라도 맞으면 포함(합집합)이며 상한은 문서·신호 각 200건입니다.
  구독을 만들 때와 `filters`를 보내 수정할 때 `watchIds`가 비면 400 "키워드 감시를 하나 이상 골라 주세요."입니다.
  감시 없이 만든 예전 구독(모두 비면 전체)은 그대로 발송되고, `filters`를 생략한 수정(이름·시각·켜기/끄기)도 됩니다.
- **결과 기록**(`digest_run`): 보내면 `SENT`. 문서도 신호도 0건이면 보내지 않고 `EMPTY`로만 기록하고 `last_sent_at`은 갱신합니다(빈 날이 다음 기간을 늘리지 않게).
  전송 실패는 `FAILED`로 남기고 `last_sent_at`을 그대로 두어 같은 시간대의 다음 틱에 다시 시도합니다. 구독 하나의 실패는 다른 구독을 막지 않습니다.
  보낸 본문은 `digest_run.body_text`에 남아 "기록"과 `/api/v1/digests/{id}/runs`로 다시 볼 수 있습니다.
- **채널**: 메일은 HTML(제목 `[sensing-hub] <구독 이름> 다이제스트 <날짜> (N건)`)입니다. 화면의 **Teams**는 API 값 `WEBHOOK`이고
  보내는 형식은 예전 웹훅 그대로 `{"text": ...}` 평문입니다(이름만 Teams로 바꿨고 Teams 전용 카드 형식은 아직 없습니다).
  주소는 사이트 등록과 같은 검사(http(s) 절대 주소, 내부망 거부)를 거칩니다.
- 네이버 검색 결과(표시 전용)는 구독 메일에 들어가지 않습니다([06-naver-news.md](06-naver-news.md)).

## 설정

| 변수 (백엔드) | 기본값 | 설명 |
| --- | --- | --- |
| `SENSING_MAIL_HOST` / `SENSING_MAIL_PORT` | (없음) / `587` | SMTP 서버. 호스트가 비어 있으면 메일 구독은 `FAILED`("메일 서버가 설정되지 않았습니다")로 남고 Teams(웹훅) 구독만 보낸다 |
| `SENSING_MAIL_USERNAME` / `SENSING_MAIL_PASSWORD` | (없음) | SMTP 계정. 비밀번호는 로그·오류·응답 어디에도 남기지 않는다 |
| `SENSING_MAIL_FROM` | (없음) | 보내는 주소. 비우면 `SENSING_MAIL_USERNAME`, 그것도 비면 메일 서버가 정한다 |
| `SENSING_MAIL_AUTH` / `SENSING_MAIL_STARTTLS` | `true` / `true` | 인증 없는 사내 릴레이(25번 포트)면 둘 다 `false` |
| `SENSING_DIGEST_ENABLED` / `SENSING_DIGEST_TICK_MS` | `true` / `300000` | 구독 발송 스케줄러 켜기·확인 간격 |

변수·API 경로·DB 테이블(`digest_subscription`·`digest_run`)은 호환을 위해 예전 이름 "digest"를 그대로 씁니다.

## API

```bash
# 구독 만들기 (매일 hourKst시 KST에 지난 발송 이후 새 문서를 감시별·토픽별로 묶어 메일 또는 Teams(WEBHOOK)로). watchIds는 필수
curl -X POST http://127.0.0.1:8080/api/v1/digests -H 'X-User: viewer' -H 'Content-Type: application/json' \
     -d '{"name":"아침 보고","channel":"EMAIL","target":"me@example.com","hourKst":8,"filters":{"watchIds":[1],"topics":["정책·규제","입찰·공고"]}}'
# 내 구독 (ADMIN은 전체)
curl http://127.0.0.1:8080/api/v1/digests -H 'X-User: viewer'
# 수정 · 삭제
curl -X PUT http://127.0.0.1:8080/api/v1/digests/1 -H 'X-User: viewer' -H 'Content-Type: application/json' \
     -d '{"channel":"WEBHOOK","target":"https://<Teams 워크플로가 준 웹후크 주소>"}'
curl -X DELETE http://127.0.0.1:8080/api/v1/digests/1 -H 'X-User: viewer'
# 미리보기 (text·html·documentCount) · 바로 보내기 (digest_run에 기록) · 보낸 기록
curl -X POST http://127.0.0.1:8080/api/v1/digests/1/preview -H 'X-User: viewer'
curl -X POST http://127.0.0.1:8080/api/v1/digests/1/send-now -H 'X-User: viewer'
curl 'http://127.0.0.1:8080/api/v1/digests/1/runs?limit=20' -H 'X-User: viewer'
```

## 한계와 주의

- 시각은 정시 단위(`hourKst` 0~23)이고 하루 한 번입니다. 스케줄러가 5분마다 확인하므로 정각에서 몇 분 늦게 나갈 수 있습니다.
- Teams 채널은 이름만 Teams이고 보내는 형식은 `{"text": ...}`(Slack·Mattermost 수신 웹훅 형식)입니다. Teams 워크플로가 이 형식을
  받는지는 워크플로 템플릿에 따라 다르므로(Adaptive Card만 받는 템플릿도 있다) 처음에는 **지금 보내기**로 채널에 실제로 올라오는지 확인하세요.
  Teams 카드(Adaptive Card) 형식으로 보내는 것은 후속입니다. 예전 Office 365 커넥터 주소(`webhook.office.com`)는 Microsoft가 2026년 5월에 종료했습니다.
- 메일 서버(`SENSING_MAIL_HOST`)가 없으면 메일 구독은 실패로 기록됩니다. Teams(웹훅)로 먼저 확인해 보는 편이 쉽습니다.
- 메일 제목·본문 머리와 꼬리는 지금도 "다이제스트"("sensing-hub 일간 다이제스트 — <구독 이름>")라고 적혀 나갑니다.
- 남은 것: 회사별 구독 화면. 지금은 구독 화면의 거래처 필터로 회사를 고릅니다.
