"""공개 API로 받는 소스 (M2).

크롤링 대신 공개 API가 있는 제공처(DART 공시, 네이버 뉴스 검색 등)는 카탈로그(catalog.py)로 판정하고, 제공처별
커넥터(dart.py, naver_news.py)가 키 검증·수집을 한다. 키워드 감시(source.kind WATCH)는 keyword_based 제공자의 커넥터가
설정의 keywords로 검색해 받고, 걸린 검색어를 ApiItem.keyword에 실어 document_watch에 남긴다. 키는 백엔드가 pgcrypto로 암호화해 api_credential에 두고, 워커는 SENSING_SECRET_KEY로 풀어 쓴다.
키는 로그·오류 메시지·증거 파일 어디에도 남기지 않는다 (base.mask_secret).
"""

from __future__ import annotations

import logging

from ..fetcher import PoliteFetcher
from .base import ApiConnector, ApiItem, mask_secret
from .catalog import Provider, lookup
from .dart import DartConnector
from .naver_news import NaverNewsConnector
from .nts_status import BizStatus, NtsStatusConnector

# httpx는 요청마다 전체 URL을 INFO로 남긴다. API 키가 쿼리에 실리므로 끈다
logging.getLogger("httpx").setLevel(logging.WARNING)

CONNECTORS: dict[str, type] = {DartConnector.provider: DartConnector, NaverNewsConnector.provider: NaverNewsConnector,
                               NtsStatusConnector.provider: NtsStatusConnector}


def make_connector(provider_id: str, fetcher: PoliteFetcher) -> ApiConnector:
    try:
        return CONNECTORS[provider_id](fetcher)
    except KeyError:
        raise ValueError(f"커넥터가 없는 API 제공자: {provider_id}") from None


__all__ = ["ApiConnector", "ApiItem", "BizStatus", "DartConnector", "NaverNewsConnector", "NtsStatusConnector", "Provider",
           "lookup", "make_connector", "mask_secret"]
