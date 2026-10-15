"""테스트용 가짜 사이트. 실제 외부 사이트에 요청하지 않고 수집 흐름을 검증한다."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

BODY = ("본문 문단입니다. " * 40).strip()


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _kdate(days_ago: float) -> str:
    d = datetime.now(timezone(timedelta(hours=9))) - timedelta(days=days_ago)
    return d.strftime("%Y.%m.%d %H:%M")


class Site:
    """경로 → (상태, content-type, 본문) 사전으로 응답한다."""

    def __init__(self, routes: dict[str, tuple[int, str, str]]):
        self.routes = routes
        self.hits: list[str] = []
        site = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                parts = urlsplit(self.path)
                key = parts.path + (f"?{parts.query}" if parts.query else "")
                site.hits.append(key)
                status, ctype, body = site.routes.get(key, site.routes.get(parts.path, (404, "text/html", "not found")))
                data = body.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        self.server.shutdown()


# ---------------------------------------------------------------------- 사이트 유형

def news_site_routes(article_ids=range(101, 107)) -> dict:
    """ndsoft 형태 언론사: RSS 없음, 섹션 목록과 articleView.html?idxno= 기사."""
    items = "".join(f'<li><a href="/news/articleView.html?idxno={i}">기사 제목 {i}번 소식</a></li>' for i in article_ids)
    home = f"""<html><head><title>데일리 테스트 뉴스</title>
    <meta property="og:type" content="website"></head><body>
    <nav><a href="/news/articleList.html?sc_section_code=S1N1&view_type=sm">경제</a>
    <a href="/news/articleList.html?sc_section_code=S1N2&view_type=sm">산업</a>
    <a href="/news/articleList.html?sc_section_code=S1N9&view_type=sm">포토</a>
    <a href="/member/login.html">로그인</a></nav>
    <ul>{items}</ul><footer>ndsoft</footer></body></html>"""
    routes = {
        "/": (200, "text/html; charset=utf-8", home),
        "/robots.txt": (200, "text/plain", "User-agent: *\nDisallow: /member/\n"),
        "/news/articleList.html": (200, "text/html; charset=utf-8", f"<html><body><ul>{items}</ul></body></html>"),
    }
    for i in article_ids:
        routes[f"/news/articleView.html?idxno={i}"] = (200, "text/html; charset=utf-8", f"""<html><head>
        <title>기사 제목 {i}번 소식 &lt; 산업 &lt; 기사본문 - 데일리 테스트 뉴스</title>
        <meta property="og:type" content="article">
        <meta property="og:title" content="기사 제목 {i}번 소식">
        <meta property="article:published_time" content="{_iso(1)}">
        <meta property="article:section" content="산업">
        </head><body><article id="article-view-content-div"><p>{BODY}</p><p>{BODY}</p></article></body></html>""")
    return routes


def gov_site_routes() -> dict:
    """공공기관 게시판: RSS 제공, 등록일 텍스트, HWP 첨부."""
    items = "".join(f"<item><title>보도자료 {i}</title><link>{{base}}/board/view.do?nttId={i}&amp;utm_source=rss</link></item>"
                    for i in range(1, 6))
    routes = {
        "/": (200, "text/html; charset=utf-8", """<html><head><title>테스트시청</title>
            <link rel="alternate" type="application/rss+xml" href="/rss.xml"></head>
            <body><a href="/board/list.do?bbsId=1">보도자료</a></body></html>"""),
        "/robots.txt": (404, "text/plain", ""),
        "/rss.xml": (200, "application/rss+xml", f"<?xml version='1.0'?><rss><channel>{items}</channel></rss>"),
    }
    for i in range(1, 6):
        routes[f"/board/view.do?nttId={i}"] = (200, "text/html; charset=utf-8", f"""<html><head><title>보도자료 {i}</title></head>
        <body><div class="board_view"><h3>시정 보도자료 {i}번 안내</h3>
        <span>등록일 {_kdate(2)}</span>
        <p>{BODY}</p>
        <a href="/cmm/fileDown.do?atchFileId=F{i}">보도자료_{i}.hwp</a></div></body></html>""")
    return routes


def blocked_site_routes() -> dict:
    page = "<html><head><title>Just a moment...</title></head><body>cf-browser-verification</body></html>"
    return {"/": (403, "text/html", page), "/robots.txt": (200, "text/plain", "User-agent: *\nAllow: /\n")}


def robots_blocked_routes() -> dict:
    return {"/": (200, "text/html", "<html><body>hi</body></html>"),
            "/robots.txt": (200, "text/plain", "User-agent: *\nDisallow: /\n")}
