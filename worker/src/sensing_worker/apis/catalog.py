"""API 카탈로그: 이 도메인은 크롤링 대신 공개 API로 받는다.

등록 시 워커가 사전 점검 전에 여기서 찾아보고, 제공자가 있으면 크롤링을 시도하지 않는다.
키 발급은 사람이 한다 (자동 가입·약관 동의 대행은 하지 않는다 — docs/requirements-mapping.md).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Provider:
    id: str                     # api_credential.provider, site.risk_flags.api_provider
    name: str
    domains: tuple[str, ...]    # 이 도메인(또는 그 하위)으로 등록하면 API 소스로 본다
    signup_url: str             # 키 발급 안내 (관리자에게 보여 준다)
    needs_key: bool
    description: str
    key_required_message: str   # 등록 요청 결과 메시지 (사용자에게 그대로 보인다)
    # 사이트가 아니라 키워드 묶음(감시, source.kind WATCH)으로 등록하는 제공자. 도메인을 그냥 등록하면 KEYWORDS_REQUIRED
    keyword_based: bool = False


PROVIDERS: tuple[Provider, ...] = (
    Provider(
        id="dart",
        name="DART 전자공시",
        domains=("dart.fss.or.kr", "opendart.fss.or.kr"),
        signup_url="https://opendart.fss.or.kr/uat/uia/egovLoginUsr.do",
        needs_key=True,
        description="금융감독원 전자공시시스템. 공시 목록을 오픈API(list.json)로 받고 원문은 dart.fss.or.kr 링크로 둔다.",
        key_required_message="DART 공시는 크롤링 대신 공시 API로 받습니다. 관리자가 API 키를 등록하면 자동으로 진행됩니다. "
                             "발급: https://opendart.fss.or.kr/uat/uia/egovLoginUsr.do",
    ),
    Provider(
        id="naver_news",
        name="네이버 뉴스 검색",
        domains=("news.naver.com", "n.news.naver.com", "openapi.naver.com", "naverapihub.apigw.ntruss.com"),
        signup_url="https://console.ncloud.com",
        needs_key=True,
        description="네이버 뉴스는 robots.txt가 전체 차단이라 크롤링하지 않고 NAVER API HUB 뉴스 검색(search/v1/news)으로 "
                    "키워드별 보도를 본다. 개정 약관(저장·AI 입력 금지)에 맞춰 기본은 표시 전용(감시 화면 실시간 조회)이고, "
                    "저장·분석(SENSING_NAVER_MODE=store)일 때만 제목·링크·발행일을 저장한다. 키는 Client ID:Client Secret 한 문자열.",
        key_required_message="네이버 뉴스는 크롤링이 금지돼 검색 API로 봅니다. 관리자가 NAVER API HUB 키(Client ID:Client Secret)를 "
                             "등록하면 진행됩니다. 발급: 네이버클라우드 콘솔 Services → Application Service → NAVER API HUB → "
                             "Application 등록(검색 선택)",
        keyword_based=True,
    ),
    Provider(
        id="data_go_kr",
        name="공공데이터포털",
        domains=("api.odcloud.kr", "apis.data.go.kr"),
        signup_url="https://www.data.go.kr",
        needs_key=True,
        description="공공데이터포털(data.go.kr) 오픈API. 서비스키 하나를 여러 API가 공유한다. 첫 용도는 국세청 사업자등록 상태조회"
                    "(회사 마스터의 사업자번호로 휴업·폐업·과세유형 변경을 매일 확인, kind CHECK 점검 소스), 둘째는 조달청 나라장터 "
                    "부정당업자(입찰참가자격 제한) 조회(apis.data.go.kr, 사용자정보 서비스). "
                    "국세청 사업자등록 상태조회 활용신청 필요 (조달청 점검에는 나라장터 사용자정보 서비스 활용신청도).",
        key_required_message="공공데이터포털 서비스키가 필요합니다. 관리자가 data.go.kr에서 국세청 사업자등록 상태조회 활용신청 후 "
                             "서비스키를 등록하면 자동으로 진행됩니다. 발급: https://www.data.go.kr",
    ),
)


def lookup(domain: str) -> Provider | None:
    """도메인(소문자, www. 유무 무관)에 맞는 제공자. 없으면 None."""
    host = (domain or "").strip().lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return None
    for p in PROVIDERS:
        if any(host == d or host.endswith("." + d) for d in p.domains):
            return p
    return None


def get(provider_id: str) -> Provider | None:
    return next((p for p in PROVIDERS if p.id == provider_id), None)
