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
