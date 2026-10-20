"""네이버 뉴스 검색 API 커넥터 (키워드 감시의 네이버 소스 — 저장·분석 모드 전용).

- 네이버 뉴스 웹(news.naver.com)은 robots.txt가 전체 차단이라 크롤링하지 않는다. 검색 API로 키워드별 보도를 받는다.
- 주소는 Settings.naver_api로 고른다 (기본 hub):
    hub        GET https://naverapihub.apigw.ntruss.com/search/v1/news?query=..&display=100&start=1&sort=date&format=json
               헤더 X-NCP-APIGW-API-KEY-ID(Client ID) / X-NCP-APIGW-API-KEY(Client Secret). NAVER API HUB(네이버클라우드).
               HTTPS만 된다(HTTP는 403). 검색 묶음 하루 25,000회·월 775,000회, 키당 50 RPS (2026-10 기준, 한시 무료).
    developers GET https://openapi.naver.com/v1/search/news.json — 예전 개발자센터. 헤더 X-Naver-Client-Id / X-Naver-Client-Secret.
               2026-07-31 신규 발급 종료, 2027-06-30 지원 종료. 기존 키를 가진 경우에만 쓴다.
  Settings.naver_search_url이 있으면 주소만 그것으로 바꾼다 (헤더는 naver_api를 따른다).
- 개정 약관(2026-09-07 시행)은 검색 결과의 저장·캐싱·AI 입력을 금지한다. 그래서 기본(Settings.naver_mode = display)에서는
  워커가 이 커넥터로 수집하지 않고(jobs.naver_enabled), 감시 화면에서 백엔드가 실시간으로 불러와 보여 주기만 한다
  (backend io.sensinghub.naver). 이 커넥터의 fetch는 법무 확인 뒤 naver_mode = store로 켰을 때만 쓴다.
- 자격 증명(api_credential.secret)은 "client_id:client_secret" 한 문자열이다. 여기서 ':'로 나눈다.
  두 값 모두 로그·오류 메시지·증거 어디에도 남기지 않는다 (_mask).
- 응답 items[]: title(HTML 태그 <b>·엔티티 포함), originallink(언론사 원문), link(네이버 링크, 없으면 원문), description, pubDate(RFC 1123).
  ApiItem은 url=originallink(비면 link), title=태그 제거·엔티티 복원, published_at=UTC, body=None (언론 기사: 제목·링크만),
  keyword=걸린 검색어. exclude 목록의 단어가 제목에 있으면 버린다 (watchmatch와 같은 영문 단어 경계 규칙).
- 오류 (describe_error): 401/403 → API_KEY_INVALID, 429(하루 허용량 초과) → API_ERROR(재시도), 400 SE01~SE04·SE06 / 404 SE05 /
  300(API 없음) → API_ERROR(재시도 안 함), 5xx(SE99) → API_ERROR(재시도). 오류 본문은 두 형식이 있다:
  검색 API {"errorCode": "SE02", "errorMessage": "..."}, API Gateway {"error": {"errorCode": "200", "message": "...", "details": "..."}}.
"""

from __future__ import annotations

import html
import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode

from ..codes import Code, CrawlFailure, MESSAGES
from ..fetcher import EVIDENCE_CHARS, PoliteFetcher
from ..watchmatch import contains, normalize
from .base import ApiItem, mask_secret

log = logging.getLogger(__name__)

HUB_URL = "https://naverapihub.apigw.ntruss.com/search/v1/news"
DEVELOPERS_URL = "https://openapi.naver.com/v1/search/news.json"
PAGE_SIZE = 100      # display 최대값
MAX_PAGES = 3        # 검색어당 최대 페이지 (300건). 주기가 60분이라 그 사이 보도가 이보다 많으면 since로 어차피 끊긴다
MAX_START = 1000     # start 최대값 (API 제한)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class Endpoint:
    api: str
    url: str
    id_header: str
    secret_header: str
    extra_params: tuple[tuple[str, str], ...] = ()


ENDPOINTS: dict[str, Endpoint] = {
    "hub": Endpoint("hub", HUB_URL, "X-NCP-APIGW-API-KEY-ID", "X-NCP-APIGW-API-KEY", (("format", "json"),)),
    "developers": Endpoint("developers", DEVELOPERS_URL, "X-Naver-Client-Id", "X-Naver-Client-Secret"),
}

# 검색 API 오류 코드 (NAVER API HUB 뉴스 검색 문서, 2026-10-19 확인)
SE_MESSAGES: dict[str, str] = {
    "SE01": "검색 요청 형식이 잘못됐습니다 (주소의 https·필수 변수 query 확인)",
    "SE02": "display 값이 허용 범위(1~100)를 벗어났습니다",
    "SE03": "start 값이 허용 범위(1~1000)를 벗어났습니다",
    "SE04": "sort 값이 잘못됐습니다 (sim 또는 date)",
    "SE05": "존재하지 않는 검색 API 주소입니다 (SENSING_NAVER_API·SENSING_NAVER_SEARCH_URL 확인)",
    "SE06": "검색어 인코딩이 잘못됐습니다 (UTF-8)",
    "SE99": "네이버 검색 서버 내부 오류입니다",
}


def endpoint_for(settings) -> Endpoint:
    """Settings.naver_api·naver_search_url에 맞는 주소와 헤더 이름. 모르는 값이면 hub."""
    api = str(getattr(settings, "naver_api", "hub") or "hub").strip().lower()
    endpoint = ENDPOINTS.get(api)
    if endpoint is None:
        log.warning("SENSING_NAVER_API=%r is not hub|developers; using hub", api)
        endpoint = ENDPOINTS["hub"]
    override = str(getattr(settings, "naver_search_url", "") or "").strip()
    if override:
        endpoint = Endpoint(endpoint.api, override, endpoint.id_header, endpoint.secret_header, endpoint.extra_params)
    return endpoint


def split_secret(secret: str) -> tuple[str, str]:
    """'client_id:client_secret' → (client_id, client_secret). 형식이 아니면 API_KEY_INVALID."""
    text = (secret or "").strip()
    client_id, sep, client_secret = text.partition(":")
    if not sep or not client_id.strip() or not client_secret.strip():
        raise CrawlFailure(Code.API_KEY_INVALID,
                           f"{MESSAGES[Code.API_KEY_INVALID]} (네이버 키는 Client ID:Client Secret 형식이어야 합니다.)",
                           stage="PROBE")
    return client_id.strip(), client_secret.strip()


def error_of(data: dict) -> tuple[str, str]:
    """오류 본문 → (코드, 메시지). 검색 API 형식과 API Gateway 형식을 모두 읽는다. 없으면 ("", "")."""
    if not isinstance(data, dict):
        return "", ""
    if data.get("errorCode") or data.get("errorMessage"):
        return str(data.get("errorCode") or "").strip(), str(data.get("errorMessage") or "").strip()
    gateway = data.get("error")
    if isinstance(gateway, dict):
        message = str(gateway.get("message") or "").strip()
        details = str(gateway.get("details") or "").strip()
        return str(gateway.get("errorCode") or "").strip(), f"{message}: {details}" if message and details else message or details
    return "", ""


def describe_error(status: int, code: str, message: str) -> tuple[Code, str, bool] | None:
    """HTTP 상태·오류 코드 → (분류, 사람이 읽을 문장, 재시도 여부). 정상(200, 코드 없음)이면 None."""
    detail = f"{code}: {message}" if code and message else code or message or f"HTTP {status}"
    if status == 401:
        return (Code.API_KEY_INVALID,
                f"{MESSAGES[Code.API_KEY_INVALID]} (네이버 검색 인증 실패 — Client ID·Client Secret이 맞는지, "
                f"API HUB Application에 '검색'이 선택돼 있는지 확인하세요. {detail})", False)
    if status == 403:
        return (Code.API_KEY_INVALID,
                f"{MESSAGES[Code.API_KEY_INVALID]} (네이버 검색 호출이 허용되지 않았습니다 — Application의 검색 API 권한과 "
                f"https 주소를 확인하세요. {detail})", False)
    if status == 429:
        return (Code.API_ERROR, f"네이버 검색 API 호출 한도(하루 허용량)를 넘었습니다 ({detail}). 다음 주기에 다시 시도합니다.",
                True)
    friendly = SE_MESSAGES.get(code)
    if status in (300, 404):
        return (Code.API_ERROR, f"네이버 검색 API 주소가 잘못됐습니다 — {friendly or 'API 없음'} ({detail})", False)
    if status >= 500:
        return (Code.API_ERROR, f"네이버 검색 API 서버 오류 — {friendly or '잠시 후 다시 시도합니다'} ({detail})", True)
    if status != 200:
        return (Code.API_ERROR, f"네이버 검색 API 요청 오류 — {friendly or '잘못된 요청'} ({detail})", False)
    if code:
        return (Code.API_ERROR, f"네이버 검색 API 오류 — {friendly or message or code} ({detail})", True)
    return None


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
        self.endpoint = endpoint_for(getattr(fetcher, "s", None))
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
        exclude = _clean_terms(config.get("exclude"))
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
                    if exclude and item.title and any(contains(normalize(item.title), e) for e in exclude):
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
        log.info("naver news search (%s): keywords=%s calls=%s items=%s (connector total %s, daily limit %s)",
                 self.endpoint.api, len(keywords), used, len(items), self.calls,
                 getattr(self.f.s, "naver_daily_limit", None))
        return items

    # ------------------------------------------------------------------
    def _call(self, client_id: str, client_secret: str, *, query: str, display: int, start: int) -> dict:
        params = [("query", query), ("display", display), ("start", start), ("sort", "date"), *self.endpoint.extra_params]
        url = self.endpoint.url + "?" + urlencode(params)
        headers = {self.endpoint.id_header: client_id, self.endpoint.secret_header: client_secret}
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
        error_code, error_message = error_of(data)
        problem = describe_error(page.status, error_code, mask(error_message) or "")
        if problem is not None:
            code, text, retryable = problem
            raise CrawlFailure(code, text, url=url, http_status=page.status, retryable=retryable, stage="LIST",
                               evidence=None if code == Code.API_KEY_INVALID else evidence)
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
