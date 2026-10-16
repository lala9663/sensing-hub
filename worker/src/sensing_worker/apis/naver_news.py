"""네이버 뉴스 검색 API 커넥터 (키워드 감시의 첫 소스).

- 네이버 뉴스 웹(news.naver.com)은 robots.txt가 전체 차단이라 크롤링하지 않는다. 검색 API로 키워드별 보도를 받는다.
    GET https://openapi.naver.com/v1/search/news.json?query=<검색어>&display=100&start=1&sort=date
    헤더 X-Naver-Client-Id / X-Naver-Client-Secret. 일 호출 한도 25,000 (Settings.naver_daily_limit로 우리 쪽 상한).
- 자격 증명(api_credential.secret)은 "client_id:client_secret" 한 문자열이다. 여기서 ':'로 나눈다.
  두 값 모두 로그·오류 메시지·증거 어디에도 남기지 않는다 (_mask).
- 응답 items[]: title(HTML 태그 <b>·엔티티 포함), originallink(언론사 원문), link(네이버 링크), description, pubDate(RFC 1123).
  ApiItem은 url=originallink(비면 link), title=태그 제거·엔티티 복원, published_at=UTC, body=None (언론 기사: 제목·링크만),
  keyword=걸린 검색어. exclude 목록의 단어가 제목에 있으면 버린다.
- 오류: HTTP 401/403(본문 errorCode 024 등) → API_KEY_INVALID, 429(한도 초과) → API_ERROR(재시도), 그 밖 → API_ERROR.
"""

from __future__ import annotations

import html
import json
import logging
import re
from datetime import date, datetime, time, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode

from ..codes import Code, CrawlFailure, MESSAGES
from ..fetcher import EVIDENCE_CHARS, PoliteFetcher
from .base import ApiItem, mask_secret

log = logging.getLogger(__name__)

SEARCH_URL = "https://openapi.naver.com/v1/search/news.json"
PAGE_SIZE = 100      # display 최대값
MAX_PAGES = 3        # 검색어당 최대 페이지 (300건). 주기가 60분이라 그 사이 보도가 이보다 많으면 since로 어차피 끊긴다
MAX_START = 1000     # start 최대값 (API 제한)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def split_secret(secret: str) -> tuple[str, str]:
    """'client_id:client_secret' → (client_id, client_secret). 형식이 아니면 API_KEY_INVALID."""
    text = (secret or "").strip()
    client_id, sep, client_secret = text.partition(":")
    if not sep or not client_id.strip() or not client_secret.strip():
        raise CrawlFailure(Code.API_KEY_INVALID,
                           f"{MESSAGES[Code.API_KEY_INVALID]} (네이버 키는 client_id:client_secret 형식이어야 합니다.)",
                           stage="PROBE")
    return client_id.strip(), client_secret.strip()


def clean_title(raw: str | None) -> str | None:
    """<b>태그</b> 제거, &quot; 같은 엔티티 복원, 공백 정리."""
    if not raw:
        return None
    text = html.unescape(_TAG_RE.sub("", raw))
    text = _WS_RE.sub(" ", text).strip()
    return text or None


def parse_pub_date(text: str | None) -> datetime | None:
    """RFC 1123 ('Thu, 16 Oct 2026 09:30:00 +0900') → UTC."""
    if not text:
        return None
    try:
        dt = parsedate_to_datetime(text.strip())
    except (TypeError, ValueError, IndexError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class NaverNewsConnector:
    provider = "naver_news"

    def __init__(self, fetcher: PoliteFetcher):
        self.f = fetcher
        self.calls = 0   # 이 커넥터 객체가 보낸 API 호출 수 (일 한도 감시용 로그)

    def default_config(self) -> dict:
        # 키워드는 사용자가 감시를 만들 때 넣는다. 도메인만 등록된 경우 비어 있고, 그러면 수집하지 않는다(KEYWORDS_REQUIRED)
        return {"method": "API", "provider": self.provider, "keywords": [], "exclude": []}

    def verify(self, secret: str) -> None:
        client_id, client_secret = split_secret(secret)
        self._call(client_id, client_secret, query="테스트", display=1, start=1)

    def fetch(self, secret: str, config: dict, since: date, limit: int) -> list[ApiItem]:
        client_id, client_secret = split_secret(secret)
        keywords = _clean_terms(config.get("keywords"))
        if not keywords:
            raise CrawlFailure(Code.KEYWORDS_REQUIRED, "감시에 키워드가 없습니다. 키워드를 넣으면 다음 주기에 수집합니다.",
                               stage="LIST")
        exclude = [e.lower() for e in _clean_terms(config.get("exclude"))]
        cutoff = datetime.combine(since, time.min, tzinfo=timezone.utc)

        items: list[ApiItem] = []
        seen: set[str] = set()
        calls_before = self.calls
        for keyword in keywords:
            if len(items) >= limit:
                break
            start = 1
            for _ in range(MAX_PAGES):
                data = self._call(client_id, client_secret, query=keyword, display=PAGE_SIZE, start=start)
                rows = data.get("items") or []
                too_old = False
                for row in rows:
                    item = _to_item(row, keyword)
                    if item is None:
                        continue
                    if item.published_at is not None and item.published_at < cutoff:
                        too_old = True     # sort=date라 이 뒤는 모두 더 오래됐다
                        break
                    if item.url in seen:
                        continue
                    if exclude and item.title and any(e in item.title.lower() for e in exclude):
                        continue
                    seen.add(item.url)
                    items.append(item)
                    if len(items) >= limit:
                        break
                if too_old or len(items) >= limit or not rows:
                    break
                # 받은 만큼 start를 민다 (API의 total을 넘거나 start 상한을 넘으면 끝)
                start += len(rows)
                if start > min(_int(data.get("total"), MAX_START), MAX_START):
                    break
        used = self.calls - calls_before
        log.info("naver news search: keywords=%s calls=%s items=%s (connector total %s, daily limit %s)",
                 len(keywords), used, len(items), self.calls, getattr(self.f.s, "naver_daily_limit", None))
        return items

    # ------------------------------------------------------------------
    def _call(self, client_id: str, client_secret: str, *, query: str, display: int, start: int) -> dict:
        url = SEARCH_URL + "?" + urlencode({"query": query, "display": display, "start": start, "sort": "date"})
        headers = {"X-Naver-Client-Id": client_id, "X-Naver-Client-Secret": client_secret}
        mask = lambda text: _mask(text, client_id, client_secret)   # noqa: E731
        self.calls += 1
        try:
            page = self.f.get_api(url, headers=headers)
        except CrawlFailure as e:
            raise CrawlFailure(e.code, mask(str(e)), url=url, http_status=e.http_status, retryable=e.retryable,
                               stage="LIST", evidence=mask(e.evidence)) from None
        evidence = mask(page.text[:EVIDENCE_CHARS]) or None
        data: dict = {}
        try:
            parsed = json.loads(page.text) if page.text else {}
            if isinstance(parsed, dict):
                data = parsed
        except ValueError:
            pass
        error_code = str(data.get("errorCode") or "").strip()
        error_message = mask(str(data.get("errorMessage") or "").strip())
        detail = f"{error_code}: {error_message}" if error_code else f"HTTP {page.status}"

        if page.status in (401, 403):
            raise CrawlFailure(Code.API_KEY_INVALID, f"{MESSAGES[Code.API_KEY_INVALID]} (네이버 {detail})", url=url,
                               http_status=page.status, stage="LIST")
        if page.status == 429:
            raise CrawlFailure(Code.API_ERROR, f"네이버 검색 API 호출 한도를 넘었습니다 ({detail}). 다음 주기에 다시 시도합니다.",
                               url=url, http_status=429, retryable=True, stage="LIST", evidence=evidence)
        if page.status != 200:
            raise CrawlFailure(Code.API_ERROR, f"네이버 검색 API 오류 ({detail})", url=url, http_status=page.status,
                               retryable=page.status >= 500, stage="LIST", evidence=evidence)
        if error_code:
            raise CrawlFailure(Code.API_ERROR, f"네이버 검색 API 오류 ({detail})", url=url, http_status=200,
                               retryable=True, stage="LIST", evidence=evidence)
        if not data or "items" not in data:
            raise CrawlFailure(Code.API_ERROR, "네이버 검색 API 응답이 예상한 JSON이 아닙니다.", url=url, http_status=200,
                               retryable=True, stage="LIST", evidence=evidence)
        return data


def _to_item(row: dict, keyword: str) -> ApiItem | None:
    url = (row.get("originallink") or "").strip() or (row.get("link") or "").strip()
    if not url:
        return None
    return ApiItem(
        url=url,
        title=clean_title(row.get("title")),
        published_at=parse_pub_date(row.get("pubDate")),
        author=None,
        section=None,
        body=None,
        keyword=keyword,
    )


def _int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clean_terms(values) -> list[str]:
    out: list[str] = []
    for v in values or []:
        term = str(v or "").strip()
        if term and term not in out:
            out.append(term)
    return out


def _mask(text: str | None, client_id: str, client_secret: str) -> str | None:
    if not text:
        return text
    text = mask_secret(text, client_secret)
    return mask_secret(text, client_id) if client_id else text
