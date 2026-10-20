# 거래처

메뉴 **거래처** (`#/companies`)와 관리자 메뉴 **케이던시아 연동** (`#/admin/caidentia`). 문서 중심("오늘 들어온 기사") 위에
**회사 중심**("어느 거래처에 무슨 일")을 한 층 얹은 기능입니다. 현업이 삼성SDS 구매·공급망관리 SaaS **케이던시아(Caidentia)** 를 함께 쓰므로
거래처 목록을 케이던시아와 맞추고 위험 신호를 보냅니다.

## 이 탭에서 하는 일

- **회사 목록**: 이름·사업자번호·신호 수·HIGH 수·최근 신호·감시 상태(회사 감시를 제외했으면 "제외"), "보관 포함" 체크. 권한은 VIEWER 이상.
- **회사 등록** (REGISTRAR 이상): 이름(필수)·별칭(쉼표)·사업자등록번호·DART 고유번호·외부 ID(케이던시아 등)·태그. 등록하면 이름·별칭을 키워드로 한
  회사 감시가 생기고, 사업자번호·DART 고유번호가 있으면 국세청·DART·조달청 점검 대상이 됩니다.
- **CSV로 여러 건 넣기**: 첫 줄 헤더 `name,aliases,biz_no,corp_code,external_id,tags`(별칭·태그는 `;`로 구분). 사업자번호가 같으면 갱신합니다(upsert).
- **회사 상세** (`#/companies/{id}`): 종류별 최근 신호, 최근 신호, 연결 문서. 관리자에게는 회사 감시의 소스 링크.
- **전체 신호** (`#/companies/signals`): 기간(오늘·3일·7일·30일·전체)·심각도·종류 칩으로 모든 회사의 신호를 봅니다.
- 관리자 **케이던시아 연동**: 연결 주소·목업 표시, 마지막 동기화 결과(공급사·생성·갱신·보관·그대로), 이벤트 전달 대기/보낸 수 카드,
  **지금 동기화 / 지금 전달** 단추, 동기화 실행 기록, (데모 전용) 케이던시아가 받은 이벤트.
- 구독에서 거래처를 고르면 그 회사의 신호가 메일 맨 앞 "거래처 신호" 절로 들어갑니다([07-subscriptions.md](07-subscriptions.md)).

## 어떻게 동작하나

회사(`company`: 이름·별칭·사업자등록번호·DART 고유번호·외부 ID·태그)를 등록하면:

1. 이름+별칭을 키워드로 한 **회사 감시**(`회사:<이름>`)가 생겨, 수집 기사 대조로 걸린 기사를 `document_company`(WATCH)로 연결합니다
   ([05-watches.md](05-watches.md). 네이버 저장·분석 모드(`store`)에서 키가 있으면 네이버 검색 360분 주기도 붙습니다). 관리자가 만든 회사 감시는 공용입니다.
2. 기사의 보강 태그(고유명사)가 회사 이름·별칭과 **정확히**(대소문자·공백 무시) 일치하면 TAG로 연결합니다. 부분 일치·유사도 매칭은 오탐 때문에 하지 않습니다.
3. 연결된 기사의 토픽이 사건·사고·인사·조직이면 `company_signal`(NEWS, MEDIUM)을 남깁니다.
4. 첫 회사를 만들 때 **점검 소스**(`source.kind = CHECK`, 설정의 `check`가 점검 종류) 네 개가 생겨 모든 회사를 주기적으로 외부 API로 확인합니다.

| 점검 종류 | 주기 | 하는 일 | 신호 |
| --- | --- | --- | --- |
| `nts_status` | 하루 1회 | 사업자번호가 있는 회사의 국세청 사업자등록 상태 | BIZ_STATUS — 폐업·휴업 HIGH, 미등록 MEDIUM, 과세유형 변경 LOW |
| `dart_corp_map` | 주 1회 | DART 고유번호 전체 목록(corpCode.xml)에서 corp_code가 빈 회사의 이름·별칭을 공백·"(주)"·"주식회사"·"㈜"를 지우고 **정확히** 맞춰, 하나(여럿이면 상장사 하나)일 때만 채운다(모호하면 로그만) | 없음 |
| `dart_disclosure` | 6시간 | 고유번호가 있는 회사마다 최근 3일 공시를 문서(`document_company` API)로 저장하고, 보고서명에 든 말로 신호 | DISCLOSURE — HIGH(회생절차·파산·영업정지·상장폐지·감사의견 거절·부적정·자본잠식·불성실공시·횡령·배임), MEDIUM(소송·최대주주 변경·대표이사 변경·유상증자·합병·분할·영업양수도·거래정지), LOW(그 밖의 주요사항보고서·정정공시). 일반 정기·수시 공시는 문서만 |
| `g2b_debarment` | 하루 1회 | 조달청 나라장터 부정당업자(입찰참가자격 제한) 사업자번호별 조회(`getUnptRsttCorpInfo02`) | DEBARMENT HIGH |

- 같은 신호는 fingerprint(상태 코드+폐업일, 기사 url_hash, 공시 접수번호, 제재 시작일+기관)로 한 번만 기록합니다. 그래야 매일 확인해도 구독 메일이 조용합니다.
- 키가 없으면(공공데이터포털 `data_go_kr`: 국세청 사업자등록 상태조회·나라장터 사용자정보 서비스 활용신청 필요, DART `dart`) 그 점검 소스는 다른 API 소스처럼
  관리자 큐 `API_KEY`에서 기다립니다 ([09-admin.md](09-admin.md#api-키)).
- 삭제는 보관(ARCHIVED)이며 그 회사 감시는 제외됩니다. 회사는 그대로인데 회사 감시만 제외하면 거래처 화면의 감시 상태가 "제외"로 보이고,
  새 기사는 그 회사에 연결되지 않습니다.
- 설계 이유는 [../decisions.md](../decisions.md), 소스 목록은 [../requirements-mapping.md](../requirements-mapping.md)의 "공급사 리스크 소스".

### 케이던시아 연동 (목업)

실제 API가 아직 없어 백엔드 안에 **가짜 케이던시아**(`/mock/caidentia/api`: 공급사 마스터 `GET/PUT suppliers`, 리스크 이벤트 `POST/GET risk-events`,
헤더 `X-Caidentia-Key` 인증 흉내, 가공의 공급사 9곳 메모리 씨앗)를 두고, 연동 코드(`io.sensinghub.caidentia`: `CaidentiaClient` 인터페이스 + RestClient
구현, 10초 제한)는 그것을 외부 시스템처럼 HTTP로 부릅니다.

- **공급사 마스터 동기화**(`CaidentiaSyncService`, 매일 06:00 KST): `external_id`(= supplierId) → 사업자번호 순으로 회사를 맞춰 생성·갱신(보관됐으면 복구)하고
  INACTIVE 공급사는 보관합니다. 결과는 `caidentia_sync_run`(V10: fetched·created·updated·archived·unchanged·error)에 남습니다.
- **리스크 이벤트 전달**(`CaidentiaPushService`, 5분마다): 케이던시아 공급사인 회사의 HIGH·MEDIUM 신호를
  `{supplierId, companyName, kind, severity, title, observedAt, detail}`로 보내고 `company_signal.pushed_at`을 찍습니다. 실패는 다음 틱에 재시도합니다.
- **실제 케이던시아가 나오면 `SENSING_CAIDENTIA_BASE_URL`·`SENSING_CAIDENTIA_API_KEY`와 `RestCaidentiaClient`의 필드 매핑만 바꿉니다.**

## 설정

| 변수 (백엔드) | 기본값 | 설명 |
| --- | --- | --- |
| `SENSING_CAIDENTIA_BASE_URL` | `http://127.0.0.1:<port>/mock/caidentia/api` | 케이던시아 API 주소. **실제 시스템이 나오면 이것만 바꾼다** (기본은 이 서버 안의 가짜 케이던시아) |
| `SENSING_CAIDENTIA_API_KEY` | `mock-key` | 헤더 `X-Caidentia-Key`로 보내는 키. 가짜 서버도 같은 값을 요구한다 |
| `SENSING_CAIDENTIA_ENABLED` | `true` | 스케줄러(동기화·전달) 켜기. 꺼도 화면의 "지금 동기화 / 지금 전달"은 된다 |
| `SENSING_CAIDENTIA_SYNC_CRON` | `0 0 6 * * *` | 공급사 마스터 동기화 시각 (KST) |
| `SENSING_CAIDENTIA_PUSH_ENABLED` / `SENSING_CAIDENTIA_PUSH_TICK_MS` | `true` / `300000` | HIGH·MEDIUM 회사 신호를 리스크 이벤트로 전달 (5분마다) |
| `SENSING_CAIDENTIA_MOCK_ENABLED` | `true` | 가짜 케이던시아 서버(`/mock/caidentia/api`) 띄우기. 실제 시스템을 보게 되면 끈다 |

점검 소스가 쓰는 공개 API 키(`data_go_kr`·`dart`)는 관리자 API 키 화면에서 넣습니다 ([09-admin.md](09-admin.md#api-키)).

## API

```bash
# 회사 등록 (REGISTRAR 이상. 회사 감시 + 점검 소스가 함께 생긴다)
curl -X POST http://127.0.0.1:8080/api/v1/companies -H 'X-User: registrar' -H 'Content-Type: application/json' \
     -d '{"name":"나부품","aliases":["NABU"],"bizNo":"123-45-67890","corpCode":"00123456","externalId":"CAI-001","tags":["1차 공급사"]}'
# 목록(신호 수·최근 신호·연결 문서 수) · 상세(최근 신호·문서 20건, 종류별 최근 1건 latestByKind)
curl http://127.0.0.1:8080/api/v1/companies -H 'X-User: viewer'
curl http://127.0.0.1:8080/api/v1/companies/1 -H 'X-User: viewer'
# PUT /api/v1/companies/1 수정, DELETE /api/v1/companies/1 보관,
# POST /api/v1/companies/import 는 JSON 배열 또는 CSV(name,aliases(;구분),biz_no,...) upsert
# 회사 신호 (심각도 → 시각 내림차순). kind=BIZ_STATUS|NEWS|DISCLOSURE|DEBARMENT · 회사별은 GET /api/v1/companies/1/signals?since=
curl 'http://127.0.0.1:8080/api/v1/company-signals?since=2026-10-15&severity=HIGH' -H 'X-User: viewer'

# 관리자: 케이던시아 연동 상태 · 지금 동기화 · 지금 전달 · (데모) 가짜 케이던시아가 받은 이벤트
curl http://127.0.0.1:8080/api/v1/integrations/caidentia -H 'X-User: admin'
curl -X POST http://127.0.0.1:8080/api/v1/integrations/caidentia/sync-now -H 'X-User: admin'
curl -X POST http://127.0.0.1:8080/api/v1/integrations/caidentia/push-now -H 'X-User: admin'
curl http://127.0.0.1:8080/api/v1/integrations/caidentia/received-events -H 'X-User: admin'
```

## 한계와 주의

- 남은 것: DART 재무 신호(FINANCIAL), 회사별 구독 화면, 실제 케이던시아 API 연결(SDS가 API를 주면 base-url·매핑 교체).
- 케이던시아는 **목업**입니다. "케이던시아가 받은 이벤트"는 가짜 서버의 수신함이라 실제 연동 뒤에는 사라집니다.
- 기사 연결은 정확 일치만 하므로, 기사에 쓰이는 다른 이름(영문명·약칭)은 별칭으로 넣어야 걸립니다.
- 조달청 API의 응답 항목명은 포털에 공개돼 있지 않아 커넥터 config `fields`로 바꿀 수 있게 했습니다(`worker/src/sensing_worker/apis/g2b_debarment.py`).
