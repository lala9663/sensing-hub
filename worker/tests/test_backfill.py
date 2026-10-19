"""과거 기사 채우기(backfill.py) 단위 테스트: 보관 사이트맵 고르기(gzip 포함), 목록 쪽 번호 찾기, 기간 판정, 기사 판정."""

from __future__ import annotations

import gzip
from datetime import date

import httpx
import pytest

from sensing_worker import backfill as bf
from sensing_worker.codes import Code, CrawlFailure
from sensing_worker.config import Settings
from sensing_worker.discovery import pattern_from_signature
from sensing_worker.fetcher import PoliteFetcher

TODAY = date(2026, 10, 19)
FROM, TO = date(2026, 7, 19), date(2026, 10, 19)


# ---------------------------------------------------------------------- 보관 사이트맵 고르기

def test_sitemap_span_reads_dates_in_archive_file_names():
    assert bf.sitemap_span("https://image.donga.com/sitemap/donga-sitemap-202609-202610.xml.gz", TODAY) == (
        date(2026, 9, 1), date(2026, 10, 31))
    assert bf.sitemap_span("https://image.donga.com/sitemap/donga-sitemap-199609-199610.xml.gz", TODAY) == (
        date(1996, 9, 1), date(1996, 10, 31))
    assert bf.sitemap_span("https://x.example/sitemap-2026-07-15.xml", TODAY) == (date(2026, 7, 15), date(2026, 7, 15))
    assert bf.sitemap_span("https://x.example/sitemap/2026/02/news.xml", TODAY) == (date(2026, 2, 1), date(2026, 2, 28))
    assert bf.sitemap_span("https://x.example/sitemap.xml?yyyymm=202512", TODAY) == (date(2025, 12, 1), date(2025, 12, 31))
    assert bf.sitemap_span("https://x.example/sitemap-2025.xml", TODAY) == (date(2025, 1, 1), date(2025, 12, 31))
    # 날짜가 아닌 숫자: 올해보다 뒤의 "연도"는 쪽 번호, 긴 숫자는 식별자
    assert bf.sitemap_span("https://x.example/sitemap-2048.xml", TODAY) is None
    assert bf.sitemap_span("https://x.example/sitemap-12345678.xml", TODAY) is None
    assert bf.sitemap_span("https://www.korea.kr/sitemap_policy.xml", TODAY) is None


DONGA_INDEX = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://image.donga.com/sitemap/donga-sitemap-199609-199610.xml.gz</loc><lastmod>2019-01-01</lastmod></sitemap>
  <sitemap><loc>https://image.donga.com/sitemap/donga-sitemap-202603-202604.xml.gz</loc><lastmod>2026-04-30</lastmod></sitemap>
  <sitemap><loc>https://image.donga.com/sitemap/donga-sitemap-202605-202606.xml.gz</loc><lastmod>2026-06-30</lastmod></sitemap>
  <sitemap><loc>https://image.donga.com/sitemap/donga-sitemap-202607-202608.xml.gz</loc><lastmod>2026-08-31</lastmod></sitemap>
  <sitemap><loc>https://image.donga.com/sitemap/donga-sitemap-202609-202610.xml.gz</loc><lastmod>2026-10-19</lastmod></sitemap>
</sitemapindex>"""


def test_index_children_overlapping_the_period_are_chosen_newest_first():
    is_index, entries = bf.parse_sitemap(DONGA_INDEX)
    assert is_index and len(entries) == 5
    kids = [e for e in entries if bf.child_wanted(e, FROM, TO, TODAY)]
    kids.sort(key=lambda e: bf.child_order(e, TODAY), reverse=True)
    assert [e.loc.rsplit("/", 1)[-1] for e in kids] == ["donga-sitemap-202609-202610.xml.gz",
                                                        "donga-sitemap-202607-202608.xml.gz"]


def test_undated_children_are_chosen_by_lastmod():
    old = bf.SitemapEntry("https://www.korea.kr/sitemap_old.xml", lastmod="2026-05-01")
    recent = bf.SitemapEntry("https://www.korea.kr/sitemap_policy.xml", lastmod="2026-10-18T09:00:00+09:00")
    unknown = bf.SitemapEntry("https://www.korea.kr/sitemap_speech.xml")
    assert not bf.child_wanted(old, FROM, TO, TODAY)       # 기간 시작 전에 마지막으로 바뀐 파일의 글은 모두 그 전 글
    assert bf.child_wanted(recent, FROM, TO, TODAY)
    assert bf.child_wanted(unknown, FROM, TO, TODAY)


def test_urlset_entries_with_cdata_entities_and_news_dates():
    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" xmlns:news="http://www.google.com/schemas/sitemap-news/0.9"
            xmlns:image="http://www.google.com/schemas/sitemap-image/1.1">
      <url><loc><![CDATA[https://www.donga.com/news/Politics/article/all/20260915/1]]></loc>
           <lastmod>2026-09-15T10:00:00+09:00</lastmod></url>
      <url><loc>https://www.x.example/view.do?a=1&amp;b=2</loc>
           <news:news><news:publication_date>2026-07-01T09:00:00+09:00</news:publication_date></news:news></url>
      <url><loc>https://www.x.example/photo/3</loc><image:image><image:loc>https://img.x.example/3.jpg</image:loc></image:image></url>
    </urlset>"""
    is_index, entries = bf.parse_sitemap(xml)
    assert not is_index
    assert [e.loc for e in entries] == ["https://www.donga.com/news/Politics/article/all/20260915/1",
                                        "https://www.x.example/view.do?a=1&b=2", "https://www.x.example/photo/3"]
    assert entries[0].lastmod == "2026-09-15T10:00:00+09:00" and entries[0].published is None
    assert entries[1].published == "2026-07-01T09:00:00+09:00"
    assert bf.entry_day(entries[0]) == date(2026, 9, 15)


def test_entry_period_uses_publication_date_strictly_and_lastmod_with_slack():
    f, t = date(2026, 7, 1), date(2026, 8, 31)
    assert bf.entry_in_period(bf.SitemapEntry("u", published="2026-07-01T00:10:00+09:00"), f, t)
    assert not bf.entry_in_period(bf.SitemapEntry("u", published="2026-06-30T23:50:00+09:00"), f, t)
    assert not bf.entry_in_period(bf.SitemapEntry("u", published="2026-09-01", lastmod="2026-08-01"), f, t)
    # lastmod는 고친 시각일 수 있다: 끝 뒤 3일까지는 후보로 두고 상세의 발행일로 확인한다
    assert bf.entry_in_period(bf.SitemapEntry("u", lastmod="2026-09-02"), f, t)
    assert not bf.entry_in_period(bf.SitemapEntry("u", lastmod="2026-09-05"), f, t)
    assert not bf.entry_in_period(bf.SitemapEntry("u", lastmod="2026-06-29"), f, t)
    assert bf.entry_in_period(bf.SitemapEntry("u"), f, t)    # 날짜 없음 → 상세에서 확인


def test_kst_day_handles_iso_utc_and_korean_list_dates():
    assert bf.kst_day("2026-10-19T23:30:00Z") == date(2026, 10, 20)        # UTC 밤 → 한국 다음 날
    assert bf.kst_day("2026-10-19") == date(2026, 10, 19)
    assert bf.kst_day("20261015") == date(2026, 10, 15)
    assert bf.kst_day("2026. 10. 15") == date(2026, 10, 15)
    assert bf.kst_day("2026.08.21 10:00") == date(2026, 8, 21)
    assert bf.kst_day("어제") is None and bf.kst_day(None) is None


# ---------------------------------------------------------------------- gzip 사이트맵 받기

URLSET = ('<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
          '<url><loc>https://www.archive.test/news/1</loc><lastmod>2026-09-01</lastmod></url></urlset>')


def _fetcher(handler) -> PoliteFetcher:
    return PoliteFetcher(Settings(min_interval_seconds=0, allow_private_hosts=True, max_retries=1),
                         transport=httpx.MockTransport(handler))


def test_get_sitemap_unzips_gz_files_and_reads_plain_and_encoded_ones():
    gz = gzip.compress(URLSET.encode("utf-8"))

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private/\n")
        if path.endswith(".xml.gz"):        # 파일 자체가 gzip (Content-Encoding 없음)
            return httpx.Response(200, content=gz, headers={"content-type": "application/x-gzip"})
        if path == "/encoded.xml":          # 서버가 전송 압축만 한 경우 (httpx가 푼다)
            return httpx.Response(200, content=gz, headers={"content-type": "application/xml", "content-encoding": "gzip"})
        if path == "/euckr.xml":
            body = '<?xml version="1.0" encoding="EUC-KR"?><urlset><url><loc>https://www.archive.test/news/한글</loc></url></urlset>'
            return httpx.Response(200, content=body.encode("cp949"), headers={"content-type": "text/xml"})
        return httpx.Response(200, content=URLSET.encode("utf-8"), headers={"content-type": "application/xml"})

    f = _fetcher(handler)
    for path in ("/sitemap/news-202609-202610.xml.gz", "/encoded.xml", "/sitemap.xml"):
        page = f.get_sitemap("https://www.archive.test" + path)
        is_index, entries = bf.parse_sitemap(page.text)
        assert not is_index and [e.loc for e in entries] == ["https://www.archive.test/news/1"], path
        assert not page.truncated
    assert bf.parse_sitemap(f.get_sitemap("https://www.archive.test/euckr.xml").text)[1][0].loc.endswith("/한글")
    with pytest.raises(CrawlFailure) as e:                    # robots를 평소와 같이 지킨다
        f.get_sitemap("https://www.archive.test/private/sitemap.xml")
    assert e.value.code == Code.ROBOTS_DISALLOWED
    f.close()


def test_get_sitemap_marks_oversized_gzip_as_truncated():
    big = gzip.compress(("<urlset>" + "<url><loc>https://www.archive.test/news/1</loc></url>" * 5000 + "</urlset>").encode())

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, content=big, headers={"content-type": "application/gzip"})

    f = _fetcher(handler)
    page = f.get_sitemap("https://www.archive.test/big.xml.gz", max_bytes=10_000)
    assert page.truncated and len(page.text) <= 10_000
    assert len(bf.parse_sitemap(page.text)[1]) > 100    # 읽은 데까지는 쓴다
    f.close()


# ---------------------------------------------------------------------- 목록 쪽 번호

def test_page_param_from_query_links_ignores_other_boards_and_article_ids():
    html = """<html><body><ul>
      <li><a href="/board/view.do?bbsId=B1&amp;nttId=12">글</a></li><li><a href="/board/view.do?bbsId=B1&amp;nttId=13">글</a></li>
      </ul><a href="/board/list.do?bbsId=B2">다른 게시판</a>
      <div class="paging"><a href="/board/list.do?bbsId=B1&amp;page=2">2</a><a href="?bbsId=B1&amp;page=3">3</a></div></body></html>"""
    assert bf.find_page_param(html, "https://city.example/board/list.do?bbsId=B1", {"nttId"}) == {"kind": "query",
                                                                                                    "name": "page"}


def test_page_param_prefers_known_page_names_over_article_ids():
    html = """<a href="/bbs/board.php?bo_table=news&amp;wr_id=12">글</a><a href="/bbs/board.php?bo_table=news&amp;wr_id=13">글</a>
              <a href="/bbs/board.php?bo_table=news&amp;page=2">2</a>"""
    assert bf.find_page_param(html, "https://g.example/bbs/board.php?bo_table=news")["name"] == "page"
    assert bf.find_page_param(html, "https://g.example/bbs/board.php?bo_table=news", {"wr_id"})["name"] == "page"


def test_page_param_from_egov_script_links_uses_hidden_input():
    html = """<form name="listForm"><input type="hidden" name="pageIndex" value="1"></form>
      <div class="paging"><a href="#" onclick="fn_egov_link_page(2); return false;">2</a>
      <a href="#" onclick="fn_egov_link_page(3); return false;">3</a><a href="javascript:fn_egov_link_page(4);">4</a></div>"""
    assert bf.find_page_param(html, "https://gov.example/bbs/list.do?bbsId=1") == {"kind": "query", "name": "pageIndex"}


def test_page_param_from_wordpress_paths_and_none_without_paging():
    html = '<a href="/category/news/page/2/">2</a><a href="https://blog.example/category/news/page/3/">3</a>'
    param = bf.find_page_param(html, "https://blog.example/category/news/")
    assert param == {"kind": "path", "template": "/category/news/page/{n}"}
    assert bf.page_url("https://blog.example/category/news/", param, 4) == "https://blog.example/category/news/page/4"
    assert bf.find_page_param("<a href='/about'>소개</a>", "https://blog.example/news") is None


def test_page_url_replaces_existing_page_value_and_keeps_board():
    param = {"kind": "query", "name": "page"}
    assert bf.page_url("https://c.example/board/list.do?bbsId=B1&page=1", param, 3) == \
        "https://c.example/board/list.do?bbsId=B1&page=3"
    assert bf.page_url("https://c.example/board/list.do?bbsId=B1", param, 2) == \
        "https://c.example/board/list.do?bbsId=B1&page=2"


# ---------------------------------------------------------------------- 기사 판정 (평소 수집과 같은 설정)

def test_article_kinds_reads_single_pattern_or_enabled_kinds():
    single = {"article_pattern": pattern_from_signature("/news/articleView.html?idxno={n}"), "hubs": ["h1"]}
    kinds = bf.article_kinds(single)
    assert len(kinds) == 1 and kinds[0]["hubs"] == ["h1"] and kinds[0]["name"] is None
    assert bf.match_article("https://www.x.example/news/articleView.html?idxno=5&utm_source=rss", kinds, "x.example") == (
        "https://www.x.example/news/articleView.html?idxno=5", None)
    assert bf.match_article("https://other.example/news/articleView.html?idxno=5", kinds, "x.example") is None

    multi = {"article_pattern": pattern_from_signature("/briefing/pressReleaseView.do?newsId={n}"), "hubs": ["h"],
             "article_patterns": [
                 {**pattern_from_signature("/briefing/pressReleaseView.do?newsId={n}"), "name": "보도자료", "enabled": True},
                 {**pattern_from_signature("/news/policyNewsView.do?newsId={n}"), "name": "정책뉴스", "enabled": True,
                  "hubs": ["policy.xml"]},
                 {**pattern_from_signature("/multi/cartoonView.do?newsId={n}"), "name": "카툰", "enabled": False}]}
    kinds = bf.article_kinds(multi)
    assert [k["name"] for k in kinds] == ["보도자료", "정책뉴스"]
    assert kinds[0]["hubs"] == ["h"] and kinds[1]["hubs"] == ["policy.xml"]
    assert bf.match_article("https://www.korea.kr/news/policyNewsView.do?newsId=148", kinds, "korea.kr")[1] == "정책뉴스"
    assert bf.match_article("https://www.korea.kr/multi/cartoonView.do?newsId=1", kinds, "korea.kr") is None
