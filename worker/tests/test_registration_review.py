"""자동 등록이 관리자 확인(REVIEW)으로 빠지던 실제 사이트 사례를 고정하는 단위 테스트 (2026-10-19).

- 한겨레21: 비표준 발행일 메타(h:published_time="202610160849"), "등록 2026-10-16 08:49" 줄, 꼬리의 "등록·발행일자 2011년"
- 주간한국: 본문 전체가 <header> 안에 있는 ndsoft 페이지, 몰려 올라오는 [부고] 단신
- 수원시 뉴스: 메뉴 "카드뉴스"가 "ì¹´ëë´ì¤"로 깨지던 문자 인코딩 판정
픽스처(tests/fixtures)는 실제 페이지에서 필요한 부분만 잘라 둔 것이다.
"""

from __future__ import annotations

import pathlib
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from bs4 import BeautifulSoup

from sensing_worker import extract
from sensing_worker.codes import Code, CrawlFailure
from sensing_worker.config import Settings
from sensing_worker.discovery import Discoverer, DiscoveryResult
from sensing_worker.extract import _body_fallback, _parse_date, extract_article
from sensing_worker.fetcher import PoliteFetcher, _decode
from sensing_worker.jobs import check_quality
from sensing_worker.policy import is_routine_notice

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
KST = timezone(timedelta(hours=9))


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ------------------------------------------------------------------ 1. 발행일 (한겨레21)

def test_h21_date_comes_from_nonstandard_meta_not_footer():
    art = extract_article(fixture("h21_article.html"), "https://h21.hani.co.kr/arti/society/environment/60053.html")
    assert art.published_at == datetime(2026, 10, 16, 8, 49, tzinfo=KST)
    assert art.title == "그 많던 벌레는 어디로 갔을까?"
    assert art.body_length >= 200 and art.is_article_like


def test_h21_date_falls_back_to_register_label_without_meta():
    html = fixture("h21_article.html").replace('name="h:publish"', 'name="x"').replace('name="h:published_time"', 'name="y"')
    art = extract_article(html, "https://h21.hani.co.kr/arti/society/environment/60053.html")
    # "등록 2026-10-16 08:49" (수정 시각 10-17이 아니라). 꼬리의 "등록·발행일자 : 2011년 7월 19일"이 아니라
    assert art.published_at == datetime(2026, 10, 16, 8, 49, tzinfo=KST)


def test_site_footer_registration_date_is_never_the_article_date():
    soup = BeautifulSoup(fixture("h21_article.html"), "lxml")
    soup.select_one(".arti-date").decompose()
    for m in soup.find_all("meta", attrs={"name": True}):
        m.decompose()
    art = extract_article(str(soup), "https://h21.hani.co.kr/arti/society/environment/60053.html")
    assert art.published_at is None


def test_article_footer_inside_article_still_counts():
    html = ("<html><body><article><h1>제목입니다 제목</h1><p>" + "본문 " * 80 + "</p>"
            "<footer class='article-footer'>입력 2026.10.15 09:30</footer></article>"
            "<footer class='site-footer'>등록일 2010.01.01</footer></body></html>")
    assert extract_article(html, "https://x.example/a/1").published_at == datetime(2026, 10, 15, 9, 30, tzinfo=KST)


@pytest.mark.parametrize("key,value,expected", [
    ("h:published_time", "202610160849", datetime(2026, 10, 16, 8, 49, tzinfo=KST)),
    ("dable:published_time", "2026-10-16T08:49:00+09:00", datetime(2026, 10, 16, 8, 49, tzinfo=KST)),
    ("publish_date", "20261016", datetime(2026, 10, 16, tzinfo=KST)),
    ("DC.date.issued", "2026-10-16", datetime(2026, 10, 16, tzinfo=KST)),
])
def test_nonstandard_publish_meta_keys(key, value, expected):
    html = f"<html><head><meta name='{key}' content='{value}'></head><body><p>본문</p></body></html>"
    assert extract_article(html, "https://x.example/a/1").published_at == expected


def test_modified_time_meta_is_not_a_publish_date():
    html = "<html><head><meta property='h:modified_time' content='202610170907'></head><body></body></html>"
    assert extract_article(html, "https://x.example/a/1").published_at is None


def test_compact_date_parsing():
    assert _parse_date("20261016084900") == datetime(2026, 10, 16, 8, 49, tzinfo=KST)
    assert _parse_date("202613010000") is None   # 13월: 날짜가 아니다


# ------------------------------------------------------------------ 2. 본문 (주간한국)

def test_body_fallback_keeps_content_inside_header():
    soup = BeautifulSoup(fixture("hankooki_article.html"), "lxml")
    body = _body_fallback(soup)
    assert body.startswith("[주간한국 이주선 기자]") and "네 번째 문단" in body
    assert len(body) >= 200
    assert "페이스북" not in body and "경제" not in body.split("\n")[0]   # 머리말 메뉴·공유 단추는 빠진다


def test_hankooki_article_without_trafilatura(monkeypatch):
    monkeypatch.setattr(extract, "trafilatura", None)   # trafilatura가 포기한 경우와 같다
    art = extract_article(fixture("hankooki_article.html"), "https://weekly.hankooki.com/news/articleView.html?idxno=7189466")
    assert art.body_length >= 200
    assert art.published_at == datetime(2026, 10, 18, 22, 8, 53, tzinfo=KST)


def test_chrome_without_body_is_still_removed():
    html = ("<html><body><form><nav>메뉴 하나 메뉴 둘</nav></form><header>사이트 머리말 " + "광고 " * 40 + "</header>"
            "<div class='article_body'>" + "진짜 본문 " * 30 + "</div></body></html>")
    assert _body_fallback(BeautifulSoup(html, "lxml")).startswith("진짜 본문")


def test_routine_notices_are_recognised():
    obit = extract_article(fixture("hankooki_obituary.html"), "https://weekly.hankooki.com/news/articleView.html?idxno=7189457")
    assert is_routine_notice(obit) and obit.body_length < 200
    normal = extract_article(fixture("hankooki_article.html"), "https://weekly.hankooki.com/news/articleView.html?idxno=7189466")
    assert not is_routine_notice(normal)
    for title in ("[인사] 가나다은행", "【부음】 홍길동씨", "[포토] 가을 하늘", "[오늘의 운세] 10월 19일"):
        assert is_routine_notice(extract.Article(url="u", title=title))
    for title in ("인사 혁신 방안 발표", "[단독] 부고 문자 사기 기승", "[국감2026] 은행장 출석"):
        assert not is_routine_notice(extract.Article(url="u", title=title))


class _Pages:
    """check_quality용 가짜 fetcher: 주소 → 픽스처 HTML."""

    def __init__(self, routes: dict[str, str]):
        self.routes = routes
        self.hits: list[str] = []

    def get(self, url: str):
        self.hits.append(url)
        if url not in self.routes:
            raise CrawlFailure(Code.HTTP_4XX, url=url, http_status=404)
        return type("P", (), {"text": self.routes[url], "url": url})()


def _fresh(html: str) -> str:
    # 픽스처 발행일(2026-10-18)이 "최근 30일" 밖으로 밀려도 시험이 깨지지 않게 오늘 날짜로 바꾼다
    today = datetime.now(KST).strftime("%Y-%m-%dT%H:%M:%S+09:00")
    return html.replace("2026-10-18T22:08:53+09:00", today).replace("2026-10-18T17:16:01+09:00", today)


def _result(urls: list[str]) -> DiscoveryResult:
    return DiscoveryResult(method="RSS", hubs=["https://weekly.hankooki.com/rss"], article_pattern={"path": "x"},
                           sample_urls=urls)


def test_quality_sample_skips_obituaries_and_draws_more_candidates():
    base = "https://weekly.hankooki.com/news/articleView.html?idxno="
    urls = [base + str(i) for i in range(1, 11)]
    obit, normal = _fresh(fixture("hankooki_obituary.html")), _fresh(fixture("hankooki_article.html"))
    pages = _Pages({u: (obit if i < 3 else normal) for i, u in enumerate(urls)})   # 최신 3건이 부고
    detail = check_quality(pages, _result(urls), "https://weekly.hankooki.com", "NEWS")
    assert detail.startswith("샘플 5건 중 5건 통과") and "단신 3건" in detail
    assert pages.hits == urls[:8]   # 단신 3건만큼 3건 더 열었다


def test_quality_sample_without_notices_opens_only_five():
    base = "https://weekly.hankooki.com/news/articleView.html?idxno="
    urls = [base + str(i) for i in range(1, 11)]
    pages = _Pages({u: _fresh(fixture("hankooki_article.html")) for u in urls})
    assert check_quality(pages, _result(urls), "https://weekly.hankooki.com", "NEWS") == "샘플 5건 중 5건 통과"
    assert pages.hits == urls[:5]


def test_quality_sample_of_only_notices_still_fails():
    urls = [f"https://weekly.hankooki.com/news/articleView.html?idxno={i}" for i in range(1, 11)]
    pages = _Pages({u: _fresh(fixture("hankooki_obituary.html")) for u in urls})
    with pytest.raises(CrawlFailure) as e:
        check_quality(pages, _result(urls), "https://weekly.hankooki.com", "NEWS")
    assert e.value.code == Code.QUALITY_FAILED


# ------------------------------------------------------------------ 3. 문자 인코딩 (수원시 뉴스)

SUWON_MENU = ('<html lang="ko"><head><meta http-equiv="Content-Type" content="text/html; charset=utf-8" /></head><body>'
              '<ul><li><a href="/?p=18">웹 진</a></li><li><a href="/?p=85">카드뉴스</a></li>'
              '<li><a href="/?p=22">분야별 뉴스</a></li><li><a href="/?p=115">의회소식</a></li></ul></body></html>')


def _resp(body: bytes, content_type: str) -> httpx.Response:
    return httpx.Response(200, headers={"content-type": content_type}, content=body)


@pytest.mark.parametrize("content_type", [
    "text/html; charset=utf-8", "text/html; charset=ISO-8859-1", "text/html; charset=windows-1252", "text/html",
    "text/html; charset=euc-kr",   # 헤더와 meta가 서로 다른 서버
])
def test_utf8_page_decodes_whatever_the_header_says(content_type):
    body = SUWON_MENU.encode("utf-8")
    text = _decode(body, _resp(body, content_type))
    assert "카드뉴스" in text and "ì¹´" not in text


def test_meta_charset_is_used_when_header_has_none():
    html = '<html><head><meta charset="euc-kr"></head><body><a href="/?p=1">보도자료 똠방각하</a></body></html>'
    body = html.encode("cp949")   # "똠"은 EUC-KR에 없고 CP949(확장 완성형)에만 있다
    assert "보도자료 똠방각하" in _decode(body, _resp(body, "text/html"))


def test_euc_kr_header_with_cp949_bytes():
    body = "<html><body>공지사항 똠</body></html>".encode("cp949")
    assert "공지사항 똠" in _decode(body, _resp(body, "text/html; charset=EUC-KR"))


def test_real_latin1_page_stays_latin1():
    body = "<html><body>Café résumé</body></html>".encode("latin-1")
    assert "Café résumé" in _decode(body, _resp(body, "text/html; charset=ISO-8859-1"))


def test_mostly_utf8_page_with_a_stray_byte_is_not_read_as_cp949():
    body = ("<html><body>" + "카드뉴스 의회소식 " * 50 + "</body></html>").encode("utf-8") + b"\xff"
    assert "카드뉴스" in _decode(body, _resp(body, "text/html"))                  # 선언 없음
    assert "카드뉴스" in _decode(body, _resp(body, "text/html; charset=utf-8"))   # 선언 있음


def test_section_names_are_not_mojibake_end_to_end():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        # 헤더는 Latin-1인데 본문은 UTF-8인 서버
        return httpx.Response(200, headers={"content-type": "text/html; charset=ISO-8859-1"},
                              content=SUWON_MENU.encode("utf-8"))

    f = PoliteFetcher(Settings(min_interval_seconds=0, allow_private_hosts=True), transport=httpx.MockTransport(handler))
    page = f.get("https://news.suwon.go.kr/")
    names = [s["name"] for s in Discoverer(f, "news.suwon.go.kr", "https://news.suwon.go.kr")._sections(page.text, page.url)]
    assert "카드뉴스" in names and "의회소식" in names
    assert not any(ch in "".join(names) for ch in "ìëÃ")
