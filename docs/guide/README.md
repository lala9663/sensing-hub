# 기능 안내 (화면 탭별)

화면 왼쪽 메뉴 순서대로 기능을 한 문서씩 설명합니다. 모든 문서는 같은 틀입니다:
**이 탭에서 하는 일**(화면 사용법) → **어떻게 동작하나**(규칙과 숫자) → **설정**(환경 변수) → **API**(요청 예시) → **한계와 주의**.

## 목차

| 메뉴 | 문서 | 한 줄 소개 |
| --- | --- | --- |
| 등록·내 요청 | [01-registrations.md](01-registrations.md) | 사이트 주소 하나(또는 수백 개)를 넣으면 점검·탐색·품질 검사·첫 수집까지 자동으로 진행하고, 내 요청의 진행을 본다 |
| 문서 | [02-documents.md](02-documents.md) | 수집한 기사·보도자료를 기간·토픽·검색어·사이트·기사 종류로 찾아보고, LLM이 붙인 분류·요약을 본다 |
| 이슈 | [03-issues.md](03-issues.md) | 같은 사건을 다룬 여러 언론사 기사를 한 묶음으로 보고, 많이 보도된 순으로 훑는다 |
| 거래처 | [04-companies.md](04-companies.md) | 거래처(회사)를 등록하면 관련 기사·공시·사업자 상태·제재를 위험 신호로 모으고, 케이던시아(목업)와 주고받는다 |
| 키워드 감시 | [05-watches.md](05-watches.md) | 자사·경쟁사·이슈 키워드를 등록하면 수집한 기사 중 그 말이 나온 기사를 감시별로 모은다 |
| 네이버 뉴스 | [06-naver-news.md](06-naver-news.md) | 네이버 뉴스를 아무 검색어로 찾아본다 (표시 전용, 저장하지 않음). 주요 키워드 칩 포함 |
| 구독 | [07-subscriptions.md](07-subscriptions.md) | 매일 정한 시각에 감시·토픽·거래처별로 묶은 새 기사 보고를 메일이나 Teams로 받는다 (키워드 감시 필수) |
| 챗봇 | [08-chatbot.md](08-chatbot.md) | 수집 자료에 자연어로 묻고 근거 링크와 함께 답을 받는다. 키워드만 넣으면 LLM 없이 바로 검색 |
| 관리자 | [09-admin.md](09-admin.md) | 처리 대기 큐, 소스(과거 기사 채우기 포함), 운영 지표, API 키, 주요 키워드, 케이던시아 연동 |

탭이 아닌 공통 설정: [LLM 연결과 무료 한도](llm.md) — 등록 레시피·요약·챗봇·의미 검색이 함께 쓰는 LLM 설정과 하루 한도 나눠 쓰기.

## 화면 공통

- 주소는 http://127.0.0.1:8080 입니다. 상단에서 사용자(admin · registrar · viewer)를 고르면 그 사용자로 동작합니다 —
  PoC 인증이라 고른 사용자가 `X-User` 헤더로 나갑니다. SSO를 붙일 때는 백엔드 `CurrentUserResolver`만 바꿉니다.
- 권한은 세 단계입니다. **VIEWER**는 조회와 감시·구독 만들기(사이트 등록·감시는 관리자 승인 뒤 진행),
  **REGISTRAR**(등록 담당자)는 바로 등록·일괄 등록·거래처 등록·과거 기사 채우기(API), **ADMIN**은 관리자 메뉴 전체입니다.
  관리자 메뉴는 `GET /api/v1/me`로 권한을 확인해 ADMIN에게만 보입니다.
- 어느 화면에서나 오른쪽 아래 **챗봇 버튼**으로 대화창을 열 수 있습니다 ([08-chatbot.md](08-chatbot.md)).
- 화면은 빌드 도구·CDN 없이 `backend/src/main/resources/static/`의 HTML·CSS·JS만으로 동작하고, 주소는 해시 라우팅(`#/documents` 등)입니다.

## 탭이 아닌 내용은 여기로

| 무엇 | 문서 |
| --- | --- |
| 사이트를 등록하면 기사를 어떻게 찾아 가져오나 (사전 점검·자동 탐색·URL 서명·품질 검사·주기 수집·과거 기사·색인·감시 대조) | [../how-collection-works.md](../how-collection-works.md) |
| LLM 제공자 연결, Gemini 무료 등급 실측, 하루 한도 장부 | [llm.md](llm.md) |
| 네이버 뉴스 검색 API(NAVER API HUB)·약관·법무 확인 항목 | [../naver-news-api.md](../naver-news-api.md) |
| 설계 결정과 이유 | [../decisions.md](../decisions.md) |
| 현업 요청서 대응표·안티봇 범위 합의안·공급사 리스크 소스 | [../requirements-mapping.md](../requirements-mapping.md) |
| 수집 중인 사이트의 과거 기사 가능 범위 조사 | [../archive-sitemap-survey.md](../archive-sitemap-survey.md) |
| 단계별로 한 일(M1~M5)과 남은 일 | [../progress.md](../progress.md) |

## API 공통

모든 API는 `/api/v1` 아래에 있고 `X-User` 헤더의 login_id(`admin`, `registrar`, `viewer` 기본 계정)로 사용자를 정합니다.
각 문서의 예시는 로컬 백엔드(`http://127.0.0.1:8080`) 기준입니다.

```bash
curl http://127.0.0.1:8080/api/v1/me -H 'X-User: viewer'   # 내 사용자·권한
```
