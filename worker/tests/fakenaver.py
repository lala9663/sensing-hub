"""테스트용 가짜 네이버 뉴스 검색 API (httpx.MockTransport 핸들러). 실제 openapi.naver.com에는 요청하지 않는다."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx

GOOD_ID = "naverid0123abcd"
GOOD_SECRET = "naversecretXYZ789"
GOOD_KEY = f"{GOOD_ID}:{GOOD_SECRET}"
KST = timezone(timedelta(hours=9))


def article(keyword: str, i: int, *, pub: datetime | None = None, title: str | None = None,
            originallink: str | None = None) -> dict:
    """검색 결과 한 건. 제목에는 실제 API처럼 <b>검색어</b>와 HTML 엔티티가 들어 있다."""
    pub = pub or (datetime.now(KST) - timedelta(hours=i))
    slug = keyword.replace(" ", "-")
    return {
        "title": title if title is not None else f"<b>{keyword}</b>, &quot;신제품&quot; 공개 {i}",
        "originallink": originallink if originallink is not None else f"https://press.example.com/{slug}/{i}",
        "link": f"https://n.news.naver.com/mnews/article/001/{slug}{i}",
        "description": f"<b>{keyword}</b> 관련 기사 {i} 요약 &amp; 설명",
        "pubDate": format_datetime(pub),
    }


def articles(keyword: str, n: int = 3) -> list[dict]:
    return [article(keyword, i) for i in range(1, n + 1)]


def naver_handler(by_keyword: dict[str, list[dict]] | None = None, *, http_status: int | None = None,
                  error_code: str | None = None, per_page: int = 100):
    """헤더의 client_id/secret이 GOOD_*이면 정상 목록, 아니면 401(errorCode 024). 모르는 검색어는 0건.
    http_status/error_code로 장애(429 한도 초과, 500 등)를 흉내 낸다.
    handler.calls에 (query, display, start)를, handler.headers에 받은 인증 헤더를 모은다."""
    rows_by_keyword = by_keyword if by_keyword is not None else {"테스트": articles("테스트", 1)}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != "openapi.naver.com" or request.url.path != "/v1/search/news.json":
            return httpx.Response(404, text="not found")
        params = dict(request.url.params)
        handler.calls.append((params.get("query"), int(params.get("display", "10")), int(params.get("start", "1"))))
        client_id = request.headers.get("X-Naver-Client-Id", "")
        client_secret = request.headers.get("X-Naver-Client-Secret", "")
        handler.headers.append((client_id, client_secret))
        if "query" in str(request.url) and (client_id in str(request.url) or client_secret in str(request.url)):
            return httpx.Response(400, json={"errorMessage": "key must not be in query", "errorCode": "SE01"})
        if http_status is not None:
            return httpx.Response(http_status, json={"errorMessage": f"상태 {http_status}", "errorCode": error_code or "SE99"})
        if client_id != GOOD_ID or client_secret != GOOD_SECRET:
            # 실제 API는 받은 값을 되돌려 주지 않지만, 커넥터가 응답 본문도 가리는지 보려고 일부러 넣는다
            return httpx.Response(401, json={"errorMessage": f"Authentication failed. (id={client_id}, secret={client_secret})",
                                             "errorCode": "024"})
        if error_code is not None:
            return httpx.Response(200, json={"errorMessage": f"오류 {error_code}", "errorCode": error_code})
        rows = rows_by_keyword.get(params.get("query", ""), [])
        display = min(int(params.get("display", "10")), per_page)
        start = int(params.get("start", "1"))
        page = rows[start - 1: start - 1 + display]
        body = {"lastBuildDate": format_datetime(datetime.now(KST)), "total": len(rows), "start": start,
                "display": len(page), "items": page}
        return httpx.Response(200, text=json.dumps(body, ensure_ascii=False),
                              headers={"content-type": "application/json;charset=utf-8"})

    handler.calls = []
    handler.headers = []
    return handler
