"""robots.txt 판정 (RFC 9309): 가장 길게 맞는 규칙, 같은 길이면 Allow, * 와 $."""

import httpx
import pytest

from sensing_worker.codes import Code, CrawlFailure
from sensing_worker.config import Settings
from sensing_worker.fetcher import PoliteFetcher
from sensing_worker.robots import RobotsRules, normalize

UA = "SensingHubBot/0.1 (+contact: sensing-hub admin)"

# 2026-10-21 https://www.seoul.co.kr/robots.txt 그대로 ("Allow:/"가 맨 앞, 콜론 앞 공백)
SEOUL = ("User-agent: *\nAllow:/\nDisallow : /SpecialEdition/dev/\nDisallow : /xml/\nDisallow : /members/\n\n"
         "Sitemap: https://www.seoul.co.kr/sitemap/sitemap_latestArticle\n"
         "Sitemap: https://www.seoul.co.kr/sitemap/sitemap_section\n"
         "Sitemap: https://www.seoul.co.kr/sitemap/sitemap_dailyArticle\n"
         "Sitemap: https://www.seoul.co.kr/sitemap/sitemap_dailyImages\n\n")


def allowed(robots: str, path: str, ua: str = UA) -> bool:
    return RobotsRules(robots).can_fetch(ua, "https://site.example" + path)


def test_seoul_disallow_after_allow_all_is_respected():
    rp = RobotsRules(SEOUL)
    base = "https://www.seoul.co.kr"
    # urllib.robotparser는 처음 맞는 "Allow: /"로 모두 허용했다
    assert not rp.can_fetch(UA, base + "/xml/rss/google_top.xml")
    assert not rp.can_fetch(UA, base + "/members/login")
    assert not rp.can_fetch(UA, base + "/SpecialEdition/dev/x")
    assert rp.can_fetch(UA, base + "/news/economy/industry/2026/10/21/20261021500123")
    assert rp.can_fetch(UA, base + "/news/politics/2026/10/21/20261021500111")
    assert rp.can_fetch(UA, base + "/")
    assert rp.can_fetch(UA, base + "/sitemap/sitemap_latestArticle")
    assert rp.can_fetch(UA, base + "/SpecialEdition/")          # /SpecialEdition/dev/ 만 막혔다
    assert rp.sitemaps == ["https://www.seoul.co.kr/sitemap/sitemap_latestArticle",
                           "https://www.seoul.co.kr/sitemap/sitemap_section",
                           "https://www.seoul.co.kr/sitemap/sitemap_dailyArticle",
                           "https://www.seoul.co.kr/sitemap/sitemap_dailyImages"]
    assert rp.has_disallow(UA)


@pytest.mark.parametrize("robots,path,ok", [
    # 같은 길이면 Allow (순서와 무관)
    ("User-agent: *\nDisallow: /page\nAllow: /page\n", "/page", True),
    ("User-agent: *\nAllow: /page\nDisallow: /page\n", "/page.html", True),
    # 더 긴 쪽이 이긴다 (순서와 무관)
    ("User-agent: *\nAllow: /p\nDisallow: /\n", "/page", True),
    ("User-agent: *\nAllow: /folder\nDisallow: /folder/page\n", "/folder/page", False),
    ("User-agent: *\nDisallow: /folder/page\nAllow: /folder\n", "/folder/other", True),
    ("User-agent: *\nDisallow: /\nAllow: /news/\n", "/news/1", True),
    ("User-agent: *\nDisallow: /\nAllow: /news/\n", "/sports/1", False),
    # 와일드카드도 패턴 길이로 겨룬다: /*.htm(6) > /page(5), /*.ph(5) = /page(5) → Allow
    ("User-agent: *\nAllow: /page\nDisallow: /*.htm\n", "/page.htm", False),
    ("User-agent: *\nAllow: /page\nDisallow: /*.ph\n", "/page.php5", True),
    # $ 는 경로 끝
    ("User-agent: *\nAllow: /$\nDisallow: /\n", "/", True),
    ("User-agent: *\nAllow: /$\nDisallow: /\n", "/page.htm", False),
    ("User-agent: *\nDisallow: /fish$\n", "/fish", False),
    ("User-agent: *\nDisallow: /fish$\n", "/fish.html", True),
    ("User-agent: *\nDisallow: /*.gif$\n", "/img/a.gif", False),
    ("User-agent: *\nDisallow: /*.gif$\n", "/img/a.gif?w=10", True),
    # * 는 0글자 이상 (중간·끝), 쿼리도 경로에 들어간다
    ("User-agent: *\nDisallow: /fish*\n", "/fishheads/yummy.html", False),
    ("User-agent: *\nDisallow: /*/print/\n", "/news/print/1", False),
    ("User-agent: *\nDisallow: /*/print/\n", "/print/1", True),
    ("User-agent: *\nDisallow: /*?*sort=\n", "/list?page=2&sort=asc", False),
    ("User-agent: *\nDisallow: /*?\n", "/list", True),
    # 빈 Disallow 는 아무것도 막지 않는다
    ("User-agent: *\nDisallow:\n", "/anything", True),
    ("User-agent: *\nDisallow: \nDisallow: /private/\n", "/public", True),
    # $ 가 끝이 아니면 글자 그대로
    ("User-agent: *\nDisallow: /a$b\n", "/a$b/c", False),
    # 패턴이 / 로 시작하지 않아도 경로로 읽는다
    ("User-agent: *\nDisallow: tmp/\n", "/tmp/x", False),
])
def test_longest_match_rules(robots, path, ok):
    assert allowed(robots, path) is ok


def test_empty_disallow_is_allowed_status():
    rp = RobotsRules("User-agent: *\nDisallow:\n")
    assert rp.can_fetch(UA, "https://site.example/")
    assert not rp.has_disallow(UA)


def test_group_selection():
    robots = ("User-agent: *\nDisallow: /private/\n\n"
              "User-agent: GPTBot\nUser-agent: CCBot\nDisallow: /\n\n"
              "User-agent: SensingHubBot\nDisallow: /search\n\n"
              "user-agent: sensinghubbot\nDisallow: /tmp/\n")
    rp = RobotsRules(robots)
    # 이어진 User-agent 줄은 한 그룹
    assert not rp.can_fetch("GPTBot", "https://site.example/a")
    assert not rp.can_fetch("CCBot/2.0", "https://site.example/a")
    # 우리 이름의 그룹들을 합치고, * 그룹은 쓰지 않는다
    assert not rp.can_fetch(UA, "https://site.example/search?q=1")
    assert not rp.can_fetch(UA, "https://site.example/tmp/x")
    assert rp.can_fetch(UA, "https://site.example/private/x")
    # 이름이 없는 봇은 * 그룹
    assert not rp.can_fetch("OtherBot/1.0", "https://site.example/private/x")
    assert rp.can_fetch("OtherBot/1.0", "https://site.example/search")


def test_no_matching_group_allows_everything():
    rp = RobotsRules("User-agent: GPTBot\nDisallow: /\n")
    assert rp.can_fetch(UA, "https://site.example/x")
    assert not rp.has_disallow(UA)


def test_line_format_tolerance():
    robots = ("﻿Disallow: /before-any-group/\n"           # 그룹 밖 규칙은 버린다
              "# 주석\n"
              "USER-AGENT : *   # 끝 주석\n"
              "\n"                                            # 빈 줄은 그룹을 끝내지 않는다
              "DisAllow : /xml/  # rss\n"
              "Crawl-delay: 3\n"
              "Sitemap : https://site.example/sitemap.xml\n")
    rp = RobotsRules(robots)
    assert rp.can_fetch(UA, "https://site.example/before-any-group/x")
    assert not rp.can_fetch(UA, "https://site.example/xml/feed.xml")
    assert rp.crawl_delay(UA) == 3.0
    assert rp.sitemaps == ["https://site.example/sitemap.xml"]


def test_rules_after_a_group_start_a_new_group():
    robots = "User-agent: a\nDisallow: /a/\nUser-agent: *\nDisallow: /star/\n"
    rp = RobotsRules(robots)
    assert rp.can_fetch(UA, "https://site.example/a/x")
    assert not rp.can_fetch(UA, "https://site.example/star/x")
    assert not rp.can_fetch("a/1.0", "https://site.example/a/x")
    assert rp.can_fetch("a/1.0", "https://site.example/star/x")


def test_percent_encoding_is_normalized():
    assert normalize("/%7efoo") == "/~foo"
    assert normalize("/a%2fb") == "/a%2Fb"                    # 예약 글자는 풀지 않는다
    assert normalize("/뉴스") == "/%EB%89%B4%EC%8A%A4"
    assert not allowed("User-agent: *\nDisallow: /%7Efoo/\n", "/~foo/bar")
    assert not allowed("User-agent: *\nDisallow: /~foo/\n", "/%7efoo/bar")
    assert not allowed("User-agent: *\nDisallow: /뉴스/\n", "/%EB%89%B4%EC%8A%A4/1")
    assert not allowed("User-agent: *\nDisallow: /%eb%89%b4%ec%8a%a4/\n", "/뉴스/1")


def test_robots_txt_itself_is_always_allowed():
    assert allowed("User-agent: *\nDisallow: /\n", "/robots.txt")


def _fetcher(robots: str) -> PoliteFetcher:
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=robots)
        return httpx.Response(200, text="<html>ok</html>")

    s = Settings(min_interval_seconds=0, max_retries=1, allow_private_hosts=True)
    return PoliteFetcher(s, transport=httpx.MockTransport(handler))


def test_fetcher_respects_seoul_robots():
    f = _fetcher(SEOUL)
    # 예전 판정은 "disallow:"(공백 없음)를 찾아 ALLOWED였다. 우리 그룹에 Disallow가 있으므로 PARTIAL
    assert f.robots_status("https://www.seoul.co.kr") == "PARTIAL"
    with pytest.raises(CrawlFailure) as e:
        f.get("https://www.seoul.co.kr/xml/rss/google_top.xml")
    assert e.value.code == Code.ROBOTS_DISALLOWED
    assert f.get("https://www.seoul.co.kr/news/economy/2026/10/21/20261021500123").status == 200
    assert f.sitemaps("https://www.seoul.co.kr")[0] == "https://www.seoul.co.kr/sitemap/sitemap_latestArticle"


def test_fetcher_status_disallowed_and_allowed():
    assert _fetcher("User-agent: *\nDisallow: /\n").robots_status("https://a.example") == "DISALLOWED"
    assert _fetcher("User-agent: *\nDisallow: /\nAllow: /$\n").robots_status("https://a.example") == "PARTIAL"
    assert _fetcher("User-agent: *\nAllow: /\n").robots_status("https://a.example") == "ALLOWED"
    # AI 봇만 막은 사이트는 우리에게 PARTIAL이 아니다 (AI 봇 차단은 ai_bots_blocked가 따로 본다)
    f = _fetcher("User-agent: GPTBot\nDisallow: /\n\nUser-agent: *\nAllow: /\n")
    assert f.robots_status("https://a.example") == "ALLOWED"
    assert f.ai_bots_blocked("https://a.example")


def test_recheck_reads_one_robots_per_site_and_flags_blocked_hubs():
    from types import SimpleNamespace

    from sensing_worker.robots_recheck import recheck, render, render_blocked

    hubs = ["https://www.seoul.co.kr/xml/rss/google_top.xml", "https://www.seoul.co.kr/xml/rss/google_politics.xml"]
    config = {"method": "RSS", "hubs": hubs,
              "article_pattern": {"path": r"^/news/[^/]+/\d+/\d+/\d+/\d+$", "query_keys": [], "id_keys": []}}
    docs = [("https://www.seoul.co.kr/news/politics/2026/10/21/20261021500111",)]

    class Conn:
        def execute(self, sql, params=None):
            rows = [(28, "서울신문", "seoul.co.kr", "http://seoul.co.kr", "ALLOWED", 1, config)] if "FROM source" in sql else docs
            return SimpleNamespace(fetchall=lambda: rows)

    asked = []

    def handler(request):
        asked.append(str(request.url))
        return httpx.Response(200, text=SEOUL) if request.url.path == "/robots.txt" else httpx.Response(500)

    f = PoliteFetcher(Settings(min_interval_seconds=0, max_retries=1, allow_private_hosts=True),
                      transport=httpx.MockTransport(handler))
    results, hosts = recheck(Conn(), f, UA)
    # 등록 주소(http://seoul.co.kr)가 아니라 같은 사이트의 수집 호스트 robots.txt 하나만 받는다
    assert asked == ["https://www.seoul.co.kr/robots.txt"] and hosts == 1
    r = results[0]
    assert r.status == "PARTIAL" and len(r.blocked_hubs) == 2 and not r.blocked_articles
    assert len(r.changed) == 2                                  # 예전 파서는 두 허브를 허용했다
    assert r.blocked_hubs[0].rule == "Disallow: /xml/"
    assert "**2/2**" in render(results, len(asked), hosts, "now") and "google_top.xml" in render_blocked(results)
