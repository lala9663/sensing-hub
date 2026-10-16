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


def _sdate(days_ago: float) -> str:
    """공공기관 목록 행에 흔한 "2026. 10. 15" 형식"""
    d = datetime.now(timezone(timedelta(hours=9))) - timedelta(days=days_ago)
    return d.strftime("%Y. %m. %d")


def gov_cms_routes() -> dict:
    """공공기관 CMS(산업부형): 사이트맵에는 게시판·콘텐츠 페이지만 있고 글은 섹션 안에만 보인다.
    og:title은 사이트 이름이고, 글 제목은 .detail-tit, 등록일은 <li><em>등록일</em><span>날짜</span></li>에 있다."""
    boards = {"ATCL3f49a5a8c": "보도자료", "ATCL369bcdf07": "알림/소식", "ATCLe0854704d": "참고자료"}
    nav = "".join(f'<a href="/kor/article/{b}">{n}</a>' for b, n in boards.items())
    head = ('<head><title>테스트부</title><meta property="og:title" content="테스트부">'
            '<meta property="og:site_name" content="테스트부"></head>')
    home = f'<html>{head}<body><nav>{nav}<a href="/kor/contents/49">조직도</a><a href="/kor/contents/91">정책Q&amp;A</a></nav></body></html>'
    sitemap = ("<?xml version='1.0'?><urlset>"
               + "".join(f"<url><loc>{{base}}/kor/article/{b}</loc><lastmod>2026-10-15</lastmod></url>" for b in boards)
               + "".join(f"<url><loc>{{base}}/kor/contents/{i}</loc><lastmod>2026-10-14</lastmod></url>" for i in (49, 91, 99))
               + "</urlset>")
    routes = {"/": (200, "text/html; charset=utf-8", home),
              "/robots.txt": (200, "text/plain", "User-agent: *\nAllow: /\nSitemap: {base}/sitemap.xml\n"),
              "/sitemap.xml": (200, "application/xml", sitemap)}
    for b, name in boards.items():
        rows = "".join(f'<tr><td><a href="/kor/article/{b}/{1000 + i}/view?mno=&amp;pageIndex=1">{name} {i}번</a></td>'
                       f"<td>{_kdate(i)}</td></tr>" for i in range(1, 7))
        routes[f"/kor/article/{b}"] = (200, "text/html; charset=utf-8",
                                       f'<html><head><title>{name} &lt; 테스트부</title><meta property="og:title" content="테스트부"></head>'
                                       f"<body><nav>{nav}</nav><table><tr><th>제목</th><th>등록일</th></tr>{rows}</table></body></html>")
        for i in range(1, 7):
            routes[f"/kor/article/{b}/{1000 + i}/view"] = (200, "text/html; charset=utf-8", f"""<html>{head}<body>
            <h1>테스트부</h1><article><div class="board-detail"><div class="detail-tit"><span>{name} {i}번 안내</span></div>
            <div class="detail-info"><ul><li><em>담당자</em><span>홍길동</span></li><li><em>등록일</em><span>{_kdate(i)}</span></li>
            <li class="info-down"><em>첨부파일</em><a href="/attach/down/abc{i}">자료_{i}.pdf [100 KB]</a></li></ul></div>
            <div class="detail-cont"><p>{BODY}</p></div></div></article></body></html>""")
    return routes


def gov_board_js_routes() -> dict:
    """공공기관 게시판(과기부형): 목록 페이지는 스크립트로 그려져 링크가 없고, 홈 행에만 날짜가 보인다.
    상세에는 제목(.view_head h2)과 짧은 안내문뿐이고 내용은 스크립트로 내려받는 HWP 첨부에 있다."""
    posts = range(3187801, 3187807)
    rows = "".join(f'<li><a href="/bbs/view.do?sCode=user&amp;mId=307&amp;bbsSeqNo=94&amp;nttSeqNo={n}">보도자료 {n}</a>'
                   f"<span>{_sdate(i)}</span></li>" for i, n in enumerate(posts))
    home = ("<html><head><title>테스트청</title></head><body>"
            '<nav><a href="/bbs/list.do?sCode=user&amp;mId=307">보도자료</a></nav>'
            f'<ul class="swiper"><li class="swiper-slide"><a href="/bbs/view.do?sCode=user&amp;mId=307&amp;bbsSeqNo=94&amp;nttSeqNo={posts[0]}">배너</a></li></ul>'
            f"<ul>{rows}</ul></body></html>")
    routes = {"/": (200, "text/html; charset=utf-8", home),
              "/robots.txt": (404, "text/plain", ""),
              "/bbs/list.do": (200, "text/html; charset=utf-8", "<html><body><div id='list'></div><script>load()</script></body></html>")}
    for n in posts:
        # 정규 주소는 쿼리 키를 정렬하므로 경로 키도 그 순서로 둔다
        routes[f"/bbs/view.do?bbsSeqNo=94&mId=307&nttSeqNo={n}&sCode=user"] = (200, "text/html; charset=utf-8", f"""<html>
        <head><title>보도자료 - 테스트청</title></head><body><div class="board_view"><div class="view_head">
        <h2>보도자료 {n}번 제목입니다</h2><div class="meta"><dl><dt>부서</dt><dd>홍보과</dd></dl></div></div>
        <div class="board_notcon">보도자료 내용입니다. 자세한 내용은 첨부파일을 참고하시기 바랍니다.</div>
        <div class="view_file"><ul><li><a class="ico_file_hwp" title="파일명">자료_{n}.hwpx</a>
        <span><a class="down" href="javascript:void(0);" onclick="fn_download('{n}', '1', 'hwpx');">다운로드</a></span></li></ul></div>
        </div></body></html>""")
    return routes


def portal_cms_routes(post_ids=range(1, 7)) -> dict:
    """공공기관 포털(자체 CMS): 메타 태그와 <title>이 모두 사이트 이름이고, 글 제목은 .headline-area strong,
    등록일은 라벨 없이 .reg에 "2026.10.14"로만 있다. 목록 행에도 날짜가 없어 휴리스틱으로는 발행일을 못 찾는다.
    셀렉터를 아는 레시피(LLM)가 있어야 통과하는 사이트다."""
    items = "".join(f'<li><a href="/portal/bbs/view.do?bbsId=B01&amp;nttId={i}">포털 보도자료 {i}번 안내</a></li>'
                    for i in post_ids)
    head = ('<head><title>테스트포털</title><meta property="og:title" content="테스트포털">'
            '<meta property="og:site_name" content="테스트포털"></head>')
    routes = {
        "/": (200, "text/html; charset=utf-8",
              f'<html>{head}<body><nav><a href="/portal/bbs/list.do?bbsId=B01">보도자료</a>'
              f'<a href="/portal/intro.do">기관 소개</a></nav><ul>{items}</ul></body></html>'),
        "/robots.txt": (404, "text/plain", ""),
        "/portal/bbs/list.do": (200, "text/html; charset=utf-8", f"<html>{head}<body><ul>{items}</ul></body></html>"),
        "/portal/intro.do": (200, "text/html; charset=utf-8", f"<html>{head}<body><p>소개</p></body></html>"),
    }
    for i in post_ids:
        # 정규 주소는 쿼리 키를 정렬하므로 경로 키도 그 순서로 둔다
        routes[f"/portal/bbs/view.do?bbsId=B01&nttId={i}"] = (200, "text/html; charset=utf-8", f"""<html>{head}<body>
        <div class="post"><p class="headline-area"><strong>포털 보도자료 {i}번 안내</strong></p>
        <p class="reg">{(datetime.now(timezone(timedelta(hours=9))) - timedelta(days=i)).strftime("%Y.%m.%d")}</p>
        <div class="cont-area"><p>{BODY}</p></div></div></body></html>""")
    return routes
