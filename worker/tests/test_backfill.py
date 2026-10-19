"""과거 기사 채우기(backfill.py) 단위 테스트: 보관 사이트맵 고르기(gzip 포함), 목록 쪽 번호 찾기, 기간 판정, 기사 판정."""

from __future__ import annotations

import gzip
from datetime import date, timedelta

import httpx
import pytest

from sensing_worker import backfill as bf
from sensing_worker.codes import Code, CrawlFailure
from sensing_worker.config import Settings
from sensing_worker.discovery import pattern_from_signature
from sensing_worker.fetcher import PoliteFetcher

TODAY = date(2026, 10, 19)


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
    # 조사한 사이트들의 이름 꼴: 5년 단위(주간동아), 연도별(매일신문 1993.xml·2024_1.xml), 일별(20261019.xml, 2026/1019.xml)
    assert bf.sitemap_span("https://weekly.donga.com/sitemap/weekly-sitemap-2000-2005.xml.gz", TODAY) == (
        date(2000, 1, 1), date(2005, 12, 31))
    assert bf.sitemap_span("https://www.imaeil.com/sitemap/1993.xml", TODAY) == (date(1993, 1, 1), date(1993, 12, 31))
    assert bf.sitemap_span("https://www.imaeil.com/sitemap/2024_1.xml", TODAY) == (date(2024, 1, 1), date(2024, 12, 31))
    assert bf.sitemap_span("https://www.koreaherald.com/sitemap/20261019.xml", TODAY) == (date(2026, 10, 19),) * 2
    assert bf.sitemap_span("https://www.hankyung.com/sitemap/2026/1019.xml", TODAY) == (date(2026, 10, 19),) * 2
    assert bf.sitemap_span("https://www.mbn.co.kr/sitemap/mbn_all_sitemap549.xml", TODAY) is None


def test_date_inside_article_url_wins_over_lastmod():
    assert bf.url_day("https://www.donga.com/news/Politics/article/all/19961015/7212345/1", TODAY) == date(1996, 10, 15)
    assert bf.url_day("https://blog.example/2026/09/15/chip-plant/", TODAY) == date(2026, 9, 15)
    assert bf.url_day("https://n.example/2026-09-15-chip-plant", TODAY) == date(2026, 9, 15)
    # 식별자·쿼리·미래 날짜는 날짜가 아니다
    assert bf.url_day("https://www.mk.co.kr/news/economy/11412345", TODAY) is None
    assert bf.url_day("https://www.x.example/news/articleView.html?idxno=20240115", TODAY) is None
    assert bf.url_day("https://www.x.example/news/20301231/1", TODAY) is None
    # 1996 파일에 나중 수정일(2024 lastmod)이 섞여도 주소 안 날짜를 쓴다
    span = bf.sitemap_span("https://image.donga.com/sitemap/donga-sitemap-199609-199610.xml.gz", TODAY)
    entry = bf.SitemapEntry("https://www.donga.com/news/Politics/article/all/19961015/7212345/1", lastmod="2024-03-02")
    assert bf.resolve_entry(entry, span) == (date(1996, 10, 15), True)
    assert bf.resolve_entry(bf.SitemapEntry("https://k.example/view.do?id=1", published="2026-07-01T09:00:00+09:00"),
                            None) == (date(2026, 7, 1), True)
    # 주소·발행일에 날짜가 없으면 lastmod (그보다 이른 글이라는 뜻) — 파일 이름의 기간 안으로 자른다
    assert bf.resolve_entry(bf.SitemapEntry("https://k.example/view.do?id=2", lastmod="2024-03-02"), span) == (
        date(1996, 10, 31), False)
    assert bf.resolve_entry(bf.SitemapEntry("https://k.example/view.do?id=3"), span) == (date(1996, 10, 31), False)
    assert bf.resolve_entry(bf.SitemapEntry("https://k.example/view.do?id=4", lastmod="2026-09-02"), None) == (
        date(2026, 9, 2), False)


# ---------------------------------------------------------------------- 기간 묶음

def test_segments_are_aligned_two_month_blocks_newest_first():
    assert bf.segment_start(date(2026, 7, 19), 2, 0) == date(2026, 7, 1)
    assert bf.segment_start(date(2026, 8, 31), 2, 0) == date(2026, 7, 1)
    assert bf.segment_start(date(2026, 2, 3), 3, 0) == date(2026, 1, 1)
    assert bf.plan_segments(date(2026, 7, 1), date(2026, 10, 19), 2, 0) == [
        (date(2026, 9, 1), date(2026, 10, 19)), (date(2026, 7, 1), date(2026, 8, 31))]
    # 10년이면 묶음 60개, 해를 넘는 묶음도 단위대로
    ten_years = bf.plan_segments(date(2016, 11, 1), date(2026, 10, 19), 2, 0)
    assert len(ten_years) == 60 and ten_years[-1] == (date(2016, 11, 1), date(2016, 12, 31))
    assert bf.plan_segments(date(2025, 12, 1), date(2026, 2, 10), 3, 2) == [
        (date(2025, 12, 1), date(2026, 2, 10))]                        # 12~2월이 한 묶음 (석 달, 12월 시작)
    assert bf.segment_label(date(2026, 9, 1), date(2026, 10, 19)) == "2026-09~10"
    assert bf.segment_label(date(2026, 9, 1), date(2026, 9, 30)) == "2026-09"
    assert bf.segment_label(date(2025, 12, 1), date(2026, 2, 28)) == "2025-12~2026-02"


def test_segment_unit_follows_archive_file_names():
    def spans(urls):
        return [bf.sitemap_span(u, TODAY) for u in urls]
    donga = [f"https://image.donga.com/sitemap/donga/donga-sitemap-{y}{m:02d}-{y}{m + 1:02d}.xml.gz"
             for y in (2025, 2026) for m in range(1, 12, 2)]
    assert bf.detect_unit(spans(donga)) == (2, 0)                      # 두 달 파일 하나 = 묶음 하나 (1~2월, 3~4월 …)
    odd = [f"https://x.example/s-{y}{m:02d}-{y if m < 12 else y + 1}{m % 12 + 1:02d}.xml" for y in (2025,) for m in (2, 4, 6, 8)]
    assert bf.detect_unit(spans(odd)) == (2, 1)                        # 2~3월, 4~5월 … 단위면 그 시작 달
    monthly = [f"https://x.example/sitemap-2026-{m:02d}.xml" for m in range(1, 10)]
    assert bf.detect_unit(spans(monthly)) == (1, 0)
    daily = [f"https://www.koreaherald.com/sitemap/202610{d:02d}.xml" for d in range(1, 20)]
    assert bf.detect_unit(spans(daily)) == (1, 0)                      # 하루 파일은 한 달 묶음
    yearly = [f"https://www.imaeil.com/sitemap/{y}.xml" for y in range(1993, 2026)]
    assert bf.detect_unit(spans(yearly)) == (2, 0)                     # 연도별 파일은 기본 두 달로 나눠 한 파일을 함께 읽는다
    assert bf.detect_unit(spans(["https://weekly.donga.com/s/weekly-sitemap-2000-2005.xml.gz"])) == (2, 0)
    assert bf.detect_unit([], 1, 0) == (1, 0)


DONGA_INDEX = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://image.donga.com/sitemap/donga-sitemap-199609-199610.xml.gz</loc><lastmod>2019-01-01</lastmod></sitemap>
  <sitemap><loc>https://image.donga.com/sitemap/donga-sitemap-202603-202604.xml.gz</loc><lastmod>2026-04-30</lastmod></sitemap>
  <sitemap><loc>https://image.donga.com/sitemap/donga-sitemap-202605-202606.xml.gz</loc><lastmod>2026-06-30</lastmod></sitemap>
  <sitemap><loc>https://image.donga.com/sitemap/donga-sitemap-202607-202608.xml.gz</loc><lastmod>2026-08-31</lastmod></sitemap>
  <sitemap><loc>https://image.donga.com/sitemap/donga-sitemap-202609-202610.xml.gz</loc><lastmod>2026-10-19</lastmod></sitemap>
</sitemapindex>"""


def file_rows(entries, depth=1):
    """backfill_sitemap 행처럼 (DB 없이 고르기 규칙을 본다)."""
    return [{**r, "is_index": None, "read_from": None, "read_at": None, "obs_from": None, "obs_to": None, "error": None}
            for r in bf.sitemap_child_rows(entries, depth, TODAY)]


def read(row, date_from, obs=None, is_index=False):
    row.update({"read_at": "now", "read_from": date_from, "is_index": is_index,
                "obs_from": obs[0] if obs else None, "obs_to": obs[1] if obs else None})


def test_each_segment_reads_only_the_archive_file_for_its_months_newest_first():
    is_index, entries = bf.parse_sitemap(DONGA_INDEX)
    assert is_index and len(entries) == 5
    rows = file_rows(entries)
    f = date(2026, 7, 1)
    sep_oct, jul_aug = (date(2026, 9, 1), date(2026, 10, 19)), (date(2026, 7, 1), date(2026, 8, 31))
    pick = bf.pick_sitemap(rows, *sep_oct, False)
    assert pick["url"].endswith("donga-sitemap-202609-202610.xml.gz")
    read(pick, f)
    assert bf.pick_sitemap(rows, *sep_oct, False) is None and bf.sitemaps_cover(rows, *sep_oct)
    pick = bf.pick_sitemap(rows, *jul_aug, False)
    assert pick["url"].endswith("donga-sitemap-202607-202608.xml.gz")
    read(pick, f)
    assert bf.pick_sitemap(rows, *jul_aug, False) is None
    # 기간을 두 달 늘리면 그 묶음의 파일만 더 읽는다 (이미 읽은 파일은 그 달 글이 없으니 다시 읽지 않는다)
    may_jun = (date(2026, 5, 1), date(2026, 6, 30))
    assert bf.pick_sitemap(rows, *may_jun, False)["url"].endswith("donga-sitemap-202605-202606.xml.gz")


def test_undated_files_are_read_by_lastmod_and_cover_only_as_far_as_their_oldest_article():
    rows = file_rows([bf.SitemapEntry("https://www.korea.kr/sitemap_old.xml", lastmod="2026-05-01"),
                      bf.SitemapEntry("https://www.korea.kr/sitemap_policy.xml", lastmod="2026-10-18T09:00:00+09:00"),
                      bf.SitemapEntry("https://www.korea.kr/sitemap_speech.xml")])
    seg = (date(2026, 7, 1), date(2026, 8, 31))
    seen = []
    while (pick := bf.pick_sitemap(rows, *seg, False)) is not None:
        seen.append(pick["url"].rsplit("/", 1)[-1])
        read(pick, seg[0], (date(2026, 7, 20), date(2026, 10, 18)))
    assert seen == ["sitemap_policy.xml", "sitemap_speech.xml"]     # 기간 시작 전에 마지막으로 바뀐 파일은 읽지 않는다
    assert not bf.sitemaps_cover(rows, *seg)                          # 가장 이른 글이 07-20이라 7월 초는 목록 페이지로
    assert bf.sitemaps_cover(rows, date(2026, 7, 19), date(2026, 8, 31))
    assert bf.pick_sitemap(file_rows([bf.SitemapEntry("https://www.korea.kr/sitemap_x.xml")]), *seg, True) is None  # 상한


def test_numbered_files_are_binary_searched_by_the_dates_inside():
    # mbn_all_sitemap0~549.xml처럼 이름에 날짜가 없고 번호만 다르다: 0번이 가장 최근, 파일 하나에 닷새치
    n = 550
    rows = file_rows([bf.SitemapEntry(f"https://www.mbn.co.kr/sitemap/mbn_all_sitemap{i}.xml") for i in range(n)])
    assert all(r["grp"] for r in rows)
    newest = date(2026, 10, 19)

    def obs(i):
        end = newest - timedelta(days=i * 5)
        return end - timedelta(days=4), end

    seg = (date(2026, 1, 1), date(2026, 2, 28))       # 59일 → 파일 열두세 개
    reads = []
    while (pick := bf.pick_sitemap(rows, *seg, True)) is not None:   # 상한에 닿아도 번호 묶음은 이분 탐색으로 읽는다
        reads.append(pick["num"])
        read(pick, seg[0], obs(pick["num"]))
    overlapping = {i for i in range(n) if obs(i)[0] <= seg[1] and obs(i)[1] >= seg[0]}
    assert overlapping <= set(reads)                  # 묶음에 걸치는 파일은 모두 읽었다
    assert len(reads) <= len(overlapping) + 2 + 12    # 양 끝 + 이분 탐색 (log2 550 ≈ 9)
    assert bf.sitemaps_cover(rows, *seg)
    # 번호가 클수록 최근이어도 같다
    rows = file_rows([bf.SitemapEntry(f"https://www.dailian.co.kr/sitemap_www_{i}.xml") for i in range(20)])
    reads = []
    while (pick := bf.pick_sitemap(rows, *seg, False)) is not None:
        reads.append(pick["num"])
        read(pick, seg[0], obs(19 - pick["num"]))
    assert {19 - i for i in range(20) if obs(i)[0] <= seg[1] and obs(i)[1] >= seg[0]} <= set(reads)


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
    assert bf.resolve_entry(entries[0], None) == (date(2026, 9, 15), True)


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
