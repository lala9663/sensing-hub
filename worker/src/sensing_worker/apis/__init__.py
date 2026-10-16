"""공개 API로 받는 소스 (M2).

크롤링 대신 공개 API가 있는 제공처(DART 공시, 네이버 뉴스 검색 등)는 카탈로그(catalog.py)로 판정하고, 제공처별
커넥터(dart.py, naver_news.py)가 키 검증·수집을 한다. 키워드 감시(source.kind WATCH)는 keyword_based 제공자의 커넥터가
설정의 keywords로 검색해 받고, 걸린 검색어를 ApiItem.keyword에 실어 document_watch에 남긴다. 키는 백엔드가 pgcrypto로 암호화해 api_credential에 두고, 워커는 SENSING_SECRET_KEY로 풀어 쓴다.
키는 로그·오류 메시지·증거 파일 어디에도 남기지 않는다 (base.mask_secret).

점검 소스(kind CHECK, M3)는 제공자 하나에 점검 종류(config["check"])가 여럿이다. 공공데이터포털(data_go_kr) 키 하나로 국세청
사업자 상태(nts_status)와 조달청 부정당업자(g2b_debarment)를, DART 키로 고유번호 매핑(dart_corp_map)과 회사별 공시(dart_disclosure)를
확인하므로 점검 소스의 커넥터는 make_check_connector(config)로 고른다.
"""

from __future__ import annotations

import logging

from ..fetcher import PoliteFetcher
from .base import ApiConnector, ApiItem, mask_secret
from .catalog import Provider, lookup
from .dart import CorpEntry, DartConnector
from .g2b_debarment import Debarment, G2bDebarmentConnector
from .naver_news import NaverNewsConnector
from .nts_status import BizStatus, NtsStatusConnector

# httpx는 요청마다 전체 URL을 INFO로 남긴다. API 키가 쿼리에 실리므로 끈다
logging.getLogger("httpx").setLevel(logging.WARNING)

CONNECTORS: dict[str, type] = {DartConnector.provider: DartConnector, NaverNewsConnector.provider: NaverNewsConnector,
                               NtsStatusConnector.provider: NtsStatusConnector}

# 점검 종류 → 커넥터. 없는 종류는 제공자 기본 커넥터(data_go_kr → 국세청, dart → DART)
CHECK_CONNECTORS: dict[str, type] = {"nts_status": NtsStatusConnector, "g2b_debarment": G2bDebarmentConnector,
                                     "dart_corp_map": DartConnector, "dart_disclosure": DartConnector}


def make_connector(provider_id: str, fetcher: PoliteFetcher) -> ApiConnector:
    try:
        return CONNECTORS[provider_id](fetcher)
    except KeyError:
        raise ValueError(f"커넥터가 없는 API 제공자: {provider_id}") from None


def make_check_connector(config: dict, fetcher: PoliteFetcher) -> ApiConnector:
    """점검 소스 설정({"provider", "check"})에 맞는 커넥터."""
    cls = CHECK_CONNECTORS.get(str(config.get("check") or ""))
    if cls is not None:
        return cls(fetcher)
    return make_connector(str(config.get("provider") or ""), fetcher)


__all__ = ["ApiConnector", "ApiItem", "BizStatus", "CorpEntry", "DartConnector", "Debarment", "G2bDebarmentConnector",
           "NaverNewsConnector", "NtsStatusConnector", "Provider", "lookup", "make_check_connector", "make_connector",
           "mask_secret"]
