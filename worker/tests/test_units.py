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
from sensing_worker.discovery import (Discoverer, canonical_article_url, matches, merge_signatures, parse_sitemap,
                                      pattern_from_signature, signature, sitemap_urls)
from sensing_worker.evidence import EvidenceStore
from sensing_worker.extract import Article, extract_article, links_with_dates
from sensing_worker.jobs import discover
from sensing_worker.fetcher import Page, PoliteFetcher, client_redirect_target, compat_ssl_context
from sensing_worker.llm import (FALLBACK_BETA, AnthropicLlm, LlmUnavailable, NoLlm, OpenAiCompatLlm, extract_json,
                                generate_recipe, make_llm, validate_schema)
from sensing_worker.db import preferred_job_type
from sensing_worker.policy import defaults_for, guess_site_type, next_interval, passes_quality
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
        ok = evaluate_site(news.base + "/news/articleView.html?idxno=101", settings,
                           sample_url=news.base + "/news/articleView.html?idxno=106")
        assert ok.outcome == "OK" and ok.method == "STATIC" and ok.site_type == "NEWS" and ok.requests > 0
        assert ok.recipe_source == "heuristic"                      # LLM 설정이 없으면 휴리스틱
        assert ok.url == news.base                                   # 기사 주소를 넣어도 사이트 단위로 본다
        assert ok.sample_match is True                               # 고른 패턴이 현업 샘플 기사를 잡는다
        miss = evaluate_site(news.base, settings, sample_url=news.base + "/news/photoView.html?idxno=1")
        assert miss.outcome == "OK" and miss.sample_match is False
        bad = evaluate_site(blocked.base, settings, sample_url=blocked.base + "/a/1")
        assert bad.outcome == "BLOCKED" and bad.code == "BOT_CHALLENGE" and bad.sample_match is None
        text = summarize([ok, miss, bad], target=3)
        assert "자동 등록 성공 2/3곳 (목표 3곳: 미달)" in text and "샘플 기사 적중 1/3곳" in text
        assert [line.split()[5] for line in text.splitlines()[1:4]] == ["O", "X", "-"]
    finally:
        news.close()
        blocked.close()
    assert read_sites(["a.com", "a.com", "b.com"]) == ["a.com", "b.com"]


def test_eval_site_list_carries_sample_article_urls(tmp_path):
    from sensing_worker.evaluate import read_site_entries, read_sites

    listing = tmp_path / "sites.txt"
    listing.write_text("# 표본\nhttps://a.com   # 샘플 없음\nhttps://b.com https://b.com/news/economy/1  # 샘플 있음\n"
                       "https://a.com https://a.com/view/2\n", encoding="utf-8")
    assert read_site_entries([str(listing), "c.com"]) == [("https://a.com", "https://a.com/view/2"),
                                                          ("https://b.com", "https://b.com/news/economy/1"), ("c.com", None)]
    assert read_sites([str(listing)]) == ["https://a.com", "https://b.com"]


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


def test_signature_generalizes_slugs_and_patterns_accept_wildcards():
    assert signature("https://www.eetimes.com/samsung-lays-out-ai-memory-roadmap/") == "/{slug}/"
    assert signature("https://www.tomshardware.com/pc-components/dram/samsung-debuts-three-memory-technologies") \
        == "/pc-components/dram/{slug}"
    assert signature("https://a.com/news/hbm4-2026-roadmap-1234") == "/news/{slug}"   # 숫자가 섞여도 단어 3개면 슬러그
    assert signature("https://a.com/tech-industry/semiconductors") == "/tech-industry/semiconductors"  # 두 단어는 고정 조각
    assert signature("https://a.com/Samsung-Lays-Out-Roadmap") == "/Samsung-Lays-Out-Roadmap"          # 소문자만 슬러그
    assert signature("https://a.com/2026-10-16/story") == "/{id}/story"                                 # 날짜는 식별자

    slug = pattern_from_signature("/{slug}/")
    assert slug["path"] == "^/[a-z0-9]+(?:-[a-z0-9]+){2,}/$"
    assert matches("https://www.eetimes.com/samsung-lays-out-ai-memory-roadmap/", slug)
    assert not matches("https://www.eetimes.com/about-us/", slug) and not matches("https://www.eetimes.com/news/", slug)
    assert canonical_article_url("https://www.eetimes.com/samsung-lays-out-ai-memory-roadmap/?utm_source=x", slug) \
        == "https://www.eetimes.com/samsung-lays-out-ai-memory-roadmap/"

    wild = pattern_from_signature("/news/{*}/{n}")
    assert wild["path"] == r"^/news/[^/]+/\d+$"
    assert matches("https://www.mk.co.kr/news/economy/12175940", wild)
    assert matches("https://www.mk.co.kr/news/society/12175941?utm_source=x", wild)
    assert not matches("https://www.mk.co.kr/news/economy/sub/12175940", wild)
    assert not matches("https://www.mk.co.kr/news/12175940", wild)
    assert canonical_article_url("https://www.mk.co.kr/news/economy/12175940?utm_source=x", wild) \
        == "https://www.mk.co.kr/news/economy/12175940"
    two = pattern_from_signature("/{*}/{*}/{slug}")
    assert matches("https://www.tomshardware.com/pc-components/dram/samsung-debuts-three-memory-technologies", two)
    assert not matches("https://www.tomshardware.com/news/samsung-debuts-three-memory-technologies", two)


def test_ranked_patterns_merge_signatures_that_differ_only_in_section():
    econ = [f"https://m.com/news/economy/{i}" for i in range(3)]
    soc = [f"https://m.com/news/society/{i}" for i in range(3)]
    lists = [f"https://m.com/news/list/{i}" for i in range(5)]           # 목록 서명은 후보도, 병합 대상도 아니다
    ranked = [p["signature"] for p in Discoverer._ranked_patterns(econ + soc + lists, 5)]
    assert ranked == ["/news/{*}/{n}", "/news/society/{n}", "/news/economy/{n}"]  # 병합 서명이 앞, 원래 서명도 남는다

    # 이어진 두 조각이 다르면 둘 다 {*}. 원래 서명은 3건 미만이라 떨어지고 병합 서명만 남는다
    toms = [f"https://t.com/tech-industry/semiconductors/samsung-foo-bar-{i}" for i in range(2)] \
        + [f"https://t.com/pc-components/dram/samsung-foo-bar-{i}" for i in range(2)]
    assert [p["signature"] for p in Discoverer._ranked_patterns(toms, 5)] == ["/{*}/{*}/{slug}"]

    # 식별자 자리가 다르거나 조각 수가 다르면 합치지 않는다. /{*} 같은 서명은 생기지 않는다
    assert merge_signatures({"/{n}": 3, "/{id}": 3}) == {}
    assert merge_signatures({"/a/{n}": 3, "/b/{id}": 3}) == {}
    assert merge_signatures({"/a/{n}": 3, "/a/b/{n}": 3}) == {}
    assert merge_signatures({"/news{n}.html": 3, "/view{n}.html": 3}) == {}
    assert merge_signatures({"/a/b/c/{n}": 3, "/x/y/z/{n}": 3}) == {}      # 세 조각이 다르면 너무 넓다
    # 섹션 이름은 식별자보다 앞에 온다: 번호 뒤에 페이지 이름이 오는 메뉴 주소(산업부 /kor/{n}/headquarters)나
    # 쿼리에만 식별자가 있는 서명은 합치지 않는다
    assert merge_signatures({"/kor/{n}/headquarters": 28, "/kor/{n}/sitemap": 18, "/kor/{n}/inderstry": 22}) == {}
    assert merge_signatures({"/a/v.do?id={n}": 2, "/b/v.do?id={n}": 2}) == {}
    # 쿼리가 있으면 같아야 하고, 병합 횟수는 덮는 서명들의 합이다
    assert merge_signatures({"/a/x/{n}?p=*": 2, "/b/x/{n}?p=*": 2, "/c/x/{n}?p=*&x=*": 2}) == {"/{*}/x/{n}?p=*": 4}

    # 합친 서명은 원래 서명의 자리를 뺏지 않는다: limit는 원래 서명에만 적용돼 기사 서명이 밀려나지 않는다
    menus = [f"https://m.com/kor/{i}/{name}" for name in ("economy", "society", "culture") for i in range(3)]
    posts = [f"https://m.com/kor/article/ATCL{i:08d}/{i}/view" for i in range(3)]
    ranked = [p["signature"] for p in Discoverer._ranked_patterns(menus + posts, 3)]
    assert "/kor/article/{id}/{n}/view" in ranked and "/kor/{n}/{*}" not in ranked
    ranked = [p["signature"] for p in Discoverer._ranked_patterns(
        [f"https://m.com/{sec}/{i}" for sec in ("a", "b", "c", "d") for i in range(5)], 2)]
    assert ranked == ["/{*}/{n}", "/d/{n}", "/c/{n}"]                      # 원래 서명 2개 + 병합 서명


def test_discover_merges_section_paths_into_one_article_pattern():
    from fakesite import Site, sectioned_news_routes
    site = Site(sectioned_news_routes())
    f = PoliteFetcher(Settings(min_interval_seconds=0, max_retries=1, allow_private_hosts=True))
    try:
        result, detail = discover(f, "127.0.0.1", site.base, "NEWS")
        assert result.method == "STATIC" and result.article_pattern["signature"] == "/news/{*}/{n}"
        assert "5건 통과" in detail
        for sec in ("economy", "society"):
            assert matches(f"{site.base}/news/{sec}/9999", result.article_pattern)
        assert {u.split("/")[-2] for u in result.sample_urls} == {"economy", "society"}   # 두 섹션의 기사를 다 잡는다
    finally:
        f.close()
        site.close()


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
        "/news/articleList.html?sc=A": 404, "/news/articleList.html?sc=B": 429}))   # 429(요청 제한)는 차단 신호. 봇 확인 화면 없는 403은 이제 비차단
    with pytest.raises(CrawlFailure) as e:
        runner._crawl(job, SOURCE, NEWS_CONFIG)
    assert e.value.code == Code.RATE_LIMITED and e.value.blocking
    assert store.record_error.call_count == 1                              # 앞서 건너뛴 허브의 404는 남긴다
    assert store.record_error.call_args.args[1].code == Code.HTTP_4XX


# ---------------------------------------------------------------------- 공개 API 소스 (M2)

def _api_fetcher(handler) -> PoliteFetcher:
    s = Settings(min_interval_seconds=0, max_retries=1, allow_private_hosts=True)
    return PoliteFetcher(s, transport=httpx.MockTransport(handler))


def test_api_catalog_lookup():
    from sensing_worker.apis import catalog
    assert catalog.lookup("dart.fss.or.kr").id == "dart"
    assert catalog.lookup("WWW.dart.fss.or.kr").id == "dart"
    assert catalog.lookup("opendart.fss.or.kr").id == "dart"
    assert catalog.lookup("m.dart.fss.or.kr").id == "dart"
    assert catalog.lookup("fss.or.kr") is None                      # 금감원 홈은 공시 API 대상이 아니다
    assert catalog.lookup("news.example.com") is None and catalog.lookup("") is None
    assert catalog.get("dart").needs_key and "opendart.fss.or.kr" in catalog.get("dart").signup_url
    assert "API 키" in catalog.get("dart").key_required_message


def test_mask_secret_hides_value_and_key_params():
    from sensing_worker.apis import mask_secret
    assert mask_secret("https://x/api?crtfc_key=abc123&bgn_de=1", "abc123") == "https://x/api?crtfc_key=***&bgn_de=1"
    assert mask_secret("key abc123 leaked", "abc123") == "key *** leaked"
    assert mask_secret("serviceKey=zzz&x=1") == "serviceKey=***&x=1"      # 값을 몰라도 패턴으로 가린다
    assert mask_secret(None, "abc") is None and mask_secret("", "abc") == ""


def test_dart_connector_lists_disclosures_without_leaking_key():
    from fakedart import GOOD_KEY, dart_handler, disclosures
    from sensing_worker.apis.dart import KST, DartConnector
    handler = dart_handler(disclosures(5), per_page=2)                 # 3페이지로 나뉜다
    c = DartConnector(_api_fetcher(handler))
    c.verify(GOOD_KEY)
    assert c.default_config() == {"method": "API", "provider": "dart", "days": 1, "corp_codes": []}

    items = c.fetch(GOOD_KEY, c.default_config(), since=datetime.now(KST).date() - timedelta(days=1), limit=10)
    assert [i.url for i in items] == [f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo=2026101500000{i}" for i in range(1, 6)]
    assert items[0].title == "주요사항보고서(유상증자결정) 1" and items[0].author == "테스트전자"
    assert items[0].section == "테스트전자" and items[0].body is None and items[0].attachments == []
    assert items[0].published_at.tzinfo == KST and items[0].published_at.hour == 0
    assert len(handler.calls) == 1 + 3                                 # verify 1회 + 목록 3페이지
    assert all("crtfc_key=" + GOOD_KEY in u for u in handler.calls)    # 키는 요청에만 실린다

    assert c.fetch(GOOD_KEY, c.default_config(), since=datetime.now(KST).date(), limit=2) == items[:2]   # limit
    assert c.fetch(GOOD_KEY, {"days": 1, "corp_codes": ["00126380"]}, since=datetime.now(KST).date(), limit=9)[0] == items[0]
    assert "corp_code=00126380" in handler.calls[-1]


def test_dart_connector_classifies_key_errors_and_outages():
    from fakedart import GOOD_KEY, dart_handler
    from sensing_worker.apis.dart import DartConnector
    since = datetime.now(timezone.utc).date()

    with pytest.raises(CrawlFailure) as e:                             # 잘못된 키 → API_KEY_INVALID, 키 노출 없음
        DartConnector(_api_fetcher(dart_handler())).verify("wrong-secret-xyz")
    assert e.value.code == Code.API_KEY_INVALID and not e.value.retryable
    assert "010" in str(e.value) and "wrong-secret-xyz" not in str(e.value)
    assert "crtfc_key=***" in e.value.url and "wrong-secret-xyz" not in e.value.url

    with pytest.raises(CrawlFailure) as e:                             # 서버 장애 → API_ERROR(재시도), 증거에도 키 없음
        DartConnector(_api_fetcher(dart_handler(server_status=503))).fetch(GOOD_KEY, {}, since, 10)
    assert e.value.code == Code.API_ERROR and e.value.retryable and e.value.http_status == 503
    assert GOOD_KEY not in (e.value.url + str(e.value) + (e.value.evidence or ""))

    with pytest.raises(CrawlFailure) as e:                             # 요청 한도 초과(020)는 키 오류가 아니다
        DartConnector(_api_fetcher(dart_handler(api_status="020"))).verify(GOOD_KEY)
    assert e.value.code == Code.API_ERROR and e.value.retryable

    assert DartConnector(_api_fetcher(dart_handler(items=[]))).fetch(GOOD_KEY, {}, since, 10) == []   # 013 = 0건

    def down(request):                                                 # 연결 실패도 URL을 가린 채 전달된다
        raise httpx.ConnectError("boom", request=request)
    with pytest.raises(CrawlFailure) as e:
        DartConnector(_api_fetcher(down)).verify(GOOD_KEY)
    assert e.value.code == Code.TIMEOUT and GOOD_KEY not in e.value.url and "crtfc_key=***" in e.value.url


def test_api_fetch_keeps_private_host_guard_but_skips_robots():
    from sensing_worker.apis.dart import DartConnector
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"status": "000", "list": [], "total_page": 1})
    DartConnector(_api_fetcher(handler)).verify("k")
    assert calls == ["/api/list.json"]                                 # robots.txt를 읽지 않는다

    s = Settings(min_interval_seconds=0, max_retries=1, allow_private_hosts=False)
    with pytest.raises(CrawlFailure) as e:
        PoliteFetcher(s, transport=httpx.MockTransport(handler)).get_api("http://127.0.0.1/api")
    assert e.value.code == Code.INTERNAL_ADDRESS


# ---------------------------------------------------------------------- 키워드 감시 (M2, 네이버 뉴스 검색)

def test_naver_news_is_keyword_based_provider():
    from sensing_worker.apis import catalog
    naver = catalog.lookup("news.naver.com")
    assert naver.id == "naver_news" and naver.keyword_based and naver.needs_key
    assert catalog.lookup("n.news.naver.com").id == "naver_news"
    assert catalog.lookup("openapi.naver.com").id == "naver_news"
    assert catalog.lookup("naver.com") is None and catalog.lookup("blog.naver.com") is None
    assert catalog.get("dart").keyword_based is False                 # 기본값
    assert "client_id:client_secret" in naver.key_required_message and "developers.naver.com" in naver.signup_url


def test_naver_connector_fetches_each_keyword_and_keeps_title_link_only():
    from fakenaver import GOOD_ID, GOOD_KEY, GOOD_SECRET, KST, article, articles, naver_handler
    from sensing_worker.apis.naver_news import NaverNewsConnector
    shared = article("경쟁사", 9, originallink="https://press.example.com/shared")   # 두 검색어에 다 걸리는 기사
    handler = naver_handler({
        "삼성전자": [article("삼성전자", 1, pub=datetime(2026, 10, 16, 9, 30, tzinfo=KST)),
                 article("삼성전자", 2, title="<b>삼성전자</b> 채용 공고"),           # exclude
                 article("삼성전자", 3, originallink="", title="원문 링크 없음"),       # originallink 비면 link
                 dict(shared, title="<b>삼성전자</b>·경쟁사 공동 보도")],
        "경쟁사": [shared] + articles("경쟁사", 2),
    })
    c = NaverNewsConnector(_api_fetcher(handler))
    c.verify(GOOD_KEY)
    assert handler.calls == [("테스트", 1, 1)]
    assert c.default_config() == {"method": "API", "provider": "naver_news", "keywords": [], "exclude": []}

    config = {"method": "API", "provider": "naver_news", "keywords": ["삼성전자", " 경쟁사 ", "삼성전자"], "exclude": ["채용"]}
    items = c.fetch(GOOD_KEY, config, since=datetime(2026, 1, 1).date(), limit=50)
    assert [i.url for i in items] == [
        "https://press.example.com/삼성전자/1",
        "https://n.news.naver.com/mnews/article/001/삼성전자3",                      # originallink가 비어 link
        "https://press.example.com/shared",
        "https://press.example.com/경쟁사/1", "https://press.example.com/경쟁사/2"]
    assert [i.keyword for i in items] == ["삼성전자", "삼성전자", "삼성전자", "경쟁사", "경쟁사"]   # 공유 기사는 먼저 걸린 검색어
    first = items[0]
    assert first.title == '삼성전자, "신제품" 공개 1'                                   # <b> 제거, &quot; 복원
    assert first.published_at == datetime(2026, 10, 16, 0, 30, tzinfo=timezone.utc)   # +0900 → UTC
    assert first.body is None and first.author is None and first.section is None and first.attachments == []
    assert all("채용" not in (i.title or "") for i in items)
    assert handler.calls[1:] == [("삼성전자", 100, 1), ("경쟁사", 100, 1)]            # 검색어마다 한 페이지씩 (100건 미만)
    assert handler.headers and all(h == (GOOD_ID, GOOD_SECRET) for h in handler.headers)   # 키는 헤더로만
    assert c.calls == 3

    with pytest.raises(CrawlFailure) as e:                             # 키워드가 없으면 수집하지 않는다
        c.fetch(GOOD_KEY, {"keywords": [], "exclude": []}, since=datetime(2026, 1, 1).date(), limit=50)
    assert e.value.code == Code.KEYWORDS_REQUIRED


def test_naver_connector_pages_by_date_until_older_than_since():
    from fakenaver import GOOD_KEY, KST, article, naver_handler
    from sensing_worker.apis.naver_news import MAX_PAGES, NaverNewsConnector
    rows = [article("k", 1, pub=datetime(2026, 10, 16, 9, 0, tzinfo=KST)),    # 10-16 00:00Z
            article("k", 2, pub=datetime(2026, 10, 15, 10, 0, tzinfo=KST)),   # 10-15 01:00Z
            article("k", 3, pub=datetime(2026, 10, 15, 8, 0, tzinfo=KST)),    # 10-14 23:00Z → since(10-15)보다 오래됨
            article("k", 4, pub=datetime(2026, 10, 14, 8, 0, tzinfo=KST))]
    handler = naver_handler({"k": rows}, per_page=1)
    c = NaverNewsConnector(_api_fetcher(handler))
    items = c.fetch(GOOD_KEY, {"keywords": ["k"]}, since=datetime(2026, 10, 15).date(), limit=50)
    assert [i.url for i in items] == ["https://press.example.com/k/1", "https://press.example.com/k/2"]
    assert handler.calls == [("k", 100, 1), ("k", 100, 2), ("k", 100, 3)]      # 오래된 기사를 본 페이지에서 멈춘다

    handler = naver_handler({"k": [article("k", i) for i in range(1, 10)]}, per_page=1)   # 전부 최신: 최대 3페이지
    items = NaverNewsConnector(_api_fetcher(handler)).fetch(GOOD_KEY, {"keywords": ["k"]}, since=datetime(2026, 1, 1).date(), limit=50)
    assert len(items) == MAX_PAGES == 3 and [s for _, _, s in handler.calls] == [1, 2, 3]

    handler = naver_handler({"k": [article("k", i) for i in range(1, 10)]})
    items = NaverNewsConnector(_api_fetcher(handler)).fetch(GOOD_KEY, {"keywords": ["k"]}, since=datetime(2026, 1, 1).date(), limit=2)
    assert len(items) == 2 and len(handler.calls) == 1                           # limit


def test_naver_connector_classifies_key_errors_and_rate_limit_without_leaking_secret():
    from fakenaver import GOOD_ID, GOOD_KEY, GOOD_SECRET, naver_handler
    from sensing_worker.apis.naver_news import NaverNewsConnector
    since = datetime.now(timezone.utc).date()

    def leaked(e: CrawlFailure) -> bool:
        blob = str(e) + (e.url or "") + (e.evidence or "")
        return GOOD_SECRET in blob or GOOD_ID in blob or "wrong-secret" in blob

    with pytest.raises(CrawlFailure) as e:                             # 틀린 시크릿 → 401/024 → API_KEY_INVALID
        NaverNewsConnector(_api_fetcher(naver_handler())).verify(f"{GOOD_ID}:wrong-secret")
    assert e.value.code == Code.API_KEY_INVALID and not e.value.retryable and e.value.http_status == 401
    assert "024" in str(e.value) and not leaked(e.value)

    handler = naver_handler()
    with pytest.raises(CrawlFailure) as e:                             # 형식이 아니면 호출 없이 API_KEY_INVALID
        NaverNewsConnector(_api_fetcher(handler)).verify("no-colon-here")
    assert e.value.code == Code.API_KEY_INVALID and "client_id:client_secret" in str(e.value) and handler.calls == []

    with pytest.raises(CrawlFailure) as e:                             # 429 한도 초과 → API_ERROR(재시도)
        NaverNewsConnector(_api_fetcher(naver_handler(http_status=429, error_code="010"))).verify(GOOD_KEY)
    assert e.value.code == Code.API_ERROR and e.value.retryable and e.value.http_status == 429
    assert "한도" in str(e.value) and not leaked(e.value)

    with pytest.raises(CrawlFailure) as e:                             # 5xx → API_ERROR(재시도)
        NaverNewsConnector(_api_fetcher(naver_handler(http_status=500))).fetch(GOOD_KEY, {"keywords": ["k"]}, since, 10)
    assert e.value.code == Code.API_ERROR and e.value.retryable and e.value.http_status == 500

    with pytest.raises(CrawlFailure) as e:                             # 잘못된 요청(400 SE01)은 재시도하지 않는다
        NaverNewsConnector(_api_fetcher(naver_handler(http_status=400, error_code="SE01"))).verify(GOOD_KEY)
    assert e.value.code == Code.API_ERROR and not e.value.retryable and "SE01" in str(e.value)

    def down(request):
        raise httpx.ConnectError("boom", request=request)
    with pytest.raises(CrawlFailure) as e:
        NaverNewsConnector(_api_fetcher(down)).verify(GOOD_KEY)
    assert e.value.code == Code.TIMEOUT and not leaked(e.value)

    from sensing_worker.apis.naver_news import clean_title, parse_pub_date
    assert clean_title("  <b>삼성</b>&amp;<b>LG</b> &lt;협력&gt;  ") == "삼성&LG <협력>" and clean_title("") is None
    assert parse_pub_date("Thu, 16 Oct 2026 09:30:00 +0900") == datetime(2026, 10, 16, 0, 30, tzinfo=timezone.utc)
    assert parse_pub_date("not a date") is None and parse_pub_date(None) is None


def test_discovery_eval_marks_api_domains_without_network():
    from sensing_worker.evaluate import evaluate_site, summarize
    settings = Settings(min_interval_seconds=0, max_retries=1)
    r = evaluate_site("https://dart.fss.or.kr/dsaf001/main.do?rcpNo=1", settings)
    assert r.outcome == "API" and r.method == "API" and r.requests == 0 and r.code == "API_KEY_REQUIRED"
    assert "opendart.fss.or.kr" in r.message
    assert "API 1" in summarize([r])


def _api_runner(tmp_path, handler, *, secret_key="unit-secret", credential="good-key-0123456789abcdef"):
    """MagicMock 저장소로 ONBOARD/CRAWL 흐름만 돌린다 (DB 흐름은 test_flow_db.py)."""
    from sensing_worker.db import Job
    from sensing_worker.jobs import JobRunner
    s = Settings(min_interval_seconds=0, max_retries=1, allow_private_hosts=True, evidence_dir=str(tmp_path),
                 secret_key=secret_key)
    store = MagicMock()
    store.load_source.return_value = {"id": 3, "site_id": 5, "domain": "dart.fss.or.kr",
                                      "base_url": "https://dart.fss.or.kr", "config": None, "config_version": None,
                                      "status": "PENDING", "interval_minutes": 60, "active_config_version_id": None}
    store.load_credential.return_value = credential
    store.found_baseline.return_value = None
    store.activate_new_config.return_value = (11, 1)
    store.upsert_document.return_value = (1, "NEW")
    runner = JobRunner(store, PoliteFetcher(s, transport=httpx.MockTransport(handler)), s, llm=NoLlm())
    job = Job(id=7, source_id=3, job_type="ONBOARD", trigger="REGISTRATION", config_version_id=None, attempt=1)
    return runner, store, job


def test_onboard_api_source_waits_for_key_without_crawling(tmp_path):
    from fakedart import dart_handler
    handler = dart_handler()
    runner, store, job = _api_runner(tmp_path, handler, credential=None)
    assert runner.run(job) == "FAILED"
    assert handler.calls == []                                                     # 네트워크 호출 없음
    store.update_site.assert_called_once_with(5, site_type="PUBLIC", risk_flags={"api_provider": "dart"})
    store.set_source_status.assert_called_once_with(3, "REVIEW", "API_KEY_REQUIRED")
    reg = store.update_registration.call_args.kwargs
    assert reg["status"] == "REVIEW" and "DART 공시는 크롤링 대신" in reg["message"] and "opendart" in reg["message"]
    store.load_credential.assert_called_once_with("dart", "unit-secret")
    store.activate_new_config.assert_not_called()

    runner, store, job = _api_runner(tmp_path, handler, secret_key="")           # 대칭키 미설정도 같은 경로
    assert runner.run(job) == "FAILED"
    store.load_credential.assert_not_called()
    assert "SENSING_SECRET_KEY" in store.update_registration.call_args.kwargs["message"]
    assert store.record_error.call_args.args[1].code == Code.API_KEY_REQUIRED


def test_onboard_api_source_with_key_verifies_and_collects(tmp_path):
    from fakedart import dart_handler
    handler = dart_handler()
    runner, store, job = _api_runner(tmp_path, handler)
    assert runner.run(job) == "SUCCESS"
    store.mark_credential.assert_called_once_with("dart", "VALID", None)
    config, interval = store.activate_new_config.call_args.args[1], store.activate_new_config.call_args.args[3]
    assert config["method"] == "API" and config["provider"] == "dart" and interval == 360
    assert store.upsert_document.call_count == 3
    assert store.upsert_document.call_args.kwargs["url"].startswith("https://dart.fss.or.kr/dsaf001/main.do?rcpNo=")
    assert store.finish_job.call_args.args[:4] == (7, "SUCCESS", 3, 3)
    assert store.update_registration.call_args.kwargs["status"] == "COMPLETED"
    assert "3건" in store.update_registration.call_args.kwargs["message"]

    runner, store, job = _api_runner(tmp_path, handler, credential="stale-key")   # 틀린 키
    assert runner.run(job) == "FAILED"
    assert store.mark_credential.call_args.args[:2] == ("dart", "INVALID")
    assert "stale-key" not in store.mark_credential.call_args.args[2]
    store.set_source_status.assert_called_once_with(3, "REVIEW", "API_KEY_INVALID")


def test_periodic_api_crawl_uses_connector_and_sends_lost_key_to_review(tmp_path):
    from fakedart import dart_handler
    from sensing_worker.db import Job
    handler = dart_handler()
    runner, store, _ = _api_runner(tmp_path, handler)
    store.load_source.return_value.update({"status": "ACTIVE", "interval_minutes": 360, "active_config_version_id": 11,
                                           "config": {"method": "API", "provider": "dart", "days": 1, "corp_codes": []}})
    store.mark_anomaly.return_value = 0
    job = Job(id=8, source_id=3, job_type="CRAWL", trigger="SCHEDULE", config_version_id=11, attempt=1)
    assert runner.run(job) == "SUCCESS"
    assert store.upsert_document.call_count == 3 and store.finish_job.call_args.args[2] == 3   # found_count
    store.set_source_status.assert_not_called()

    store.load_credential.return_value = None                                    # 키가 지워졌다
    assert runner.run(job) == "FAILED"
    store.set_source_status.assert_called_once_with(3, "REVIEW", "API_KEY_REQUIRED")


# ---------------------------------------------------------------------- 보강 (요약·분류)

def _doc(i: int, **kw):
    from sensing_worker.enrich import PendingDoc
    base = dict(id=i, version=1, url=f"https://a.go.kr/view/{i}", title=f"제목 {i}", body="본문 " * 50,
                domain="a.go.kr", section="보도자료", site_type="PUBLIC")
    base.update(kw)
    return PendingDoc(**base)


def test_enrich_prompt_numbers_documents_and_hides_press_bodies():
    from sensing_worker.enrich import SYSTEM_PROMPT, TOPICS, build_batch_prompt
    docs = [_doc(1, body="가나다 " * 3000),                                                    # 본문 4,000자 넘김
            _doc(2, site_type="NEWS", body="언론사 본문이 있어도 보여 주지 않는다", domain="news.example.com",
                 watches=[("자사 보도", "메타넷"), (None, None)]),
            _doc(3, body=None, section=None)]                                                   # 본문 없음 → 제목만
    prompt = build_batch_prompt(docs, body_chars=4000)
    assert "문서 3건" in prompt
    assert "[문서 1]\n출처: 도메인 a.go.kr / 섹션 보도자료\n제목: 제목 1\n본문: 가나다" in prompt
    assert "...(잘림)" in prompt and prompt.count("가나다") < 1500
    assert "[문서 2]\n출처: 도메인 news.example.com / 섹션 보도자료 / 언론사 / 감시 '자사 보도' 검색어 '메타넷' / 감시\n" in prompt
    assert "언론사 본문이 있어도" not in prompt and prompt.count("본문: (없음 — 언론 기사, 제목만 저장)") == 2
    assert "[문서 3]\n출처: 도메인 a.go.kr\n제목: 제목 3\n" in prompt
    assert all(t in SYSTEM_PROMPT for t in TOPICS)


def test_enrich_parse_matches_idx_drops_unknown_topics_and_caps_tags():
    from sensing_worker.enrich import ENRICH_SCHEMA, check_items, parse_items
    from sensing_worker.llm import validate_schema
    docs = [_doc(1), _doc(2, site_type="NEWS", body=None), _doc(3)]
    data = {"items": [
        {"idx": 2, "summary": "언론 기사인데 요약을 보냈다", "topics": ["자사", "없는분류", " 경쟁사 "], "tags": ["메타넷"]},
        {"idx": 1, "summary": "  첫  문장.\n둘째 문장.  ", "topics": ["정책·규제", "정책·규제"],
         "tags": ["a", "b", " ", "c", "a", "d", "e", "f", "g"]},
        {"idx": 1, "summary": "중복 번호는 무시", "topics": ["기타"], "tags": []},
        {"idx": 9, "summary": "없는 번호", "topics": ["기타"], "tags": []},
        {"idx": 3, "summary": "", "topics": ["완전히 다른 값"], "tags": [" ", "국토교통부"]},
    ]}
    assert validate_schema(data, ENRICH_SCHEMA) == []
    out = parse_items(data, docs)
    assert set(out) == {1, 2, 3}
    assert out[1].summary == "첫 문장. 둘째 문장." and out[1].topics == ["정책·규제"]
    assert out[1].tags == ["a", "b", "c", "d", "e"]                       # 빈 값·중복 제거 후 5개
    assert out[2].summary is None and out[2].topics == ["자사", "경쟁사"]  # 언론 기사는 summary 강제 null, 체계 밖 제거
    assert out[3].summary is None and out[3].topics == ["기타"] and out[3].tags == ["국토교통부"]
    assert all(s.status == "DONE" for s in out.values())

    errors = check_items(data, 3)
    assert any("idx 9" in e for e in errors) and any("두 번" in e for e in errors)
    assert check_items({"items": [{"idx": 1}, {"idx": 3}]}, 3) == ["items: 번호 [2]의 항목이 없습니다. 모든 문서에 대해 하나씩 내세요"]
    assert check_items({"items": [{"idx": i} for i in (1, 2, 3)]}, 3) == []


def test_enrich_batch_retries_once_then_marks_batch_failed():
    from sensing_worker.enrich import enrich_batch
    docs = [_doc(1), _doc(2)]
    settings = Settings(enrich_body_chars=100)
    llm = FakeLlm(['{"items": [{"idx": 1, "summary": "a", "topics": ["기타"], "tags": []}]}',          # 2번 누락 → 재요청
                   '{"items": [{"idx": 1, "summary": "a", "topics": ["기타"], "tags": []},'
                   '{"idx": 2, "summary": null, "topics": ["지원사업"], "tags": ["중소벤처기업부"]}]}'])
    results, gen = enrich_batch(llm, docs, settings)
    assert gen.attempts == 2 and "번호 [2]" in llm.calls[1][1]
    assert results[1].summary == "a" and results[2].summary is None and results[2].topics == ["지원사업"]

    llm = FakeLlm(['{"items": []}'])                                       # 두 번 다 빈 응답 → 묶음 전체 FAILED
    results, gen = enrich_batch(llm, docs, settings)
    assert gen.data is None and len(llm.calls) == 2
    assert {r.status for r in results.values()} == {"FAILED"} and "검증 실패 (2회)" in results[1].error


class _FakeEnrichStore:
    """DB 없이 enrich_pending을 돌리기 위한 최소 저장소."""

    def __init__(self, rows):
        import contextlib
        self.rows = rows
        self.saved = []
        self.conn = SimpleNamespace(transaction=contextlib.nullcontext)

    def lock_pending_enrichment(self, limit, failed_retry_hours=24):
        return self.rows[:limit]

    def load_watch_hits(self, ids):
        return {ids[0]: [("자사 보도", "메타넷")]} if ids else {}

    def save_enrichment(self, document_id, document_version, **fields):
        self.saved.append((document_id, document_version, fields))

    # 회사 레이어 (M3): companies=[{id, name, aliases}], links={document_id: [(company_id, matched_by)]}
    companies: list = []
    links: dict = {}
    signals: list = []

    def load_company_names(self):
        return list(self.companies)

    def add_document_company(self, document_id, company_id, matched_by):
        pairs = self.links.setdefault(document_id, [])
        if any(c == company_id for c, _ in pairs):
            return False
        pairs.append((company_id, matched_by))
        return True

    def companies_of_documents(self, ids):
        return {i: list(self.links[i]) for i in ids if i in self.links}

    def add_company_signal(self, company_id, kind, severity, fingerprint, title, detail, observed_at, document_id=None,
                           source_id=None):
        key = (company_id, kind, fingerprint)
        if any((s["company_id"], s["kind"], s["fingerprint"]) == key for s in self.signals):
            return False
        self.signals.append(dict(company_id=company_id, kind=kind, severity=severity, fingerprint=fingerprint, title=title,
                                 detail=detail, observed_at=observed_at, document_id=document_id, source_id=source_id))
        return True


def _rows(n, **kw):
    rows = []
    for i in range(1, n + 1):
        row = dict(id=i, version=1, url=f"https://n.example.com/{i}", title=f"기사 {i}", body_text=None,
                   section=None, domain="n.example.com", site_type="NEWS")
        row.update(kw)
        rows.append(row)
    return rows


def test_enrich_pending_writes_nothing_when_llm_is_unavailable():
    from fakellm import BatchFakeLlm
    from sensing_worker.enrich import enrich_pending
    store = _FakeEnrichStore(_rows(3))
    outcome = enrich_pending(store, BatchFakeLlm(error=LlmUnavailable("down")), Settings(), limit=10)
    assert outcome.llm_unavailable and outcome.fetched == 3 and outcome.done == 0
    assert store.saved == []

    outcome = enrich_pending(_FakeEnrichStore([]), BatchFakeLlm(error=LlmUnavailable("down")), Settings(), limit=10)
    assert outcome.fetched == 0 and not outcome.llm_unavailable


def test_enrich_pending_saves_results_and_skips_empty_documents():
    from fakellm import BatchFakeLlm
    from sensing_worker.enrich import enrich_pending
    rows = _rows(2) + _rows(1, id=3, title=None, body_text="  ")                        # 3번은 제목·본문 없음
    rows += _rows(1, id=4, site_type="PUBLIC", body_text="공고 본문 " * 20, title="2026년 지원사업 공고")
    store = _FakeEnrichStore(rows)
    llm = BatchFakeLlm()
    outcome = enrich_pending(store, llm, Settings(), limit=10)
    assert (outcome.fetched, outcome.done, outcome.skipped, outcome.failed) == (4, 3, 1, 0)
    assert outcome.usage == {"input_tokens": 1000, "output_tokens": 50}
    assert len(llm.calls) == 1 and "[문서 3]" in llm.calls[0][1] and "[문서 4]" not in llm.calls[0][1]   # 빈 문서는 안 보냄
    assert "감시 '자사 보도' 검색어 '메타넷'" in llm.calls[0][1]
    saved = {doc_id: fields for doc_id, _, fields in store.saved}
    assert saved[1]["summary"] is None and saved[1]["topics"] == ["정책·규제"] and len(saved[1]["tags"]) == 5   # 언론: 요약 없음
    assert saved[1]["status"] == "DONE" and saved[1]["model"] == "fake fake-1"
    assert saved[3]["status"] == "SKIPPED" and saved[3]["topics"] == []
    assert saved[4]["summary"] == "요약 3" and saved[4]["status"] == "DONE"                 # 본문 있는 문서는 요약 저장


def test_main_loop_runs_enrich_only_when_idle_and_llm_is_configured(monkeypatch):
    from sensing_worker import main as m
    calls = []
    monkeypatch.setattr(m, "enrich_pending", lambda store, llm, settings, limit: calls.append(limit))
    m.enrich_idle(None, NoLlm(), Settings())
    assert calls == []
    m.enrich_idle(None, FakeLlm(["{}"]), Settings(enrich_enabled=False))
    assert calls == []
    m.enrich_idle(None, FakeLlm(["{}"]), Settings(enrich_batch=7))
    assert calls == [7]

    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(m, "enrich_pending", boom)
    m.enrich_idle(None, FakeLlm(["{}"]), Settings())     # 보강 오류는 루프를 죽이지 않는다


# ---------------------------------------------------------------- 회사 레이어 (M3): 국세청 상태 커넥터, 태그 매칭, 신호 규칙

def test_data_go_kr_is_a_shared_portal_provider():
    from sensing_worker.apis import catalog
    p = catalog.lookup("api.odcloud.kr")
    assert p.id == "data_go_kr" and p.needs_key and not p.keyword_based
    assert catalog.lookup("apis.data.go.kr").id == "data_go_kr"
    assert "국세청 사업자등록 상태조회 활용신청 필요" in p.description and "data.go.kr" in p.signup_url
    assert catalog.lookup("www.data.go.kr") is None                     # 포털 홈 자체는 API 도메인이 아니다


def test_nts_connector_checks_in_batches_and_classifies_statuses_without_leaking_key():
    from fakents import GOOD_KEY, closed, continuing, nts_handler, suspended, unregistered
    from sensing_worker.apis.nts_status import BATCH_SIZE, NtsStatusConnector
    nos = [f"{1000000000 + i:010d}" for i in range(0, 250)]              # 250건 → 100·100·50 세 번
    handler = nts_handler({nos[0]: closed(nos[0]), nos[1]: suspended(nos[1]), nos[2]: unregistered(nos[2]),
                           nos[3]: continuing(nos[3], "부가가치세 간이과세자", "02")})
    c = NtsStatusConnector(_api_fetcher(handler))
    c.verify(GOOD_KEY)
    assert handler.calls[0] == (["0000000000"], "POST")
    assert c.default_config() == {"method": "API", "provider": "data_go_kr", "check": "nts_status"}
    assert c.fetch(GOOD_KEY, {}, datetime.now(timezone.utc).date(), 10) == []   # 문서를 만들지 않는다

    statuses = c.check(GOOD_KEY, ["123-45-67890"] + nos[1:] + [nos[0], "bad"])   # 하이픈 정규화·중복·잘못된 형식
    assert [len(b) for b, _ in handler.calls[1:]] == [BATCH_SIZE, BATCH_SIZE, 51]
    assert handler.calls[1][0][0] == "1234567890"
    by_no = {s.biz_no: s for s in statuses}
    assert len(statuses) == 251 and len(by_no) == 251                       # 응답 순서와 무관하게 요청 순서, 중복 없음
    assert by_no[nos[0]].status_code == "03" and by_no[nos[0]].end_dt == "20260930" and not by_no[nos[0]].continuing
    assert by_no[nos[1]].status_code == "02" and by_no[nos[1]].status == "휴업자"
    assert by_no[nos[2]].status_code == "" and not by_no[nos[2]].registered and by_no[nos[2]].status == "미등록"
    assert "등록되지 않은" in by_no[nos[2]].tax_type
    assert by_no[nos[3]].continuing and by_no[nos[3]].tax_type_code == "02"
    assert by_no["1234567890"].status_code == "" and by_no[nos[0]].as_detail()["b_stt"] == "폐업자"
    # 키는 요청 URL에만 실린다 (인코딩되어). 응답·오류 어디에도 남지 않는다 (아래 테스트)
    assert all("serviceKey=" in u and GOOD_KEY not in u for u in handler.urls)


def test_nts_connector_classifies_key_errors_and_outages_and_masks_key():
    from fakents import GOOD_KEY, nts_handler
    from sensing_worker.apis.nts_status import NtsStatusConnector

    with pytest.raises(CrawlFailure) as e:                                   # 잘못된 키 → API_KEY_INVALID, 본문·URL 모두 가림
        NtsStatusConnector(_api_fetcher(nts_handler())).verify("wrong-service-key")
    assert e.value.code == Code.API_KEY_INVALID and not e.value.retryable and e.value.http_status == 401
    assert "등록되지 않은 인증키" in str(e.value) and "wrong-service-key" not in str(e.value)
    assert "serviceKey=***" in e.value.url and "wrong-service-key" not in e.value.url

    with pytest.raises(CrawlFailure) as e:                                   # 한도 초과 → API_ERROR(재시도)
        NtsStatusConnector(_api_fetcher(nts_handler(http_status=429))).check(GOOD_KEY, ["1234567890"])
    assert e.value.code == Code.API_ERROR and e.value.retryable and e.value.http_status == 429
    assert GOOD_KEY not in (e.value.url + str(e.value) + (e.value.evidence or ""))

    with pytest.raises(CrawlFailure) as e:                                   # 서버 장애도 재시도
        NtsStatusConnector(_api_fetcher(nts_handler(http_status=503))).check(GOOD_KEY, ["1234567890"])
    assert e.value.code == Code.API_ERROR and e.value.retryable

    def down(request):
        raise httpx.ConnectError("boom", request=request)
    with pytest.raises(CrawlFailure) as e:
        NtsStatusConnector(_api_fetcher(down)).verify(GOOD_KEY)
    assert e.value.code == Code.TIMEOUT and GOOD_KEY not in e.value.url and "serviceKey=***" in e.value.url

    assert NtsStatusConnector(_api_fetcher(nts_handler())).check(GOOD_KEY, ["bad", ""]) == []   # 보낼 번호가 없으면 호출 없음


def test_post_api_does_not_follow_redirects_and_keeps_private_host_guard():
    seen = []

    def handler(request):
        seen.append((request.method, str(request.url)))
        return httpx.Response(302, headers={"location": "https://evil.example.com/"}, json={})
    page = _api_fetcher(handler).post_api("https://api.odcloud.kr/x", {"a": 1}, headers={"Accept": "application/json"})
    assert page.status == 302 and seen == [("POST", "https://api.odcloud.kr/x")]

    s = Settings(min_interval_seconds=0, max_retries=1, allow_private_hosts=False)
    with pytest.raises(CrawlFailure) as e:
        PoliteFetcher(s, transport=httpx.MockTransport(handler)).post_api("http://10.0.0.5/api", {})
    assert e.value.code == Code.INTERNAL_ADDRESS


def test_tags_link_to_companies_only_on_exact_name_or_alias_match():
    from sensing_worker.company import build_name_index, match_companies_by_tags, normalize_name
    index = build_name_index([
        {"id": 1, "name": "가전자", "aliases": ["GA Electronics", "가전자(주)"]},
        {"id": 2, "name": "나부품", "aliases": []},
        {"id": 3, "name": "가전자", "aliases": ["중복이름"]},                      # 같은 이름은 먼저 온 회사가 이긴다
    ])
    assert normalize_name("  GA  Electronics ") == "gaelectronics"
    assert match_companies_by_tags(["가전자", "나부품", "가전자"], index) == [1, 2]
    assert match_companies_by_tags(["ga electronics", "가 전 자 (주)", "중복이름"], index) == [1, 3]   # 대소문자·공백 무시
    assert match_companies_by_tags(["가전자 반도체", "주식회사 가전자", "나부", "전자", "GA"], index) == []   # 부분 일치는 안 된다
    assert match_companies_by_tags([None, 3, ""], index) == [] and match_companies_by_tags([], {}) == []


def test_biz_status_signal_severity_and_fingerprint_rules():
    from fakents import closed, continuing, suspended, unregistered
    from sensing_worker.apis.nts_status import _to_status
    from sensing_worker.company import biz_status_signal

    s = biz_status_signal(_to_status(closed("1234567890", "20260930")), None)
    assert (s.kind, s.severity, s.fingerprint) == ("BIZ_STATUS", "HIGH", "0320260930")
    assert s.title == "폐업자 (폐업일 2026-09-30)" and s.detail["b_stt_cd"] == "03"
    assert biz_status_signal(_to_status(suspended("1234567890")), {"b_stt_cd": "01"}).severity == "HIGH"
    assert biz_status_signal(_to_status(suspended("1234567890")), None).fingerprint == "02"
    u = biz_status_signal(_to_status(unregistered("1234567890")), None)
    assert u.severity == "MEDIUM" and u.fingerprint == "unregistered" and "등록되지 않은" in u.title
    # 같은 폐업 상태는 다음 날에도 같은 fingerprint → DB unique가 중복을 막는다
    again = biz_status_signal(_to_status(closed("1234567890", "20260930")), {"b_stt_cd": "03", "end_dt": "20260930"})
    assert again.fingerprint == "0320260930"

    ok = _to_status(continuing("1234567890"))
    assert biz_status_signal(ok, None) is None                                           # 처음 확인한 정상 사업자: 신호 없음
    assert biz_status_signal(ok, {"b_stt_cd": "01", "tax_type_cd": "01"}) is None        # 변화 없음
    tax = biz_status_signal(_to_status(continuing("1234567890", "부가가치세 일반과세자", "01")),
                            {"b_stt_cd": "01", "tax_type": "부가가치세 간이과세자", "tax_type_cd": "02"})
    assert tax.severity == "LOW" and tax.fingerprint == "tax:01" and tax.title == "과세유형 변경: 부가가치세 간이과세자 → 부가가치세 일반과세자"
    resumed = biz_status_signal(ok, {"b_stt_cd": "02", "tax_type_cd": "01"})
    assert resumed.severity == "LOW" and "영업 재개" in resumed.title and resumed.fingerprint == "01"


def test_news_signal_only_for_incident_and_personnel_topics():
    from sensing_worker.company import news_signal
    s = news_signal("h" * 64, "  나부품 공장 화재 ", ["사건·사고", "산업동향"], url="https://n/1", matched_by="WATCH")
    assert (s.kind, s.severity, s.fingerprint, s.title) == ("NEWS", "MEDIUM", "h" * 64, "나부품 공장 화재")
    assert s.detail == {"topics": ["사건·사고"], "url": "https://n/1", "matched_by": "WATCH"}
    assert news_signal("h", "임원 인사", ["인사·조직"]).severity == "MEDIUM"
    assert news_signal("h", "신제품", ["자사", "산업동향"]) is None and news_signal("h", "x", []) is None
    assert news_signal("h", None, ["사건·사고"]).title == "(제목 없음)"


def test_enrich_links_tags_to_companies_and_raises_news_signals_in_the_same_pass():
    from fakellm import BatchFakeLlm
    from sensing_worker.db import sha256
    from sensing_worker.enrich import enrich_pending
    rows = _rows(3)
    store = _FakeEnrichStore(rows)
    store.companies = [{"id": 1, "name": "가전자", "aliases": ["GA"]}, {"id": 2, "name": "나부품", "aliases": []}]
    store.links = {2: [(2, "WATCH")]}                                             # 2번 기사는 나부품 감시로 이미 연결됨
    store.signals = []

    def make(idx, prompt):
        if idx == 1:
            return {"idx": 1, "summary": None, "topics": ["사건·사고"], "tags": ["가전자", "가전자 반도체사업부"]}   # 태그 정확 일치 → 1번 회사
        if idx == 2:
            return {"idx": 2, "summary": None, "topics": ["인사·조직"], "tags": ["누군가"]}                   # 감시 연결 → 신호
        return {"idx": 3, "summary": None, "topics": ["사건·사고"], "tags": ["전자"]}                       # 부분 일치뿐 → 연결 없음
    outcome = enrich_pending(store, BatchFakeLlm(make), Settings(), limit=10)
    assert (outcome.done, outcome.company_links, outcome.company_signals) == (3, 1, 2)
    assert store.links == {2: [(2, "WATCH")], 1: [(1, "TAG")]}
    assert sorted((s["company_id"], s["kind"], s["severity"], s["fingerprint"], s["document_id"], s["detail"]["matched_by"])
                  for s in store.signals) == [(1, "NEWS", "MEDIUM", sha256("https://n.example.com/1"), 1, "TAG"),
                                              (2, "NEWS", "MEDIUM", sha256("https://n.example.com/2"), 2, "WATCH")]
    assert {s["title"] for s in store.signals} == {"기사 1", "기사 2"}

    # 같은 묶음을 다시 보강해도(본문 변경 등) 신호는 같은 fingerprint라 늘지 않는다
    again = enrich_pending(store, BatchFakeLlm(make), Settings(), limit=10)
    assert again.company_signals == 0 and again.company_links == 0 and len(store.signals) == 2

    # 회사가 하나도 없으면 아무것도 연결하지 않는다
    empty = _FakeEnrichStore(_rows(1))
    empty.companies, empty.links, empty.signals = [], {}, []
    enrich_pending(empty, BatchFakeLlm(make), Settings(), limit=10)
    assert empty.links == {} and empty.signals == []


# ---------------------------------------------------------------------- 회사 점검 (M3-2): DART 고유번호 매핑·회사별 공시·조달청 부정당업자

def test_dart_corp_codes_zip_is_parsed_and_key_errors_are_masked(monkeypatch):
    import io
    import zipfile
    from fakedart import CORPS, GOOD_KEY, corp_zip, dart_handler
    import sensing_worker.apis.dart as dart_mod
    from sensing_worker.apis.dart import DartConnector, parse_corp_code_zip

    entries = parse_corp_code_zip(corp_zip())
    assert [(e.corp_code, e.corp_name, e.stock_code) for e in entries] == list(CORPS)   # stock_code의 공백은 지운다
    assert entries[0].listed and not entries[1].listed and entries[0].modify_date == "20260101"
    with pytest.raises(zipfile.BadZipFile):
        parse_corp_code_zip(b"PK not really a zip")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("readme.txt", "no xml here")
    with pytest.raises(ValueError):
        parse_corp_code_zip(buf.getvalue())

    handler = dart_handler()
    c = DartConnector(_api_fetcher(handler))
    assert len(c.corp_codes(GOOD_KEY)) == len(CORPS)
    assert handler.calls[-1] == "https://opendart.fss.or.kr/api/corpCode.xml?crtfc_key=" + GOOD_KEY   # 키는 요청에만

    with pytest.raises(CrawlFailure) as e:                                   # 틀린 키: ZIP 대신 XML 010 → API_KEY_INVALID, 가림
        c.corp_codes("wrong-secret-xyz")
    assert e.value.code == Code.API_KEY_INVALID and not e.value.retryable and "010" in str(e.value)
    assert "wrong-secret-xyz" not in str(e.value) + e.value.url and "crtfc_key=***" in e.value.url

    with pytest.raises(CrawlFailure) as e:                                   # 서버 장애 → 재시도
        DartConnector(_api_fetcher(dart_handler(server_status=503))).corp_codes(GOOD_KEY)
    assert e.value.code == Code.API_ERROR and e.value.retryable and e.value.http_status == 503

    monkeypatch.setattr(dart_mod, "CORP_ZIP_MAX_BYTES", 100)                 # 상한보다 큰 ZIP은 받지 않는다 (압축 폭탄 방어)
    with pytest.raises(CrawlFailure) as e:
        DartConnector(_api_fetcher(dart_handler())).corp_codes(GOOD_KEY)
    assert e.value.code == Code.API_ERROR and not e.value.retryable and "상한" in str(e.value)


def test_get_api_bytes_keeps_body_and_flags_oversize():
    big = b"PK\x03\x04" + b"\0" * 100_000

    def handler(request):
        return httpx.Response(200, content=big, headers={"content-type": "application/zip"})
    f = _api_fetcher(handler)
    page = f.get_api_bytes("https://opendart.fss.or.kr/api/corpCode.xml")
    assert page.body == big and page.text == "" and not page.truncated           # 큰 바이너리는 텍스트로 풀지 않는다
    small = _api_fetcher(lambda r: httpx.Response(200, text="<result><status>010</status></result>"))
    assert small.get_api_bytes("https://opendart.fss.or.kr/x").text.startswith("<result>")   # 작은 오류 응답은 텍스트도 둔다
    page = f.get_api_bytes("https://opendart.fss.or.kr/api/corpCode.xml", max_bytes=1000)
    assert page.truncated and len(page.body) <= 1000
    assert f.get_api("https://opendart.fss.or.kr/api/list.json").body == b"" and not f.get_api("https://x.example.com/").truncated


def test_corp_name_normalization_and_mapping_rules():
    from fakedart import CORPS
    from sensing_worker.apis.dart import CorpEntry
    from sensing_worker.company import build_corp_index, normalize_corp_name, pick_corp_code
    assert normalize_corp_name(" 주식회사 나부품 ") == normalize_corp_name("(주)나부품") == normalize_corp_name("㈜ 나부품") == "나부품"
    assert normalize_corp_name("GA Electronics Co.") == "gaelectronicsco." and normalize_corp_name(None) == ""

    index = build_corp_index(CorpEntry(c, n, s) for c, n, s in CORPS)
    assert pick_corp_code("나부품 주식회사", [], index).entry.corp_code == "00000001"          # 유일 → 채움
    ga = pick_corp_code("가전자", [], index)
    assert ga.entry.corp_code == "00000002" and len(ga.candidates) == 2 and not ga.ambiguous   # 둘 중 상장사 하나 → 그것
    dup = pick_corp_code("다중복", ["나부품"], index)
    assert dup.entry is None and dup.ambiguous and dup.matched_on == "name"                   # 비상장 둘 → 모호, 별칭으로 넘어가지 않는다
    alias = pick_corp_code("없는회사", ["NABU", "라물류"], index)
    assert alias.entry.corp_code == "00000006" and alias.matched_on == "alias:라물류"           # "(주)라물류"와 정확 일치
    none = pick_corp_code("없는회사", ["나부"], index)
    assert none.entry is None and not none.ambiguous and none.candidates == []               # 부분 일치는 안 된다
    assert pick_corp_code(None, [None, 3], index).entry is None and pick_corp_code("x", [], {}).matched_on is None


def test_disclosure_severity_rules_and_signal_fingerprint():
    from sensing_worker.company import disclosure_severity, disclosure_signal
    assert disclosure_severity("회생절차 개시신청") == ("HIGH", ["회생절차"])
    assert disclosure_severity("감사보고서 (감사의견: 의견거절)")[0] == "HIGH"
    for word in ("파산신청", "영업정지", "상장폐지", "부적정", "자본잠식", "불성실공시법인지정", "횡령·배임혐의발생"):
        assert disclosure_severity(word)[0] == "HIGH", word
    assert disclosure_severity("[기재정정]주요사항보고서(유상증자결정)") == ("MEDIUM", ["유상증자"])    # HIGH→MEDIUM→LOW 순
    for word in ("소송등의제기", "최대주주 변경", "대표이사 변경", "회사합병결정", "회사분할결정", "영업양수결정", "영업양도결정", "매매거래정지"):
        assert disclosure_severity(word)[0] == "MEDIUM", word
    assert disclosure_severity("주요사항보고서(자기주식취득결정)") == ("LOW", ["주요사항보고서"])
    assert disclosure_severity("[기재정정]사업보고서 (2025.12)") == ("LOW", ["기재정정", "정정"])
    assert disclosure_severity("사업보고서 (2025.12)") == (None, []) and disclosure_severity(None) == (None, [])
    assert disclosure_severity("임원ㆍ주요주주특정증권등소유상황보고서") == (None, [])

    s = disclosure_signal("20261015000001", " 회생절차개시신청 ", corp_name="나부품", rcept_dt="20261015", url="https://dart/x", flr_nm="나부품")
    assert (s.kind, s.severity, s.fingerprint, s.title) == ("DISCLOSURE", "HIGH", "20261015000001", "회생절차개시신청")
    assert s.detail["matched"] == ["회생절차"] and s.detail["corp_name"] == "나부품" and s.detail["url"] == "https://dart/x"
    assert disclosure_signal("1", "사업보고서") is None


def test_g2b_connector_checks_by_biz_no_and_switches_field_names_and_modes():
    from fakeg2b import GOOD_KEY, g2b_handler, sanction
    from sensing_worker.apis.g2b_debarment import DEFAULT_ENDPOINT, G2bDebarmentConnector
    handler = g2b_handler({"1234567890": [sanction("1234567890", "나부품"),
                                          sanction("1234567890", "나부품", start="20250101", end="20250301", agency="국방부")]})
    c = G2bDebarmentConnector(_api_fetcher(handler))
    c.verify(GOOD_KEY)
    assert handler.calls == [("1", "0000000000", "1")]
    assert c.default_config()["check"] == "g2b_debarment" and c.default_config()["endpoint"] == DEFAULT_ENDPOINT
    assert c.fetch(GOOD_KEY, {}, datetime.now(timezone.utc).date(), 10) == []        # 문서를 만들지 않는다

    companies = [{"id": 1, "name": "나부품", "aliases": [], "biz_no": "123-45-67890"},
                 {"id": 2, "name": "가전자", "aliases": [], "biz_no": None},
                 {"id": 3, "name": "다", "aliases": [], "biz_no": "1018112345"}]
    result = c.check(GOOD_KEY, companies, c.default_config())
    assert list(result) == [1] and len(result[1]) == 2
    d = result[1][0]
    assert (d.biz_no, d.company_name, d.start_date, d.end_date, d.agency) == ("1234567890", "나부품", "20260101", "20260630", "조달청")
    assert d.reason == "계약의 이행을 하지 아니한 자" and d.period == "2026-01-01~2026-06-30" and d.raw["lmtBgnDt"] == "20260101"
    assert handler.calls[1:] == [("1", "1234567890", "1"), ("1", "1018112345", "1")]   # 번호 없는 회사는 조회하지 않는다
    assert all("ServiceKey=" in u and GOOD_KEY not in u for u in handler.urls)

    # 응답 항목명이 후보에 없으면 config.fields로 바꾼다 (실제 스펙 확인 뒤 운영자가 넣는 값)
    custom = g2b_handler({"1018112345": [{"bizno": "1018112345", "entNm": "다", "sanctStart": "2026-02-01",
                                          "sanctEnd": "2026-04-30", "ordInst": "서울시"}]})
    cfg = {**c.default_config(), "fields": {"company_name": "entNm", "start_date": "sanctStart", "end_date": "sanctEnd", "agency": "ordInst"}}
    d2 = G2bDebarmentConnector(_api_fetcher(custom)).check(GOOD_KEY, companies, cfg)[3][0]
    assert (d2.company_name, d2.start_date, d2.end_date, d2.agency, d2.reason) == ("다", "20260201", "20260430", "서울시", None)

    # 기간 조회 모드: 조회구분 값이 없으면 설정 오류(재시도 없음), 있으면 전체를 페이징해 사업자번호 → 정규화한 이름으로 대조
    with pytest.raises(CrawlFailure) as e:
        c.check(GOOD_KEY, companies, {"mode": "period"})
    assert e.value.code == Code.API_ERROR and not e.value.retryable and "period_inqry_div" in str(e.value)
    period = g2b_handler({"1234567890": [sanction("1234567890", "나부품")], "9999999999": [sanction("9999999999", "(주)가전자")],
                          "8888888888": [sanction("8888888888", "남")]}, per_page=2)
    r3 = G2bDebarmentConnector(_api_fetcher(period)).check(GOOD_KEY, companies, {"mode": "period", "period_inqry_div": "2", "page_size": 2})
    assert sorted(r3) == [1, 2] and r3[2][0].company_name == "(주)가전자"
    assert period.calls == [("2", None, "1"), ("2", None, "2")]                       # 3건을 2건씩 두 페이지
    assert "inqryBgnDt=" in period.urls[0] and "inqryEndDt=" in period.urls[0]


def test_g2b_connector_classifies_key_and_limit_errors_and_masks_key():
    from fakeg2b import GOOD_KEY, g2b_handler
    from sensing_worker.apis.g2b_debarment import G2bDebarmentConnector
    one = [{"id": 1, "name": "x", "aliases": [], "biz_no": "1234567890"}]

    with pytest.raises(CrawlFailure) as e:                                   # 게이트웨이 30 (HTTP 200 + XML) → 키 오류, 본문·URL 가림
        G2bDebarmentConnector(_api_fetcher(g2b_handler())).verify("wrong-service-key")
    assert e.value.code == Code.API_KEY_INVALID and not e.value.retryable and "30" in str(e.value)
    assert "wrong-service-key" not in str(e.value) + e.value.url and "ServiceKey=***" in e.value.url

    for status in (401, 403):
        with pytest.raises(CrawlFailure) as e:
            G2bDebarmentConnector(_api_fetcher(g2b_handler(http_status=status))).verify(GOOD_KEY)
        assert e.value.code == Code.API_KEY_INVALID and e.value.http_status == status

    with pytest.raises(CrawlFailure) as e:                                   # 429 → 재시도
        G2bDebarmentConnector(_api_fetcher(g2b_handler(http_status=429))).check(GOOD_KEY, one)
    assert e.value.code == Code.API_ERROR and e.value.retryable and e.value.http_status == 429
    assert GOOD_KEY not in (e.value.url + str(e.value) + (e.value.evidence or ""))

    with pytest.raises(CrawlFailure) as e:                                   # 게이트웨이 22 (일 한도) → 재시도, 키 오류 아님
        G2bDebarmentConnector(_api_fetcher(g2b_handler(gateway_code="22"))).verify(GOOD_KEY)
    assert e.value.code == Code.API_ERROR and e.value.retryable and "22" in str(e.value)

    with pytest.raises(CrawlFailure) as e:
        G2bDebarmentConnector(_api_fetcher(g2b_handler(http_status=503))).check(GOOD_KEY, one)
    assert e.value.code == Code.API_ERROR and e.value.retryable

    def down(request):
        raise httpx.ConnectError("boom", request=request)
    with pytest.raises(CrawlFailure) as e:
        G2bDebarmentConnector(_api_fetcher(down)).verify(GOOD_KEY)
    assert e.value.code == Code.TIMEOUT and "ServiceKey=***" in e.value.url

    assert G2bDebarmentConnector(_api_fetcher(g2b_handler())).check(GOOD_KEY, [{"id": 1, "name": "x", "biz_no": None}]) == {}


def test_debarment_signal_is_high_with_start_date_and_agency_fingerprint():
    from fakeg2b import sanction
    from sensing_worker.apis.g2b_debarment import Debarment, _to_debarment
    from sensing_worker.company import debarment_signal
    d = _to_debarment(sanction("1234567890", "나부품"), None)
    s = debarment_signal(d)
    assert (s.kind, s.severity, s.fingerprint) == ("DEBARMENT", "HIGH", "20260101:조달청")
    assert s.title == "입찰참가자격 제한 (2026-01-01~2026-06-30, 조달청)" and s.detail["raw"] == d.raw
    assert s.detail["reason"] == "계약의 이행을 하지 아니한 자" and s.detail["biz_no"] == "1234567890"
    bare = debarment_signal(Debarment("1234567890", "나부품", None, None, None, None, {"x": 1}))
    assert bare.fingerprint.startswith("raw:") and bare.title == "입찰참가자격 제한 (기간 미상, 기관 미상)"
    assert _to_debarment({"foo": "bar"}, None) is None                                # 번호도 이름도 없으면 버린다
    assert _to_debarment({"bizno": "1234567890", "unptRsttBgnDt": "202603011200"}, None).start_date == "20260301"   # 시각이 붙어도 날짜 8자리


def test_check_connector_is_chosen_by_check_kind():
    from sensing_worker.apis import DartConnector, G2bDebarmentConnector, NtsStatusConnector, make_check_connector
    f = _api_fetcher(lambda r: httpx.Response(404))
    assert isinstance(make_check_connector({"provider": "data_go_kr", "check": "nts_status"}, f), NtsStatusConnector)
    assert isinstance(make_check_connector({"provider": "data_go_kr", "check": "g2b_debarment"}, f), G2bDebarmentConnector)
    assert isinstance(make_check_connector({"provider": "dart", "check": "dart_corp_map"}, f), DartConnector)
    assert isinstance(make_check_connector({"provider": "dart", "check": "dart_disclosure"}, f), DartConnector)
    assert isinstance(make_check_connector({"provider": "data_go_kr"}, f), NtsStatusConnector)   # 종류가 없으면 제공자 기본


def test_dart_check_kinds_fill_corp_codes_and_record_disclosures(tmp_path):
    from fakedart import GOOD_KEY, dart_handler, disclosure
    from sensing_worker.db import Job
    handler = dart_handler([disclosure("00000001", "나부품", "회생절차개시신청", "20261016000001"),
                            disclosure("00000001", "나부품", "사업보고서 (2025.12)", "20261016000002"),
                            disclosure("00000002", "가전자", "주요사항보고서(유상증자결정)", "20261016000003", stock_code="001234")])
    runner, store, _ = _api_runner(tmp_path, handler, credential=GOOD_KEY)
    store.load_source.return_value.update({"kind": "CHECK", "status": "ACTIVE", "interval_minutes": 10080, "active_config_version_id": 11,
                                           "config": {"method": "API", "provider": "dart", "check": "dart_corp_map"}})
    store.companies_without_corp_code.return_value = [{"id": 1, "name": "나부품 주식회사", "aliases": []}, {"id": 2, "name": "다중복", "aliases": []}]
    store.set_corp_code.return_value = True
    job = Job(id=9, source_id=3, job_type="CRAWL", trigger="SCHEDULE", config_version_id=11, attempt=1)

    # 고유번호 매핑: 유일 일치만 채운다 (다중복은 모호 → 로그만). found 2, new 1
    assert runner.run(job) == "SUCCESS"
    store.set_corp_code.assert_called_once_with(1, "00000001")
    assert store.finish_job.call_args.args[:4] == (9, "SUCCESS", 2, 1)
    assert sum("corpCode.xml" in u for u in handler.calls) == 1 and not any("list.json" in u for u in handler.calls)
    store.add_company_signal.assert_not_called()

    # 회사별 공시: 회사마다 corp_code로 부르고, 문서 저장 + API 연결 + 신호 (회생 HIGH, 유상증자 MEDIUM, 사업보고서는 문서만)
    store.reset_mock()
    store.load_source.return_value["config"] = {"method": "API", "provider": "dart", "check": "dart_disclosure", "days": 3}
    store.companies_with_corp_code.return_value = [{"id": 1, "name": "나부품", "corp_code": "00000001"},
                                                   {"id": 2, "name": "가전자", "corp_code": "00000002"}]
    store.upsert_document.return_value = (41, "NEW")
    store.add_company_signal.return_value = True
    assert runner.run(job) == "SUCCESS"
    assert store.upsert_document.call_count == 3 and store.add_document_company.call_count == 3
    assert store.add_document_company.call_args_list[0].args == (41, 1, "API")
    assert store.upsert_document.call_args_list[0].kwargs["url"] == "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20261016000001"
    sigs = [c.args[:4] for c in store.add_company_signal.call_args_list]
    assert sigs == [(1, "DISCLOSURE", "HIGH", "20261016000001"), (2, "DISCLOSURE", "MEDIUM", "20261016000003")]
    assert store.add_company_signal.call_args_list[0].kwargs["document_id"] == 41
    assert store.finish_job.call_args.args[:4] == (9, "SUCCESS", 2, 2)
    assert [u for u in handler.calls if "corp_code=00000001" in u] and [u for u in handler.calls if "corp_code=00000002" in u]

    # 한 회사의 API 오류는 기록하고 다음 회사로 (PARTIAL); 키 오류는 전체 실패 + 자격 증명 INVALID
    store.reset_mock()
    store.load_source.return_value["config"] = {"method": "API", "provider": "dart", "check": "dart_disclosure", "days": 3}
    store.upsert_document.return_value = (41, "SAME")
    store.add_company_signal.return_value = False
    broken = dart_handler(api_status="100")
    runner2, store2, _ = _api_runner(tmp_path, broken, credential=GOOD_KEY)
    store2.load_source.return_value.update(store.load_source.return_value)
    store2.companies_with_corp_code.return_value = store.companies_with_corp_code.return_value
    assert runner2.run(job) == "PARTIAL"
    assert store2.record_error.call_count == 2 and store2.finish_job.call_args.args[:5] == (9, "PARTIAL", 0, 0, 2)
    runner3, store3, _ = _api_runner(tmp_path, dart_handler(), credential="stale-key")
    store3.load_source.return_value.update(store.load_source.return_value)
    store3.companies_with_corp_code.return_value = store.companies_with_corp_code.return_value
    assert runner3.run(job) == "FAILED"
    assert store3.mark_credential.call_args.args[:2] == ("dart", "INVALID") and "stale-key" not in store3.mark_credential.call_args.args[2]
    store3.set_source_status.assert_called_once_with(3, "REVIEW", "API_KEY_INVALID")

    # 지원하지 않는 종류
    store.load_source.return_value["config"] = {"method": "API", "provider": "dart", "check": "dart_financial"}
    assert runner.run(job) == "FAILED"
    assert store.record_error.call_args.args[1].code == Code.UNEXPECTED


def test_g2b_check_kind_records_debarment_signals_with_mock_store(tmp_path):
    from fakeg2b import GOOD_KEY, g2b_handler, sanction
    from sensing_worker.db import Job
    handler = g2b_handler({"1234567890": [sanction("1234567890", "나부품")]})
    runner, store, _ = _api_runner(tmp_path, handler, credential=GOOD_KEY)
    store.load_source.return_value.update({"kind": "CHECK", "status": "ACTIVE", "interval_minutes": 1440, "active_config_version_id": 11,
                                           "domain": "apis.data.go.kr",
                                           "config": {"method": "API", "provider": "data_go_kr", "check": "g2b_debarment", "days": 7}})
    store.companies_for_debarment.return_value = [{"id": 1, "name": "나부품", "aliases": [], "biz_no": "1234567890"},
                                                  {"id": 2, "name": "가전자", "aliases": [], "biz_no": None}]
    store.add_company_signal.return_value = True
    job = Job(id=10, source_id=3, job_type="CRAWL", trigger="SCHEDULE", config_version_id=11, attempt=1)
    assert runner.run(job) == "SUCCESS"
    assert handler.calls == [("1", "1234567890", "1")]                              # 주기 실행은 시험 호출 없이, 번호 있는 회사만
    args, kwargs = store.add_company_signal.call_args.args, store.add_company_signal.call_args.kwargs
    assert args[:4] == (1, "DEBARMENT", "HIGH", "20260101:조달청") and args[4] == "입찰참가자격 제한 (2026-01-01~2026-06-30, 조달청)"
    assert args[6].isoformat() == "2026-01-01T00:00:00+09:00" and kwargs["source_id"] == 3    # observed_at = 제재 시작일 (KST)
    assert store.finish_job.call_args.args[:4] == (10, "SUCCESS", 2, 1)
    store.load_credential.assert_called_once_with("data_go_kr", "unit-secret")


def test_claim_order_alternates_onboard_and_crawl():
    # 기본 3:1 — 등록 셋을 집으면 네 번째는 주기 수집을 먼저 집는다. 빈 polling은 세지 않으므로 claimed만 본다
    assert [preferred_job_type(n, 3) for n in range(8)] == ["ONBOARD"] * 3 + ["CRAWL"] + ["ONBOARD"] * 3 + ["CRAWL"]
    assert [preferred_job_type(n, 1) for n in range(4)] == ["ONBOARD", "CRAWL", "ONBOARD", "CRAWL"]
    assert all(preferred_job_type(n, 0) == "ONBOARD" for n in range(10))     # 0이면 예전 규칙


def test_adaptive_interval_grows_when_quiet_and_returns_to_base_when_busy():
    # 늘리기: 최근 10회 평균 새 문서 0.2건 → 두 배. 상한 1440분
    assert next_interval(60, 60, [0, 0, 0, 0, 0, 1, 0, 0, 1, 0])[0] == 120
    assert next_interval(120, 60, [0] * 10)[0] == 240
    new, note = next_interval(1000, 60, [0] * 10)
    assert new == 1440 and "1000→1440분" in note and "평균 새 문서 0.0건" in note
    assert next_interval(1440, 60, [0] * 10) == (1440, None)                  # 상한에서는 더 안 늘린다
    # 줄이기: 평균이 실행당 상한(40)의 절반을 넘으면 기본값까지 한 번에
    new, note = next_interval(480, 60, [25, 30, 21, 40, 22, 35, 28, 19, 33, 40])
    assert new == 60 and "480→60분" in note and "기본값으로" in note
    assert next_interval(60, 60, [40] * 10) == (60, None)                     # 이미 기본값
    # 그 사이는 그대로. 이력이 10회 미만이면 판단 보류
    assert next_interval(60, 60, [3, 5, 2, 4, 6, 1, 3, 2, 5, 4]) == (60, None)
    assert next_interval(60, 60, [0] * 9) == (60, None)
    assert next_interval(60, 60, [0] * 15, window=5)[0] == 120                # 창은 설정 가능 — 최신 5회만 본다
    # 사람이 정한 주기는 어느 쪽으로도 건드리지 않는다
    assert next_interval(30, 60, [0] * 10, locked=True) == (30, None)
    assert next_interval(480, 60, [40] * 10, locked=True) == (480, None)


def test_title_unescapes_double_encoded_entities():
    # korea.kr은 og:title을 "&amp;middot;"로 내보내 파서가 "&middot;"를 돌려준다 → 한 번 더 풀어 "·"
    html = ('<html><head><title>x</title><meta property="og:type" content="article">'
            '<meta property="og:title" content="문체부와 전남, 문화&amp;middot;체육&amp;middot;관광 협력 &amp;amp; 논의">'
            '<meta property="article:published_time" content="2026-10-17T12:00:00+09:00"></head><body><p>본문</p></body></html>')
    art = extract_article(html, "https://www.korea.kr/briefing/pressReleaseView.do?newsId=1")
    assert art.title == "문체부와 전남, 문화·체육·관광 협력 & 논의"


def test_plain_403_is_not_a_site_block_but_home_403_is():
    """추측 경로(/rss/)의 403은 그 경로만 거부된 것(HTTP_4XX, 비차단). 홈이 403이면 사이트 차단(BOT_CHALLENGE)."""
    from sensing_worker.jobs import probe_site

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.path == "/rss/":
            return httpx.Response(403, text="<html><title>주간조선</title><body>forbidden</body></html>")
        return httpx.Response(200, text="<html><body>" + "<p>home</p>" * 5 + "</body></html>")

    f = _fetcher(handler)
    with pytest.raises(CrawlFailure) as e:
        f.get("https://site.example/rss/")
    assert e.value.code == Code.HTTP_4XX and e.value.http_status == 403 and not e.value.blocking
    assert f.get("https://site.example/").status == 200

    def home_forbidden(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(403, text="<html><body>forbidden</body></html>")

    with pytest.raises(CrawlFailure) as e:
        probe_site(_fetcher(home_forbidden), "site.example", "https://site.example")
    assert e.value.code == Code.BOT_CHALLENGE and e.value.blocking and e.value.stage == "PROBE"
# ---------------------------------------------------------------------- 링크 모음(포털) 페이지 → 언론사 확장

PORTAL_URL = "https://stand.example.com/"
PORTAL_DOMAIN = "stand.example.com"


def portal_html(outlets: int = 20, extra: str = "") -> str:
    """뉴스스탠드처럼 다른 호스트의 언론사 로고(이미지 링크)를 모아 둔 홈. 같은 사이트 메뉴와 SNS 링크도 섞여 있다."""
    logos = "".join(f'<li><a href="https://www.press{i:02d}.co.kr/"><img alt="프레스{i:02d}일보"></a></li>' for i in range(outlets))
    nav = ('<a href="/about">소개</a><a href="/notice/list?page=2">공지</a><a href="https://help.stand.example.com/">도움말</a>'
           '<a href="https://www.naver.com/">네이버</a><a href="https://www.facebook.com/stand">페이스북</a>'
           '<a href="https://www.youtube.com/@stand">유튜브</a><a href="https://apps.apple.com/app/1">앱</a>'
           '<a href="http://127.0.0.1:8080/">내부</a><a href="http://localhost/">로컬</a><a href="mailto:a@b.c">메일</a>')
    return f"<html><body><nav>{nav}</nav><ul>{logos}</ul>{extra}</body></html>"


def test_portal_outlets_are_off_site_media_hosts_without_denied_or_internal_ones():
    from sensing_worker.portal import is_portal, outlet_candidates
    html = portal_html(20, extra='<a href="https://www.press03.co.kr/economy">프레스03일보 경제</a>'
                                 '<a href="https://www.press03.co.kr/">프레스03일보</a>')
    outlets = outlet_candidates(html, PORTAL_URL, PORTAL_DOMAIN)
    domains = [o.domain for o in outlets]
    assert len(domains) == 20 and len(set(domains)) == 20                      # 같은 도메인은 한 번 (중복 제거)
    assert domains[0] == "press03.co.kr"                                       # 가장 많이 가리킨 매체가 먼저
    assert outlets[0].name == "프레스03일보" and outlets[0].base_url == "https://www.press03.co.kr"  # 이미지 alt가 매체명
    assert "stand.example.com" not in domains and "help.stand.example.com" not in domains  # 같은 사이트 제외
    for bad in ("naver.com", "facebook.com", "youtube.com", "apple.com", "apps.apple.com", "127.0.0.1", "localhost"):
        assert bad not in domains
    assert is_portal(html, PORTAL_URL, PORTAL_DOMAIN, outlets)


def test_portal_requires_media_hint_unless_known_news_portal():
    from sensing_worker.portal import outlet_candidates
    # 매체 단서가 없는 외부 링크(쇼핑몰·블로그)는 후보가 아니다
    html = '<html><body>' + "".join(f'<a href="https://shop{i}.example.org/">쇼핑 {i}</a>' for i in range(12)) + '</body></html>'
    assert outlet_candidates(html, PORTAL_URL, PORTAL_DOMAIN) == []
    # 호스트에 news·daily 같은 단서가 있으면 링크 텍스트가 없어도 된다
    html = '<html><body><a href="https://m.dailyfoo.com/"></a><a href="https://newsbar.kr/">바로가기</a></body></html>'
    assert [o.domain for o in outlet_candidates(html, PORTAL_URL, PORTAL_DOMAIN)] == ["m.dailyfoo.com", "newsbar.kr"]
    # 알려진 뉴스 포털(뉴스스탠드)은 외부 링크를 모두 언론사 후보로 본다
    html = '<html><body>' + "".join(f'<a href="https://www.outlet{i}.com/"></a>' for i in range(3)) + '</body></html>'
    assert outlet_candidates(html, PORTAL_URL, PORTAL_DOMAIN) == []
    assert len(outlet_candidates(html, "https://newsstand.naver.com/", "newsstand.naver.com")) == 3


def test_is_portal_false_for_few_outlets_or_a_news_home_with_many_own_articles():
    from sensing_worker.portal import detect, is_portal, outlet_candidates
    # 언론사 9곳뿐이면 모음 페이지가 아니다
    few = portal_html(9)
    assert not is_portal(few, PORTAL_URL, PORTAL_DOMAIN, outlet_candidates(few, PORTAL_URL, PORTAL_DOMAIN))
    assert detect(few, PORTAL_URL, PORTAL_DOMAIN) == []
    # 언론사 홈: 제휴 매체 12곳을 링크하지만 자기 기사 링크가 훨씬 많다
    own = "".join(f'<li><a href="/news/articleView.html?idxno={i}">기사 제목 {i}번</a></li>' for i in range(100, 140))
    news_home = portal_html(12, extra=f"<ul>{own}</ul>")
    cands = outlet_candidates(news_home, PORTAL_URL, PORTAL_DOMAIN)
    assert len(cands) == 12
    assert not is_portal(news_home, PORTAL_URL, PORTAL_DOMAIN, cands)
    assert detect(news_home, PORTAL_URL, PORTAL_DOMAIN) == []
    # 모음 페이지: 외부 매체 링크 20개 > 자기 기사형 링크 0개
    assert len(detect(portal_html(20), PORTAL_URL, PORTAL_DOMAIN)) == 20


def test_portal_registrable_mirrors_backend_url_normalizer():
    from sensing_worker.portal import registrable
    assert registrable("https://www.chosun.com:443/x?y=1") == ("chosun.com", "https://www.chosun.com")
    assert registrable("http://news.example.com:8080/") == ("news.example.com", "http://news.example.com:8080")
    for bad in ("http://127.0.0.1/", "http://10.0.0.1/", "http://localhost/", "http://host.internal/", "ftp://a.com/",
                "https://user:pw@a.com/", "http://nodot/", "http://127.1/", "https://[::1]/"):
        assert registrable(bad) is None, bad


def test_portal_reads_press_list_embedded_in_inline_script():
    """뉴스스탠드 실제 홈: 매체 목록이 <a>가 아니라 인라인 스크립트의 JSON(pressList: nm·home·shcutUrl)에 있다."""
    from sensing_worker.portal import detect, outlet_candidates, script_links
    items = ",".join(
        f'{{"id":"{200 + i}","nm":"매체{i}","home":"http:\\/\\/www.press{i:02d}.co.kr\\/","shcutTitl":"매체{i}",'
        f'"shcutUrl":"http:\\/\\/www.press{i:02d}.co.kr\\/","logo":"up\\/2025\\/nsd{i}.png",'
        f'"ombs":"http:\\/\\/cafe.naver.com\\/navernewscast\\/menu\\/{i}"}}' for i in range(12))
    html = (f'<html><body><a href="http://www.naver.com">NAVER</a><a href="http://www.navercorp.com/">NAVER Corp.</a>'
            f'<script type="text/javascript">var pressList = [{items}];</script>'
            f'<script src="/js/c/nsd.min.js"></script></body></html>')
    found = script_links('var x = [{"nm":"MBC","home":"http:\\/\\/imnews.imbc.com\\/","logo":"a.png"}];')
    assert found == [("http://imnews.imbc.com/", "MBC")]
    outlets = outlet_candidates(html, "https://newsstand.naver.com/", "newsstand.naver.com")
    assert [o.domain for o in outlets][:3] == ["press00.co.kr", "press01.co.kr", "press02.co.kr"] and len(outlets) == 12
    assert outlets[0].name == "매체0" and outlets[0].base_url == "http://www.press00.co.kr"
    assert not any(o.domain.endswith(("naver.com", "navercorp.com")) for o in outlets)   # 카페·네이버 자체는 제외
    assert len(detect(html, "https://newsstand.naver.com/", "newsstand.naver.com")) == 12


def test_portal_message_and_batch_id():
    from sensing_worker.jobs import portal_batch_id, portal_message
    assert portal_batch_id(7) == "portal:7"
    assert portal_message(12, "portal:7") == "링크 모음 페이지입니다. 이 페이지가 가리키는 언론사 12곳을 대신 등록했습니다. [묶음 portal:7]"
    assert portal_message(2, "portal:7", duplicates=1, over_cap=3).endswith("(이미 등록된 1곳 제외, 상한을 넘은 3곳 제외) [묶음 portal:7]")


def test_naver_enabled_reads_watch_mode_and_defaults_to_naver_for_old_configs():
    from sensing_worker.jobs import naver_enabled
    assert naver_enabled({"kind": "WATCH", "config": {"naver": False, "keywords": ["a"]}}) is False
    assert naver_enabled({"kind": "WATCH", "config": {"naver": True}}) is True
    assert naver_enabled({"kind": "WATCH", "config": {"keywords": ["a"]}}) is True     # V13 이전 감시 = 네이버 모드
    assert naver_enabled({"kind": "WATCH", "config": None}) is True
    assert naver_enabled({"kind": "SITE", "config": {"naver": False}}) is True         # 감시가 아니면 해당 없음


def test_watch_keywords_match_crawled_documents():
    """긁어 온 기사도 키워드 감시에 대조한다: 대소문자·공백 무시, 제외어는 제목 기준, 감시당 처음 걸린 검색어 하나."""
    from sensing_worker.watchmatch import from_configs, match_keywords
    watches = from_configs([
        {"id": 10, "config": {"keywords": ["메타넷", "메타넷글로벌"], "exclude": ["채용"]}},
        {"id": 11, "config": {"keywords": ["반도체"], "exclude": []}},
        {"id": 12, "config": {"keywords": [], "exclude": []}},            # 키워드 없는 감시는 뺀다
    ])
    assert [w.source_id for w in watches] == [10, 11]
    assert match_keywords("메타넷글로벌, 반도체 공장 투자", None, watches) == [(10, "메타넷"), (11, "반도체")]
    assert match_keywords("Metanet 신규 채용", "메타넷이 뽑는다", watches) == []           # 제외어가 제목에
    assert match_keywords("경제 소식", "본문에 반도체 수출이 늘었다", watches) == [(11, "반도체")]   # 본문도 본다
    assert match_keywords("메타 넷 소식", None, watches) == []                           # 띄어쓰기가 다르면 다른 말
    assert match_keywords(None, None, watches) == []
