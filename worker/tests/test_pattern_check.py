"""누락 점검(pattern_check): 공개 목록의 기사 주소 중 켜진 종류에 맞지 않는 비율."""

from types import SimpleNamespace

import pytest

from sensing_worker.codes import Code, CrawlFailure
from sensing_worker.discovery import pattern_from_signature
from sensing_worker.pattern_check import FEED, LIST, SITEMAP, measure, near, sitemap_reference


def legacy(sig: str) -> dict:
    return {"article_pattern": pattern_from_signature(sig), "hubs": ["https://x"]}


def test_measure_counts_feed_urls_the_pattern_misses():
    """동아일보 설정 v1: 사회면 패턴 하나로 전체 RSS를 읽는다."""
    base = "https://www.donga.com/news"
    urls = ([f"{base}/Society/article/all/20261021/1348805{i:02d}/1" for i in range(3)]
            + [f"{base}/Politics/article/all/20261021/1348811{i:02d}/1" for i in range(5)]
            + [f"{base}/Economy/article/all/20261021/1348809{i:02d}/1" for i in range(2)]
            + ["https://www.donga.com/news/Politics/List", "https://other.example/news/1/2/3",
               f"{base}/Society/article/all/20261021/134880500/1"])                         # 중복은 한 번
    m = measure([(u, FEED) for u in urls], legacy("/news/Society/article/all/{n}/{n}/{n}"), "donga.com")
    assert (m.article, m.matched, m.off, m.unmatched) == (10, 3, 0, 7)
    assert m.ratio == 0.7
    assert m.top[0] == {"signature": "/news/Politics/article/all/{n}/{n}/{n}", "count": 5,
                        "example": f"{base}/Politics/article/all/20261021/134881100/1"}


def test_measure_separates_kinds_turned_off():
    config = {"article_patterns": [
        {**pattern_from_signature("/news/{*}/{n}"), "name": "기사", "enabled": True, "hubs": []},
        {**pattern_from_signature("/photo/{n}"), "name": "포토", "enabled": False, "hubs": []}]}
    entries = [(f"https://m.com/news/a/{i}", SITEMAP) for i in range(4)] + [(f"https://m.com/photo/{i}", SITEMAP)
                                                                          for i in range(2)]
    m = measure(entries, config, "m.com")
    assert (m.article, m.matched, m.off, m.unmatched, m.ratio) == (6, 4, 2, 0, 0.0)


def test_measure_counts_only_html_links_near_an_enabled_kind():
    """수원시 소식: 홈·섹션 링크에는 메뉴(?p=58)·목록(mode=blog)이 섞인다. mode 없는 상세 링크만 누락이다."""
    config = legacy("/?mode=*&p={n}&reqIdx={n}&viewMode=*")
    b = "https://news.suwon.go.kr/"
    entries = ([(f"{b}?mode=blog&p=22&reqIdx=20261020092514{i:04d}&viewMode=view", LIST) for i in range(4)]
               + [(f"{b}?p=35&viewMode=view&reqIdx=20261021100444{i:04d}", LIST) for i in range(2)]
               + [(f"{b}?p={i}", LIST) for i in range(50, 60)] + [(f"{b}?p=22&mode=blog", LIST)])
    m = measure(entries, config, "news.suwon.go.kr")
    assert (m.article, m.matched, m.unmatched) == (6, 4, 2)
    assert m.top[0]["signature"] == "/?p={n}&reqIdx={n}&viewMode=*"
    # 경로로 가리키는 종류는 앞 경로가 같아야 가깝다
    kind = pattern_from_signature("/news/{*}/{n}/{n}/{n}/{n}")
    assert near("https://www.seoul.co.kr/news/economy/industry/2026/10/21/1", kind)
    assert not near("https://www.seoul.co.kr/company/history/2026", kind)
    assert not near("https://x.com/a/1", pattern_from_signature("/{*}/{n}"))       # 앞 경로가 없는 종류는 HTML에서 세지 않는다


class _Page(SimpleNamespace):
    pass


def _sitemap(*locs: str, index: bool = False) -> str:
    tag, item = ("sitemapindex", "sitemap") if index else ("urlset", "url")
    return f"<{tag}>" + "".join(f"<{item}><loc>{u}</loc><lastmod>2026-10-21</lastmod></{item}>" for u in locs) + f"</{tag}>"


def test_sitemap_reference_picks_one_article_sitemap_within_budget():
    pages = {
        "https://s.com/sitemap-images.xml": _sitemap("https://s.com/i/1.jpg"),
        "https://s.com/hub.xml": _sitemap("https://s.com/news/a/1"),
        "https://s.com/broken.xml": CrawlFailure(Code.HTTP_4XX),
        "https://s.com/index.xml": _sitemap("https://s.com/sm-2026-10.xml", "https://s.com/sm-2026-09.xml", index=True),
        "https://s.com/sm-2026-10.xml": _sitemap("https://s.com/news/a/1", "https://s.com/news/b/c/2", "https://o.com/x/3"),
    }
    asked = []

    def get(url):
        asked.append(url)
        page = pages[url]
        if isinstance(page, Exception):
            raise page
        return _Page(text=page)

    sm, urls = sitemap_reference(get, ["https://s.com/sitemap-images.xml", "https://s.com/hub.xml",
                                       "https://s.com/broken.xml", "https://s.com/index.xml"],
                                 "s.com", skip={"https://s.com/hub.xml"})
    assert sm == "https://s.com/index.xml"
    assert sorted(urls) == ["https://s.com/news/a/1", "https://s.com/news/b/c/2"]
    # 이미지 사이트맵·허브로 이미 연 사이트맵은 열지 않고, 실패한 후보 뒤에 색인 + 최신 하위 하나만 연다 (3회)
    assert asked == ["https://s.com/broken.xml", "https://s.com/index.xml", "https://s.com/sm-2026-10.xml"]

    with pytest.raises(CrawlFailure) as e:                                          # 차단 신호는 그대로 올린다
        sitemap_reference(lambda url: (_ for _ in ()).throw(CrawlFailure(Code.RATE_LIMITED)),
                          ["https://s.com/a.xml"], "s.com", set())
    assert e.value.code == Code.RATE_LIMITED
    assert sitemap_reference(get, [], "s.com", set()) == (None, [])
