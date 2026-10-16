"""DART 오픈API 커넥터: 공시 목록(list.json).

- 인증: crtfc_key 쿼리. 키는 URL에 실리므로 로그·오류·증거 어디에도 남기지 않는다 (mask_secret).
- PoliteFetcher.get_api로 호출해 도메인 간격·내부망 차단·연결 재시도는 지키되 robots 검사는 하지 않는다
  (API 주소는 robots 대상이 아니다).
- 응답 status: "000" 정상, "013" 조회 결과 없음(정상·0건), "010/011/012/901" 키 문제(API_KEY_INVALID),
  그 밖("020" 요청 한도 초과, "100" 잘못된 값, "800" 점검 등)은 API_ERROR로 분류해 다음 주기에 다시 시도한다.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode

from ..codes import Code, CrawlFailure, MESSAGES
from ..fetcher import EVIDENCE_CHARS, PoliteFetcher
from .base import ApiItem, mask_secret

log = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
LIST_URL = "https://opendart.fss.or.kr/api/list.json"
VIEWER_URL = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"
PAGE_COUNT = 100
# 키가 잘못됐거나 쓸 수 없다: 등록되지 않은 키(010), 사용할 수 없는 키(011), 접근할 수 없는 IP(012), 계정 만료(901)
KEY_ERROR_STATUSES = {"010", "011", "012", "901"}
NO_DATA_STATUS = "013"


def _ymd(d: date) -> str:
    return d.strftime("%Y%m%d")


class DartConnector:
    provider = "dart"

    def __init__(self, fetcher: PoliteFetcher):
        self.f = fetcher

    def default_config(self) -> dict:
        # days: 매 실행에서 오늘부터 며칠 전까지 받을지. corp_codes: 비면 전체 공시, 있으면 그 회사(corp_code)만
        return {"method": "API", "provider": self.provider, "days": 1, "corp_codes": []}

    def verify(self, secret: str) -> None:
        today = datetime.now(KST).date()
        self._call(secret, {"bgn_de": _ymd(today), "end_de": _ymd(today), "page_count": "1"})

    def fetch(self, secret: str, config: dict, since: date, limit: int) -> list[ApiItem]:
        end = datetime.now(KST).date()
        base = {"bgn_de": _ymd(min(since, end)), "end_de": _ymd(end), "page_count": str(PAGE_COUNT)}
        corp_codes = [c for c in (config.get("corp_codes") or []) if c] or [None]
        items: list[ApiItem] = []
        seen: set[str] = set()
        for corp in corp_codes:
            page_no = 1
            while len(items) < limit:
                params = {**base, "page_no": str(page_no)}
                if corp:
                    params["corp_code"] = corp
                data = self._call(secret, params)
                for row in data.get("list") or []:
                    item = _to_item(row)
                    if item is None or item.url in seen:
                        continue
                    seen.add(item.url)
                    items.append(item)
                    if len(items) >= limit:
                        break
                if page_no >= _int(data.get("total_page"), 1):
                    break
                page_no += 1
        return items

    # ------------------------------------------------------------------
    def _call(self, secret: str, params: dict) -> dict:
        url = LIST_URL + "?" + urlencode({"crtfc_key": secret, **params})
        safe_url = mask_secret(url, secret)
        try:
            page = self.f.get_api(url)
        except CrawlFailure as e:
            raise CrawlFailure(e.code, mask_secret(str(e), secret), url=safe_url, http_status=e.http_status,
                               retryable=e.retryable, stage="LIST",
                               evidence=mask_secret(e.evidence, secret)) from None
        evidence = mask_secret(page.text[:EVIDENCE_CHARS], secret) or None
        if page.status != 200:
            raise CrawlFailure(Code.API_ERROR, f"DART API가 HTTP {page.status}로 응답했습니다.", url=safe_url,
                               http_status=page.status, retryable=page.status >= 500, stage="LIST", evidence=evidence)
        try:
            data = json.loads(page.text)
        except ValueError:
            raise CrawlFailure(Code.API_ERROR, "DART API 응답이 JSON이 아닙니다.", url=safe_url, http_status=200,
                               retryable=True, stage="LIST", evidence=evidence) from None
        status = str(data.get("status", "")).strip()
        if status == "000":
            return data
        if status == NO_DATA_STATUS:
            return {**data, "list": [], "total_page": 0}
        message = mask_secret(str(data.get("message", "")).strip(), secret)
        if status in KEY_ERROR_STATUSES:
            raise CrawlFailure(Code.API_KEY_INVALID, f"{MESSAGES[Code.API_KEY_INVALID]} (DART {status}: {message})",
                               url=safe_url, http_status=200, stage="LIST")
        raise CrawlFailure(Code.API_ERROR, f"DART API 오류 {status}: {message}", url=safe_url, http_status=200,
                           retryable=True, stage="LIST", evidence=evidence)


def _to_item(row: dict) -> ApiItem | None:
    rcept_no = str(row.get("rcept_no") or "").strip()
    if not rcept_no:
        return None
    return ApiItem(
        url=VIEWER_URL.format(rcept_no=rcept_no),
        title=(row.get("report_nm") or "").strip() or None,
        published_at=_kst_date(row.get("rcept_dt")),
        author=(row.get("corp_name") or "").strip() or None,
        section=(row.get("flr_nm") or "").strip() or None,
        body=None,
    )


def _kst_date(text: str | None) -> datetime | None:
    """rcept_dt "20261015" → 한국 시간 그날 자정."""
    text = (text or "").strip()
    if len(text) != 8 or not text.isdigit():
        return None
    try:
        return datetime(int(text[:4]), int(text[4:6]), int(text[6:]), tzinfo=KST)
    except ValueError:
        return None


def _int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
