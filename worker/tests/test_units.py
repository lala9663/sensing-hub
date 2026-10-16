import json
import ssl
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest

from fakellm import FakeLlm, portal_recipe
from sensing_worker.codes import Code, CrawlFailure
from sensing_worker.config import Settings
from sensing_worker.discovery import (Discoverer, canonical_article_url, matches, parse_sitemap, pattern_from_signature,
                                      signature, sitemap_urls)
from sensing_worker.evidence import EvidenceStore
from sensing_worker.extract import Article, extract_article, links_with_dates
from sensing_worker.jobs import discover
from sensing_worker.fetcher import Page, PoliteFetcher, client_redirect_target, compat_ssl_context
from sensing_worker.llm import (FALLBACK_BETA, AnthropicLlm, LlmUnavailable, NoLlm, OpenAiCompatLlm, extract_json,
                                generate_recipe, make_llm, validate_schema)
from sensing_worker.policy import defaults_for, guess_site_type, passes_quality
from sensing_worker.recipe import RECIPE_SCHEMA, clean_html, quick_check
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


def _tls_fetcher(strict_handler, compat_handler, **overrides) -> PoliteFetcher:
    """기본 클라이언트와 호환 TLS 클라이언트에 서로 다른 MockTransport를 달아 어느 쪽으로 갔는지 구분한다."""
    s = Settings(min_interval_seconds=0, max_retries=2, allow_private_hosts=True, **overrides)
    return PoliteFetcher(s, transport=httpx.MockTransport(strict_handler),
                         compat_transport=httpx.MockTransport(compat_handler))


def _handshake_failure(request: httpx.Request) -> httpx.ConnectError:
    err = ssl.SSLError(1, "[SSL: SSLV3_ALERT_HANDSHAKE_FAILURE] sslv3 alert handshake failure (_ssl.c:1010)")
    failure = httpx.ConnectError(str(err), request=request)
    failure.__cause__ = err
    return failure


def test_tls_handshake_failure_falls_back_to_compat_client_and_remembers_host():
    strict_calls, compat_calls = [], []

    def strict(request):
        strict_calls.append(str(request.url))
        if request.url.host == "old.example":
            raise _handshake_failure(request)
        return httpx.Response(404 if request.url.path == "/robots.txt" else 200, text="modern")

    def compat(request):
        compat_calls.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, headers={"content-type": "text/html"}, text="<html><p>legacy ok</p></html>")

    f = _tls_fetcher(strict, compat)
    page = f.get("https://old.example/a")
    assert page.status == 200 and "legacy ok" in page.text
    # robots.txt가 첫 요청: 기본 클라이언트가 핸드셰이크에 실패하면 같은 주소를 호환 클라이언트로 한 번만 다시 보낸다
    assert strict_calls == ["https://old.example/robots.txt"]
    assert compat_calls == ["https://old.example/robots.txt", "https://old.example/a"]
    assert "old.example" in f.compat_hosts

    # 같은 호스트의 다음 요청은 기본 클라이언트를 거치지 않고 바로 호환 클라이언트로 간다
    f.get("https://old.example/b")
    assert strict_calls == ["https://old.example/robots.txt"]
    assert compat_calls[-1] == "https://old.example/b"
    assert f.request_count == 3

    # 다른 호스트는 영향을 받지 않는다
    assert f.get("https://fine.example/x").text == "modern"
    assert compat_calls[-1] == "https://old.example/b"
    assert "fine.example" not in f.compat_hosts


def test_tls_error_on_compat_client_too_is_tls_error():
    def strict(request):
        raise _handshake_failure(request)

    compat_calls = []

    def compat(request):
        compat_calls.append(str(request.url))
        raise _handshake_failure(request)

    f = _tls_fetcher(strict, compat)
    with pytest.raises(CrawlFailure) as e:
        f.get("https://old.example/a")
    assert e.value.code == Code.TLS_ERROR
    assert "호환 설정" in str(e.value) and "HANDSHAKE_FAILURE" in str(e.value)
    # robots.txt와 본문 각각 호환 재시도는 한 번뿐이고, 호스트는 호환 목록에 남지 않는다
    assert compat_calls == ["https://old.example/robots.txt", "https://old.example/a"]
    assert f.compat_hosts == set()


def test_certificate_verification_failure_is_not_retried_with_compat():
    strict_calls, compat_calls = [], []

    def strict(request):
        strict_calls.append(str(request.url))
        err = ssl.SSLCertVerificationError(1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
                                              "certificate has expired (_ssl.c:1010)")
        failure = httpx.ConnectError(str(err), request=request)
        failure.__cause__ = err
        raise failure

    def compat(request):
        compat_calls.append(str(request.url))
        return httpx.Response(200, text="should not be reached")

    f = _tls_fetcher(strict, compat)
    with pytest.raises(CrawlFailure) as e:
        f.get("https://expired.example/a")
    assert e.value.code == Code.TLS_ERROR
    assert "인증서 검증 실패" in str(e.value) and "expired" in str(e.value)
    assert strict_calls == ["https://expired.example/robots.txt", "https://expired.example/a"]
    assert compat_calls == []


def test_certificate_failure_reported_only_as_text_is_still_not_retried():
    # httpx는 OpenSSL 메시지를 문자열로만 넘기기도 한다 (__cause__ 없음)
    def strict(request):
        raise httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: Hostname mismatch",
                                 request=request)

    compat_calls = []

    def compat(request):
        compat_calls.append(str(request.url))
        return httpx.Response(200, text="no")

    with pytest.raises(CrawlFailure) as e:
        _tls_fetcher(strict, compat).get("https://mismatch.example/a")
    assert e.value.code == Code.TLS_ERROR and "인증서 검증 실패" in str(e.value)
    assert compat_calls == []


def test_tls_compat_can_be_switched_off():
    def strict(request):
        raise _handshake_failure(request)

    compat_calls = []

    def compat(request):
        compat_calls.append(str(request.url))
        return httpx.Response(200, text="no")

    f = _tls_fetcher(strict, compat, tls_compat=False)
    with pytest.raises(CrawlFailure) as e:
        f.get("https://old.example/a")
    assert e.value.code == Code.TLS_ERROR and "호환 재시도 꺼짐" in str(e.value)
    assert compat_calls == []
    assert f.compat_hosts == set()


def test_compat_ssl_context_keeps_certificate_verification():
    ctx = compat_ssl_context()
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
    assert ctx.minimum_version == ssl.TLSVersion.TLSv1_2
    assert ctx.options & 0x4  # SSL_OP_LEGACY_SERVER_CONNECT
    names = {c["name"] for c in ctx.get_ciphers()}
    # RSA 키 교환(ECDHE 없음) 암호가 다시 허용된다: 용인시처럼 그것만 지원하는 서버용
    assert "AES256-GCM-SHA384" in names
    assert "AES256-GCM-SHA384" not in {c["name"] for c in ssl.create_default_context().get_ciphers()}


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
        assert ok.recipe_source == "heuristic"                      # LLM 설정이 없으면 휴리스틱
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


# ---------------------------------------------------------------------- LLM 레시피

def test_extract_json_from_fenced_or_chatty_response():
    fenced = '물론입니다. 레시피는 다음과 같습니다.\n```json\n{"a": 1, "b": [1, 2]}\n```\n도움이 되길 바랍니다.'
    assert extract_json(fenced) == {"a": 1, "b": [1, 2]}
    chatty = 'Here you go: {"hubs": ["https://x"], "n": {"k": "v}"}} trailing } text'
    assert extract_json(chatty) == {"hubs": ["https://x"], "n": {"k": "v}"}}
    with pytest.raises(ValueError):
        extract_json("JSON을 만들 수 없습니다.")
    with pytest.raises(ValueError):
        extract_json("[1, 2, 3]")


def test_schema_validation_without_jsonschema():
    good = json.loads(portal_recipe("https://p.go.kr"))
    assert validate_schema(good, RECIPE_SCHEMA) == []
    bad = {**good, "extra": 1, "selectors": {"title": 3, "date": None}, "hubs": "https://p.go.kr/"}
    del bad["notes"]
    errors = validate_schema(bad, RECIPE_SCHEMA)
    assert any("$.notes" in e for e in errors) and any("$.extra" in e for e in errors)
    assert any("$.hubs" in e and "array" in e for e in errors)
    assert any("$.selectors.title" in e for e in errors) and any("$.selectors.body" in e for e in errors)


def test_quick_check_rejects_bad_selector_signature_and_foreign_hub():
    data = json.loads(portal_recipe("https://p.go.kr", article_signature="/portal/bbs/view.do",
                                    selectors={"title": "div[[", "date": None, "body": None, "attachment": None},
                                    hubs=["https://other.com/list", "/relative"]))
    errors = quick_check(data, "p.go.kr")
    assert any("selectors.title" in e for e in errors)
    assert any("article_signature" in e and "{n}" in e for e in errors)
    assert any("같은 사이트" in e for e in errors) and any("절대 URL" in e for e in errors)
    assert quick_check(json.loads(portal_recipe("https://p.go.kr")), "p.go.kr") == []


def test_generate_recipe_retries_once_with_validation_errors():
    schema = {"type": "object", "additionalProperties": False, "required": ["x"], "properties": {"x": {"type": "string"}}}
    llm = FakeLlm(['{"x": 1}', '{"x": "ok"}'])
    gen = generate_recipe(llm, "sys", "user", schema)
    assert gen.data == {"x": "ok"} and gen.attempts == 2
    assert "[이전 응답의 문제]" in llm.calls[1][1] and "$.x" in llm.calls[1][1]
    assert gen.usage == {"input_tokens": 2000, "output_tokens": 100}

    llm = FakeLlm(['{"x": "a"}', '{"x": "b"}', '{"x": "c"}'])
    gen = generate_recipe(llm, "sys", "user", schema, check=lambda d: [] if d["x"] == "c" else ["x는 c여야 합니다"])
    assert gen.data is None and gen.attempts == 2 and gen.errors == ["x는 c여야 합니다"]   # 한 번만 재요청

    with pytest.raises(LlmUnavailable):
        generate_recipe(FakeLlm(error=LlmUnavailable("down")), "sys", "user", schema)


def test_clean_html_strips_scripts_and_truncates():
    html = "<html><head><script>var a=1</script><style>p{}</style></head><body><svg><path/></svg><!-- c --><p>본문   글</p></body></html>"
    text = clean_html(html, 1000)
    assert "var a" not in text and "svg" not in text and "<!--" not in text and "<p>본문 글</p>" in text
    assert clean_html("<p>" + "x" * 100 + "</p>", 20).endswith("...(잘림)")


def _portal_site():
    from fakesite import Site, portal_cms_routes
    return Site(portal_cms_routes())


def test_llm_recipe_rescues_site_that_heuristics_reject():
    site = _portal_site()
    f = PoliteFetcher(Settings(min_interval_seconds=0, max_retries=1, allow_private_hosts=True))
    settings = Settings(min_interval_seconds=0, allow_private_hosts=True)
    try:
        with pytest.raises(CrawlFailure) as e:
            discover(f, "127.0.0.1", site.base, "ETC")               # 라벨 없는 날짜: 휴리스틱으로는 발행일을 못 찾는다
        assert e.value.code == Code.QUALITY_FAILED

        llm = FakeLlm([portal_recipe(site.base)])
        steps: list[str] = []
        result, detail = discover(f, "127.0.0.1", site.base, "ETC", llm=llm, settings=settings, progress=steps.append)
        assert result.recipe_source == "llm" and result.method == "STATIC" and result.llm_model == "fake fake-1"
        assert result.hubs == [f"{site.base}/portal/bbs/list.do?bbsId=B01"]
        assert result.article_pattern["signature"] == "/portal/bbs/view.do?bbsId=*&nttId={n}"
        assert result.selectors == {"title": ".headline-area strong", "date": ".reg", "body": ".cont-area"}
        assert result.date_format == "%Y.%m.%d" and len(result.sample_urls) >= 5
        assert "5건 통과" in detail and "fake fake-1" in detail and "토큰 입력 1,000" in detail
        assert steps == ["사이트 구조를 분석해 수집 방법을 만드는 중입니다."]
        config = result.to_config()
        assert config["recipe_source"] == "llm" and config["selectors"]["title"] == ".headline-area strong"
        # LLM에 보여 준 입력: 홈 HTML, 샘플 페이지, 휴리스틱 후보와 서명 통계
        system, user, schema = llm.calls[0]
        assert schema is RECIPE_SCHEMA and "JSON 객체만" in system
        assert "[홈 페이지 HTML" in user and "[샘플 페이지 1" in user and "/portal/bbs/view.do?bbsId=*&nttId={n}" in user
        assert "<script" not in user
    finally:
        f.close()
        site.close()


def test_llm_recipe_with_too_few_matching_links_is_rejected():
    site = _portal_site()
    f = PoliteFetcher(Settings(min_interval_seconds=0, max_retries=1, allow_private_hosts=True))
    settings = Settings(min_interval_seconds=0, allow_private_hosts=True)
    try:
        llm = FakeLlm([portal_recipe(site.base, article_signature="/portal/notice/{n}")])
        with pytest.raises(CrawlFailure) as e:
            discover(f, "127.0.0.1", site.base, "ETC", llm=llm, settings=settings)   # 레시피 거부 → 휴리스틱 불합격 그대로
        assert e.value.code == Code.QUALITY_FAILED and len(llm.calls) == 1
    finally:
        f.close()
        site.close()


def test_llm_recipe_is_requested_again_after_invalid_selector():
    site = _portal_site()
    f = PoliteFetcher(Settings(min_interval_seconds=0, max_retries=1, allow_private_hosts=True))
    settings = Settings(min_interval_seconds=0, allow_private_hosts=True)
    try:
        bad = portal_recipe(site.base, selectors={"title": "div[[", "date": ".reg", "body": None, "attachment": None})
        llm = FakeLlm(["```json\n" + bad + "\n```", portal_recipe(site.base)])
        result, _ = discover(f, "127.0.0.1", site.base, "ETC", llm=llm, settings=settings)
        assert result.recipe_source == "llm" and len(llm.calls) == 2
        assert "selectors.title" in llm.calls[1][1]
    finally:
        f.close()
        site.close()


def test_llm_outage_falls_back_to_heuristics():
    from fakesite import Site, news_site_routes
    site = Site(news_site_routes())
    f = PoliteFetcher(Settings(min_interval_seconds=0, max_retries=1, allow_private_hosts=True))
    settings = Settings(min_interval_seconds=0, allow_private_hosts=True)
    try:
        llm = FakeLlm(error=LlmUnavailable("connection refused"))
        result, detail = discover(f, "127.0.0.1", site.base, "NEWS", llm=llm, settings=settings)
        assert result.recipe_source == "heuristic" and result.method == "STATIC" and result.selectors == {}
        assert result.to_config()["recipe_source"] == "heuristic" and "LLM" not in detail
        assert len(llm.calls) == 1
        # NoLlm은 호출조차 하지 않는다
        result, _ = discover(f, "127.0.0.1", site.base, "NEWS", llm=NoLlm(), settings=settings)
        assert result.recipe_source == "heuristic"
    finally:
        f.close()
        site.close()


def test_make_llm_from_settings():
    assert isinstance(make_llm(Settings()), NoLlm)
    assert isinstance(make_llm(Settings(llm_provider="none")), NoLlm)
    with pytest.raises(RuntimeError):
        make_llm(Settings(llm_provider="openai", llm_base_url=""))        # 주소 없음
    with pytest.raises(RuntimeError):
        make_llm(Settings(llm_provider="bard"))
    llm = make_llm(Settings(llm_provider="openai", llm_base_url="http://localhost:11434/v1/", llm_model="llama3"))
    assert isinstance(llm, OpenAiCompatLlm) and llm.base_url == "http://localhost:11434/v1"


def _anthropic_response(text: str, stop_reason: str = "end_turn"):
    return SimpleNamespace(stop_reason=stop_reason, stop_details=SimpleNamespace(category="cyber"),
                           content=[SimpleNamespace(type="text", text=text)],
                           usage=SimpleNamespace(input_tokens=30, output_tokens=7))


def test_anthropic_llm_uses_json_schema_output_and_fallbacks():
    client = MagicMock()
    client.beta.messages.create.return_value = _anthropic_response('{"ok": true}')
    llm = AnthropicLlm(client=client)
    assert llm.model == "claude-opus-5-5"
    assert llm.complete("sys", "user", RECIPE_SCHEMA) == '{"ok": true}'
    kw = client.beta.messages.create.call_args.kwargs
    assert kw["model"] == "claude-opus-5-5" and kw["system"] == "sys" and kw["max_tokens"] == 16000
    assert kw["messages"] == [{"role": "user", "content": "user"}]
    assert kw["output_config"]["format"]["type"] == "json_schema" and kw["output_config"]["format"]["schema"] is RECIPE_SCHEMA
    assert kw["fallbacks"] == "default" and kw["betas"] == [FALLBACK_BETA]
    assert llm.last_usage == {"input_tokens": 30, "output_tokens": 7}
    client.messages.create.assert_not_called()

    # SENSING_LLM_FALLBACKS=false: 일반 Messages API, fallbacks·betas 없음
    plain = MagicMock()
    plain.messages.create.return_value = _anthropic_response("{}")
    AnthropicLlm("claude-sonnet-5-5", fallbacks=False, client=plain).complete("s", "u", {"type": "object"})
    kw = plain.messages.create.call_args.kwargs
    assert kw["model"] == "claude-sonnet-5-5" and "fallbacks" not in kw and "betas" not in kw
    assert kw["output_config"]["format"]["type"] == "json_schema"
    plain.beta.messages.create.assert_not_called()

    refusing = MagicMock()
    refusing.beta.messages.create.return_value = _anthropic_response("", stop_reason="refusal")
    with pytest.raises(LlmUnavailable):
        AnthropicLlm(client=refusing).complete("s", "u", {"type": "object"})


def test_openai_compat_llm_downgrades_response_format_on_400():
    bodies: list[dict] = []
    headers: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "http://vllm:8000/v1/chat/completions"
        body = json.loads(request.content)
        bodies.append(body)
        headers.append(dict(request.headers))
        fmt = body.get("response_format", {}).get("type")
        if fmt in ("json_schema", "json_object"):
            return httpx.Response(400, json={"error": {"message": f"response_format {fmt} is not supported"}})
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": '{"x": 1}'}}],
                                         "usage": {"prompt_tokens": 12, "completion_tokens": 3}})

    llm = OpenAiCompatLlm("http://vllm:8000/v1/", "qwen", "secret-key", transport=httpx.MockTransport(handler))
    assert llm.complete("sys", "user", RECIPE_SCHEMA) == '{"x": 1}'
    assert [b.get("response_format", {}).get("type") for b in bodies] == ["json_schema", "json_object", None]
    first = bodies[0]
    assert first["model"] == "qwen" and first["temperature"] == 0
    assert first["messages"] == [{"role": "system", "content": "sys"}, {"role": "user", "content": "user"}]
    assert first["response_format"]["json_schema"] == {"name": "recipe", "schema": RECIPE_SCHEMA, "strict": True}
    assert headers[0]["authorization"] == "Bearer secret-key"
    assert llm.last_usage == {"input_tokens": 12, "output_tokens": 3}
    # 한 번 내려간 단계를 기억해 다음 요청은 바로 통한다
    bodies.clear()
    llm.complete("sys", "user2", RECIPE_SCHEMA)
    assert len(bodies) == 1 and "response_format" not in bodies[0]

    # 키가 없으면 Authorization 헤더를 보내지 않는다 (로컬 Ollama·vLLM)
    seen: list[httpx.Request] = []

    def ok(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})
    OpenAiCompatLlm("http://localhost:11434/v1", "llama3", "", transport=httpx.MockTransport(ok)).complete("s", "u", {})
    assert "authorization" not in seen[0].headers and json.loads(seen[0].content)["response_format"]["type"] == "json_schema"

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")
    with pytest.raises(LlmUnavailable):
        OpenAiCompatLlm("http://localhost:11434/v1", "llama3", transport=httpx.MockTransport(down)).complete("s", "u", {})

    def server_error(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")
    with pytest.raises(LlmUnavailable):
        OpenAiCompatLlm("http://localhost:11434/v1", "llama3", transport=httpx.MockTransport(server_error)).complete("s", "u", {})


def test_extract_uses_recipe_selectors_and_date_format():
    html = ('<html><head><title>테스트포털</title><meta property="og:title" content="테스트포털"></head><body>'
            '<p class="headline-area"><strong>중소기업 지원 계획 발표</strong></p><p class="reg">2026.10.14</p>'
            '<div class="cont-area"><p>' + "본문 " * 100 + "</p></div></body></html>")
    plain = extract_article(html, "https://p.go.kr/portal/bbs/view.do?bbsId=B01&nttId=1")
    assert plain.title == "테스트포털" and plain.published_at is None            # 셀렉터 없이는 사이트 이름·날짜 없음
    art = extract_article(html, "https://p.go.kr/portal/bbs/view.do?bbsId=B01&nttId=1",
                          {"title": ".headline-area strong", "date": ".reg", "body": ".cont-area"}, date_format="%Y.%m.%d")
    assert art.title == "중소기업 지원 계획 발표"
    assert art.published_at == datetime(2026, 10, 13, 15, 0, tzinfo=timezone.utc) and art.body_length >= 200
    # 형식이 맞지 않으면 공통 해석으로 넘어간다
    assert extract_article(html, "https://p.go.kr/x", {"date": ".reg"}, date_format="%d/%m/%Y").published_at is not None


def _crawl_runner(tmp_path, handler):
    """가짜 사이트(MockTransport)와 MagicMock 저장소로 JobRunner._crawl만 돌린다."""
    from sensing_worker.db import Job
    from sensing_worker.jobs import JobRunner
    s = Settings(min_interval_seconds=0, max_retries=1, allow_private_hosts=True, evidence_dir=str(tmp_path))
    store = MagicMock()
    store.found_baseline.return_value = None
    store.known_urls.return_value = set()
    store.upsert_document.return_value = (1, "NEW")
    runner = JobRunner(store, PoliteFetcher(s, transport=httpx.MockTransport(handler)), s, llm=NoLlm())
    job = Job(id=7, source_id=3, job_type="CRAWL", trigger="SCHEDULE", config_version_id=None, attempt=1)
    return runner, store, job


def _news_handler(statuses: dict[str, int]):
    items = "".join(f"<li><a href='/news/articleView.html?idxno={i}'>기사 {i}</a></li>" for i in range(1, 4))
    article = ("<html><head><title>기사</title><meta property='og:title' content='기사 제목'>"
               "<meta property='article:published_time' content='2026-10-01T09:00:00+09:00'></head>"
               "<body><article><p>본문</p></article></body></html>")

    def handler(request):
        key = request.url.path + ("?" + request.url.query.decode() if request.url.query else "")
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if key in statuses:
            return httpx.Response(statuses[key], text="<html><body>oops</body></html>")
        if request.url.path == "/news/articleView.html":
            return httpx.Response(200, text=article)
        return httpx.Response(200, text=f"<html><body><ul>{items}</ul></body></html>")
    return handler


NEWS_CONFIG = {"method": "STATIC", "store_scope": "TITLE_LINK",
               "hubs": ["https://site.example/", "https://site.example/news/articleList.html?sc=A",
                        "https://site.example/news/articleList.html?sc=B"],
               "article_pattern": pattern_from_signature("/news/articleView.html?idxno={n}")}
SOURCE = {"domain": "site.example", "interval_minutes": 60, "active_config_version_id": None}


def test_crawl_skips_hub_that_fails_without_blocking(tmp_path):
    runner, store, job = _crawl_runner(tmp_path, _news_handler({"/news/articleList.html?sc=A": 404}))
    stats = runner._crawl(job, SOURCE, NEWS_CONFIG)
    assert stats.found == 3 and stats.new == 3 and stats.errors == 1
    store.record_error.assert_called_once()
    failure = store.record_error.call_args.args[1]
    assert failure.code == Code.HTTP_4XX and failure.stage == "LIST" and failure.url.endswith("?sc=A")


def test_crawl_fails_with_last_error_when_no_hub_opens(tmp_path):
    runner, store, job = _crawl_runner(tmp_path, _news_handler({
        "/": 404, "/news/articleList.html?sc=A": 404, "/news/articleList.html?sc=B": 503}))
    with pytest.raises(CrawlFailure) as e:
        runner._crawl(job, SOURCE, NEWS_CONFIG)
    assert e.value.code == Code.HTTP_5XX and e.value.stage == "LIST"       # 마지막 오류가 실패 사유
    assert store.record_error.call_count == 2                              # 앞의 두 허브 오류만 여기서 기록 (마지막은 run()이)
    assert [c.args[1].code for c in store.record_error.call_args_list] == [Code.HTTP_4XX, Code.HTTP_4XX]


def test_crawl_stops_at_blocking_hub(tmp_path):
    runner, store, job = _crawl_runner(tmp_path, _news_handler({
        "/news/articleList.html?sc=A": 404, "/news/articleList.html?sc=B": 403}))
    with pytest.raises(CrawlFailure) as e:
        runner._crawl(job, SOURCE, NEWS_CONFIG)
    assert e.value.code == Code.BOT_CHALLENGE and e.value.blocking
    assert store.record_error.call_count == 1                              # 앞서 건너뛴 허브의 404는 남긴다
    assert store.record_error.call_args.args[1].code == Code.HTTP_4XX
