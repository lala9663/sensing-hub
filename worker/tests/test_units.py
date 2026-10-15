from datetime import datetime, timedelta, timezone

import httpx
import pytest

from sensing_worker.codes import Code, CrawlFailure
from sensing_worker.config import Settings
from sensing_worker.discovery import (Discoverer, canonical_article_url, matches, parse_sitemap, pattern_from_signature,
                                      signature, sitemap_urls)
from sensing_worker.evidence import EvidenceStore
from sensing_worker.extract import Article, extract_article, links_with_dates
from sensing_worker.jobs import discover
from sensing_worker.fetcher import Page, PoliteFetcher, client_redirect_target
from sensing_worker.policy import defaults_for, guess_site_type, passes_quality
from sensing_worker.throttle import LocalThrottle, throttle_key


def test_signature_and_pattern_for_ndsoft():
    sig = signature("https://daily.hankooki.com/news/articleView.html?idxno=1415376")
    assert sig == "/news/articleView.html?idxno={n}"
    pattern = Discoverer._dominant_pattern([f"https://x.com/news/articleView.html?idxno={i}" for i in range(5)]
                                           + ["https://x.com/news/articleList.html?sc_section_code=S1N1"] * 5)
    assert pattern["signature"] == "/news/articleView.html?idxno={n}"
    assert matches("https://x.com/news/articleView.html?idxno=99", pattern)
    assert not matches("https://x.com/news/articleView.html?idxno=abc", pattern)
    assert not matches("https://x.com/news/articleList.html?sc_section_code=S1N1", pattern)


def test_canonical_url_drops_tracking_params():
    pattern = {"path": r"^/board/view\.do$", "query_keys": ["nttId"], "id_keys": ["nttId"]}
    url = "https://a.go.kr/board/view.do;jsessionid=ABC?nttId=3&utm_source=rss"
    assert canonical_article_url(url, pattern) == "https://a.go.kr/board/view.do?nttId=3"


def test_extract_meta_and_clean_title():
    html = """<html><head><title>삼성전자, PIM 기술 적용 확대 &lt; IT일반 &lt; 기사본문 - 데일리한국</title>
    <meta property="og:type" content="article">
    <meta property="article:published_time" content="2026-10-15T18:54:21+09:00"></head>
    <body><article><p>""" + "내용 " * 120 + "</p></article></body></html>"
    art = extract_article(html, "https://daily.hankooki.com/news/articleView.html?idxno=1")
    assert art.title == "삼성전자, PIM 기술 적용 확대"
    assert art.published_at == datetime(2026, 10, 15, 9, 54, 21, tzinfo=timezone.utc)
    assert art.body_length >= 200 and art.is_article_like


def test_extract_korean_board_date_and_attachment():
    html = """<html><body><div class="board_view"><h3>공지</h3><span>등록일 2026.10.14 09:30</span>
    <p>""" + "공지 본문입니다. " * 30 + """</p><a href="/cmm/fileDown.do?atchFileId=F1">계획서.hwp</a>
    <a href="https://other.com/x.html">외부</a></div></body></html>"""
    art = extract_article(html, "https://www.test.go.kr/board/view.do?nttId=1")
    assert art.published_at == datetime(2026, 10, 14, 0, 30, tzinfo=timezone.utc)  # KST 09:30
    assert art.attachments == [("https://www.test.go.kr/cmm/fileDown.do?atchFileId=F1", "계획서.hwp")]


def test_quality_gate():
    now = datetime.now(timezone.utc)
    good = Article(url="u", title="t", published_at=now - timedelta(days=1), body="x" * 300)
    bad = Article(url="u", title=None, published_at=None, body="")
    assert passes_quality([good] * 4 + [bad])[0]
    assert not passes_quality([good] * 3 + [bad] * 2)[0]
    old = Article(url="u", title="t", published_at=now - timedelta(days=90), body="x" * 300)
    assert not passes_quality([old] * 5)[0]


def test_site_type_and_defaults():
    assert guess_site_type("www.hscity.go.kr", "") == "PUBLIC"
    assert guess_site_type("daily.hankooki.com", "") == "NEWS"
    assert defaults_for("NEWS")["store_scope"] == "TITLE_LINK"
    assert defaults_for("PUBLIC")["collect_attachments"] is True


def _fetcher(handler) -> PoliteFetcher:
    s = Settings(min_interval_seconds=0, max_retries=2, allow_private_hosts=True)
    return PoliteFetcher(s, transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("status,body,code", [
    (403, "<title>Just a moment...</title>", Code.BOT_CHALLENGE),
    (429, "slow down", Code.RATE_LIMITED),
    (404, "nope", Code.HTTP_4XX),
    (503, "down", Code.HTTP_5XX),
    (200, "<html><div class='g-recaptcha'></div></html>", Code.BOT_CHALLENGE),
    (200, "<form><input type='password'></form>", Code.LOGIN_REQUIRED),
])
def test_fetcher_classifies_failures(status, body, code):
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(status, text=body)

    with pytest.raises(CrawlFailure) as e:
        _fetcher(handler).get("https://site.example/page")
    assert e.value.code == code


def test_fetcher_follows_redirect_to_login_as_login_required():
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(302, headers={"location": "/member/login.html?ret=/a"})

    with pytest.raises(CrawlFailure) as e:
        _fetcher(handler).get("https://site.example/a")
    assert e.value.code == Code.LOGIN_REQUIRED


def test_robots_disallow_and_ai_bot_block():
    robots = "User-agent: GPTBot\nDisallow: /\n\nUser-agent: *\nDisallow: /private/\n"

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=robots)
        return httpx.Response(200, text="<html>ok</html>")

    f = _fetcher(handler)
    assert f.robots_status("https://site.example") == "PARTIAL"
    assert f.ai_bots_blocked("https://site.example")
    with pytest.raises(CrawlFailure) as e:
        f.get("https://site.example/private/x")
    assert e.value.code == Code.ROBOTS_DISALLOWED


def test_proxy_refusal_is_network_blocked():
    def handler(request):
        raise httpx.ProxyError("403 Forbidden")

    with pytest.raises(CrawlFailure) as e:
        _fetcher(handler).get("https://site.example/a")
    assert e.value.code == Code.NETWORK_BLOCKED and e.value.blocking


def test_internal_address_is_blocked_without_flag():
    f = PoliteFetcher(Settings(min_interval_seconds=0))
    with pytest.raises(CrawlFailure) as e:
        f.get("http://127.0.0.1:9/")
    assert e.value.code == Code.INTERNAL_ADDRESS


def test_local_throttle_reserves_slots_per_domain():
    t = LocalThrottle(10)
    assert t.reserve("www.a.com") == 0
    assert 9.9 < t.reserve("a.com") <= 10
    assert 19.9 < t.reserve("a.com") <= 20
    assert t.reserve("b.com") == 0
    assert throttle_key("WWW.News.Example.COM.") == "news.example.com"


def test_evidence_store_writes_relative_path_and_prunes(tmp_path):
    store = EvidenceStore(str(tmp_path), retention_days=30)
    assert store.save(1, CrawlFailure(Code.TIMEOUT, url="https://a.com")) is None   # 본문 없으면 남기지 않음
    rel = store.save(7, CrawlFailure(Code.BOT_CHALLENGE, url="https://a.com/x--y", http_status=403,
                                     evidence="<html>challenge</html>"))
    assert rel and rel.split("/")[1].startswith("job7-bot_challenge-")
    text = (tmp_path / rel).read_text(encoding="utf-8")
    assert text.endswith("<html>challenge</html>") and "x%2D%2Dy" in text and "http_status: 403" in text
    (tmp_path / "20000101").mkdir()
    assert store.prune() == 1 and (tmp_path / rel).exists()


def test_http_errors_carry_evidence():
    def handler(request):
        return httpx.Response(404, text="<html>gone</html>")
    f = PoliteFetcher(Settings(min_interval_seconds=0, allow_private_hosts=True), transport=httpx.MockTransport(handler))
    with pytest.raises(CrawlFailure) as e:
        f.get("https://a.example/x")
    assert e.value.code == Code.HTTP_4XX and e.value.evidence == "<html>gone</html>"


def test_discovery_eval_classifies_sites_like_onboarding():
    from fakesite import Site, blocked_site_routes, news_site_routes
    from sensing_worker.evaluate import evaluate_site, read_sites, summarize

    settings = Settings(min_interval_seconds=0, max_retries=1, allow_private_hosts=True)
    news, blocked = Site(news_site_routes()), Site(blocked_site_routes())
    try:
        ok = evaluate_site(news.base + "/news/articleView.html?idxno=101", settings)
        assert ok.outcome == "OK" and ok.method == "STATIC" and ok.site_type == "NEWS" and ok.requests > 0
        assert ok.url == news.base                                   # 기사 주소를 넣어도 사이트 단위로 본다
        bad = evaluate_site(blocked.base, settings)
        assert bad.outcome == "BLOCKED" and bad.code == "BOT_CHALLENGE"
        text = summarize([ok, bad], target=2)
        assert "자동 등록 성공 1/2곳 (목표 2곳: 미달)" in text
    finally:
        news.close()
        blocked.close()
    assert read_sites(["a.com", "a.com", "b.com"]) == ["a.com", "b.com"]


def test_signature_generalizes_alphanumeric_ids():
    assert signature("https://www.motir.go.kr/kor/article/ATCL6fd477428") == "/kor/article/{id}"
    assert signature("https://a.com/view/20261016A003") == "/view/{id}"
    assert signature("https://a.com/news/2026/10/16/story") == "/news/{n}/{n}/{n}/story"
    assert signature("https://a.com/page2.html") == "/page{n}.html"       # 짧은 토큰은 숫자만 바꾼다
    pattern = Discoverer._dominant_pattern([f"https://m.go.kr/kor/article/ATCL{h}" for h in ("6fd477428", "f589e9758", "0a1b2c3d4")]
                                           + ["https://m.go.kr/kor/contents/49"] * 3)
    assert pattern["signature"] == "/kor/article/{id}"
    assert matches("https://m.go.kr/kor/article/ATCL111aaa222", pattern)
    assert not matches("https://m.go.kr/kor/article/list", pattern)


def test_client_side_redirect_is_followed():
    def handler(request):
        if request.url.path == "/":
            return httpx.Response(200, headers={"content-type": "text/html;charset=UTF-8"},
                                  text='<script>\r\nlocation.href = "/index.do";\r\n</script>\r\n')
        if request.url.path == "/index.do":
            return httpx.Response(200, headers={"content-type": "text/html"}, text="<html><body>" + "<p>home</p>" * 5 + "</body></html>")
        return httpx.Response(404, text="")
    f = PoliteFetcher(Settings(min_interval_seconds=0, allow_private_hosts=True), transport=httpx.MockTransport(handler))
    page = f.get("https://msit.example/")
    assert page.url == "https://msit.example/index.do" and "home" in page.text
    big = Page("u", 200, "text/html", "<html>" + "x" * 5000 + 'location.href="/x"</html>', {})
    assert client_redirect_target(big) is None                           # 본문이 있는 페이지의 스크립트는 무시
    meta = Page("u", 200, "text/html", '<meta http-equiv="refresh" content="0; url=/main.do">', {})
    assert client_redirect_target(meta) == "/main.do"


SITEMAP_INDEX = """<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<sitemap><loc>https://k.kr/sitemap_old.xml</loc><lastmod>2024-01-01</lastmod></sitemap>
<sitemap><loc>https://k.kr/sitemap_news.xml</loc><lastmod>2026-10-16T09:00:00+09:00</lastmod></sitemap>
<sitemap><loc>https://k.kr/sitemap_speech.xml</loc><lastmod>2026-09-01</lastmod></sitemap>
</sitemapindex>"""
SITEMAP_NEWS = """<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://k.kr/news/view.do?newsId=1</loc><lastmod>2026-10-01</lastmod></url>
<url><loc>https://k.kr/news/view.do?newsId=3</loc><lastmod>2026-10-16</lastmod></url>
<url><loc>https://k.kr/news/view.do?newsId=2</loc><lastmod>2026-10-10</lastmod></url>
</urlset>"""


def test_sitemap_prefers_newest_by_lastmod():
    assert parse_sitemap(SITEMAP_INDEX)[1] == ["https://k.kr/sitemap_news.xml", "https://k.kr/sitemap_speech.xml",
                                               "https://k.kr/sitemap_old.xml"]
    opened = []

    def get(url):
        opened.append(url)
        return Page(url, 200, "application/xml", SITEMAP_NEWS if "news" in url else "<urlset></urlset>", {})

    urls = sitemap_urls(get, SITEMAP_INDEX, max_children=2)
    assert opened == ["https://k.kr/sitemap_news.xml", "https://k.kr/sitemap_speech.xml"]   # 오래된 것은 안 연다
    assert urls == ["https://k.kr/news/view.do?newsId=3", "https://k.kr/news/view.do?newsId=2",
                    "https://k.kr/news/view.do?newsId=1"]


def test_site_type_from_jsonld_and_article_links():
    hankyung_like = ('<html><head><meta property="og:type" content="website">'
                     '<meta property="og:site_name" content="한국경제"></head><body>'
                     '<script type="application/ld+json">{"@type":"NewsMediaOrganization"}</script></body></html>')
    assert guess_site_type("hankyung.com", hankyung_like) == "NEWS"
    links_only = ('<html><head><meta property="og:site_name" content="서울경제"></head><body>'
                  + "".join(f'<a href="/article/{i}">기사</a>' for i in range(12)) + "</body></html>")
    assert guess_site_type("sedaily.com", links_only) == "NEWS"
    assert guess_site_type("korea.kr", "<html></html>") == "PUBLIC"
    assert guess_site_type("acme.com", '<meta property="og:site_name" content="경제연구소"><a href="/about">x</a>') == "ETC"


def test_title_and_date_from_board_markup():
    html = ('<html><head><title>테스트부</title><meta property="og:title" content="테스트부">'
            '<meta property="og:site_name" content="테스트부"></head><body><h1>테스트부</h1>'
            '<div class="board-detail"><div class="detail-tit"><span>중견기업 성장을 위한 10대 프로젝트 시작한다</span></div>'
            '<div class="detail-info"><ul><li><em>담당자</em><span>홍길동</span></li>'
            '<li><em>등록일</em><span>2026-10-15</span></li></ul></div><p>' + "본문 " * 100 + "</p></div></body></html>")
    art = extract_article(html, "https://m.go.kr/kor/article/ATCLx/1/view")
    assert art.title == "중견기업 성장을 위한 10대 프로젝트 시작한다"          # og:title(사이트 이름)은 무시
    assert art.published_at == datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc)   # KST 자정
    # 목록 페이지의 표 머리글 "등록일"은 아래 행의 날짜를 집지 않는다 (게시판을 기사로 오인하지 않게)
    board = "<html><body><table><tr><th>제목</th><th>등록일</th></tr><tr><td>글</td><td>2026-10-15</td></tr></table></body></html>"
    assert extract_article(board, "https://m.go.kr/b").published_at is None
    assert extract_article(board, "https://m.go.kr/b", date_hint="2026. 10. 15").published_at is not None


def test_js_download_attachment_is_recorded_with_filename():
    html = ('<html><body><div class="view_file"><ul><li><a class="ico_file_hwp" title="파일명">261014 보도자료.hwpx</a>'
            '<span><a class="down" href="javascript:void(0);" onclick="fn_download(\'55308\', \'1\', \'hwpx\');">다운로드</a>'
            '<a class="view" href="javascript:void(0);" onclick="getExtension_path(\'55308\', \'1\');">바로보기</a></span></li></ul></div>'
            "</body></html>")
    art = extract_article(html, "https://www.msit.go.kr/bbs/view.do?nttSeqNo=1")
    assert art.attachments == [("https://www.msit.go.kr/bbs/view.do?nttSeqNo=1#js:fn_download('55308', '1', 'hwpx');",
                                "261014 보도자료.hwpx")]


def test_links_with_dates_reads_row_date():
    html = ('<ul><li><a href="/bbs/view.do?nttSeqNo=1">보도자료 1</a><span>2026. 10. 15</span></li>'
            '<li class="swiper-slide"><a href="/bbs/view.do?nttSeqNo=2">카드뉴스</a></li></ul>'
            '<div><a href="/about">소개</a>' + "x" * 500 + " 2024-01-01</div>")
    assert links_with_dates(html, "https://g.go.kr/") == [
        ("https://g.go.kr/bbs/view.do?nttSeqNo=1", "2026. 10. 15"),
        ("https://g.go.kr/bbs/view.do?nttSeqNo=2", None),
        ("https://g.go.kr/about", None)]                                   # 큰 묶음의 날짜는 행 날짜가 아니다


def test_sections_found_by_korean_board_names():
    f = PoliteFetcher(Settings(min_interval_seconds=0, allow_private_hosts=True))
    d = Discoverer(f, "m.go.kr", "https://m.go.kr")
    html = ('<a href="/kor/contents/49">조직도</a><a href="/kor/article/ATCL369bcdf07">알림/소식</a>'
            '<a href="/kor/article/ATCL3f49a5a8c">보도자료</a><a href="/news/list.do">목록</a><a href="/">홈</a>'
            '<a href="/news/articleView.html?idxno=101">기사 제목 101번 소식</a>'      # 글 제목은 섹션이 아니다
            '<a href="/news/articleView.html?idxno=102">올해 뉴스 결산 발표</a>')
    assert [s["name"] for s in d._sections(html, "https://m.go.kr/")] == ["목록", "보도자료", "알림/소식"]
    f.close()


def test_page_params_are_not_part_of_identity():
    sig = signature("https://m.go.kr/kor/article/ATCL3f49a5a8c/172296/view?mno=&pageIndex=1")
    assert sig == "/kor/article/{id}/{n}/view?mno=*"
    pattern = pattern_from_signature(sig)
    assert canonical_article_url("https://m.go.kr/kor/article/ATCL3f49a5a8c/172296/view?mno=&pageIndex=2", pattern) \
        == "https://m.go.kr/kor/article/ATCL3f49a5a8c/172296/view?mno="


def test_quality_accepts_attachment_instead_of_body_for_public_sites():
    now = datetime.now(timezone.utc)
    stub = Article(url="u", title="t", published_at=now, body="첨부 참고", attachments=[("https://g.go.kr/f.hwp", "f.hwp")])
    assert not passes_quality([stub] * 5)[0]
    assert passes_quality([stub] * 5, attachments_ok=True)[0]


def test_discover_skips_sitemap_boards_and_finds_posts_in_sections():
    from fakesite import Site, gov_cms_routes
    site = Site({})
    site.routes.update({k: (s, t, b.replace("{base}", site.base)) for k, (s, t, b) in gov_cms_routes().items()})
    f = PoliteFetcher(Settings(min_interval_seconds=0, max_retries=1, allow_private_hosts=True))
    try:
        assert next(Discoverer(f, "127.0.0.1", site.base).candidates()).method == "SITEMAP"   # 첫 후보는 게시판 목록
        result, detail = discover(f, "127.0.0.1", site.base, "PUBLIC")
        assert result.method == "STATIC"
        assert result.article_pattern["signature"] == "/kor/article/{id}/{n}/view?mno=*"
        assert "5건 통과" in detail and len(result.sample_dates) >= 5
    finally:
        f.close()
        site.close()


def test_discover_js_board_uses_row_dates_and_attachments():
    from fakesite import Site, gov_board_js_routes
    site = Site(gov_board_js_routes())
    f = PoliteFetcher(Settings(min_interval_seconds=0, max_retries=1, allow_private_hosts=True))
    try:
        result, detail = discover(f, "127.0.0.1", site.base, "PUBLIC")
        assert result.method == "STATIC" and result.article_pattern["signature"].startswith("/bbs/view.do?")
        assert len(result.sample_dates) >= 5 and "5건 통과" in detail
        with pytest.raises(CrawlFailure) as e:
            discover(f, "127.0.0.1", site.base, "NEWS")       # 언론사 기준(본문 필수)으로는 불합격
        assert e.value.code == Code.QUALITY_FAILED
    finally:
        f.close()
        site.close()


def test_board_page_linking_to_its_posts_is_hub_like():
    rows = "".join(f'<li><a href="/kor/article/ATCL1/{1000 + i}/view?mno=">글 {i}</a><span>2026-10-{10 + i}</span></li>'
                   for i in range(5))
    board = f'<html><head><title>보도자료</title></head><body><ul>{rows}</ul><p>{"본문 " * 100}</p></body></html>'
    art = extract_article(board, "https://m.go.kr/kor/article/ATCL1", date_hint="2026-10-15")
    assert art.is_hub_like and not art.is_article_like
    assert not passes_quality([art] * 5, attachments_ok=True)[0]
    post = f'<html><head><title>글 1</title></head><body><a href="/kor/article/ATCL1">목록</a><p>{"본문 " * 100}</p></body></html>'
    assert not extract_article(post, "https://m.go.kr/kor/article/ATCL1/1001/view?mno=", date_hint="2026-10-15").is_hub_like
