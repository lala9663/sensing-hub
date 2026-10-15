from datetime import datetime, timedelta, timezone

import httpx
import pytest

from sensing_worker.codes import Code, CrawlFailure
from sensing_worker.config import Settings
from sensing_worker.discovery import Discoverer, canonical_article_url, matches, signature
from sensing_worker.evidence import EvidenceStore
from sensing_worker.extract import Article, extract_article
from sensing_worker.fetcher import PoliteFetcher
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
