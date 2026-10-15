"""자동 탐색: 도메인만 받아 새 기사가 나오는 입구(허브)와 기사 URL 패턴을 찾는다.

앞 단계에서 찾으면 뒤로 가지 않는다.
1. RSS/Atom  2. 사이트맵  3. 홈·섹션 링크 패턴 판정 (CMS 패턴 라이브러리 우선)
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urljoin, urlsplit

from bs4 import BeautifulSoup

from .codes import Code, CrawlFailure
from .extract import feed_links, links, links_with_dates
from .fetcher import PoliteFetcher

COMMON_FEED_PATHS = ("/rss", "/rss.xml", "/feed", "/rss/allArticle.xml", "/rss/S1N1.xml", "/rssIndex.html")
# 섹션(목록) 페이지로 보이는 주소
LIST_HINT = re.compile(r"(list|section|category|catalog|board(?!.*(wr_id|idx|seq|no=|view|detail))|bbs.*list)", re.I)
# 기사 패턴 후보에서 제외할 목록형 경로
LIST_PATH = re.compile(r"(list|section|category|catalog)", re.I)
# 추적용·목록 쪽수 파라미터: 기사를 식별하지 않으므로 서명·정규 주소에서 뺀다
TRACKING_PARAM = re.compile(r"^(utm_.*|fbclid|gclid|ref|referrer|from|sns|share|page|pageindex|pageno|pagenum)$", re.I)
# 주소에 list 같은 단서가 없어도 메뉴 이름이 이러면 게시판(섹션)으로 본다 (공공기관 CMS: /kor/article/ATCL…)
BOARD_NAME_HINT = re.compile(r"(보도|공지|알림|소식|뉴스|자료|브리핑|공고|정책|행사|채용|입찰|발표|news|notice|press)", re.I)
BOARD_NAME_FIRST = re.compile(r"(보도|press)", re.I)
BOARD_NAME_SECOND = re.compile(r"(공지|알림|소식|뉴스|공고|news|notice)", re.I)
SKIP_PATH = re.compile(r"(login|logout|join|member|search|print|rss|mailto|share|comment|reply|tag/|"
                       r"\.(jpg|jpeg|png|gif|css|js|pdf|hwp|hwpx|zip|xlsx?|docx?)$)", re.I)
# 포토·영상·연예 등 텍스트가 거의 없는 섹션은 기본 제외
EXCLUDED_SECTION_WORDS = ("포토", "사진", "영상", "동영상", "tv", "연예", "운세", "만평", "photo", "video", "gallery")

# 국내에서 흔한 CMS의 기사 상세 패턴 (서명 형태)
KNOWN_ARTICLE_SIGNATURES = (
    "/news/articleView.html?idxno={n}",           # ndsoft
    "/bbs/board.php?bo_table=*&wr_id={n}",        # 그누보드
)


@dataclass
class DiscoveryResult:
    method: str                                    # RSS | SITEMAP | STATIC
    hubs: list[str]
    article_pattern: dict                          # {"path": regex, "query_keys": [...]}
    sections: list[dict] = field(default_factory=list)
    excluded_sections: list[str] = field(default_factory=list)
    sample_urls: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    domain: str = ""                               # 실제 수집 대상 호스트 (홈이 다른 호스트로 넘긴 경우 그곳)
    sample_dates: dict[str, str] = field(default_factory=dict)  # 샘플 URL → 목록·피드에 보인 날짜 (상세에 없을 때 씀)

    def to_config(self) -> dict:
        return {"method": self.method, "hubs": self.hubs, "article_pattern": self.article_pattern,
                "sections": self.sections, "excluded_sections": self.excluded_sections,
                "site_domain": self.domain}


# ---------------------------------------------------------------------- URL 서명

ID_SEGMENT_RE = re.compile(r"^(?=.*\d.*\d.*\d)[A-Za-z0-9_-]{6,}$")  # 숫자 3개 이상 섞인 6자 이상 토큰


def _generalize_segment(seg: str) -> str:
    if seg.isdigit():
        return "{n}"
    if ID_SEGMENT_RE.match(seg) and not seg.isalpha():
        return "{id}"
    return re.sub(r"\d+", "{n}", seg)


def signature(url: str) -> str:
    """URL을 패턴 서명으로 바꾼다. 숫자는 {n}, 영숫자 식별자는 {id}, 쿼리 값은 숫자면 {n} 아니면 *."""
    parts = urlsplit(url)
    path = "/".join(_generalize_segment(seg) for seg in (parts.path or "/").split("/"))
    q = sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not TRACKING_PARAM.match(k))
    if q:
        qs = "&".join(f"{k}={'{n}' if v.isdigit() else '*'}" for k, v in q)
        return f"{path}?{qs}"
    return path


def pattern_from_signature(sig: str) -> dict:
    path, _, query = sig.partition("?")
    regex = ("^" + re.escape(path).replace(re.escape("{n}"), r"\d+")
             .replace(re.escape("{id}"), r"[A-Za-z0-9_-]*\d[A-Za-z0-9_-]*") + "$")
    keys = [kv.split("=", 1)[0] for kv in query.split("&") if kv]
    id_keys = [kv.split("=", 1)[0] for kv in query.split("&") if kv.endswith("={n}")]
    return {"path": regex, "query_keys": keys, "id_keys": id_keys, "signature": sig}


def matches(url: str, pattern: dict) -> bool:
    parts = urlsplit(url)
    if not re.match(pattern["path"], parts.path or "/"):
        return False
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    for key in pattern.get("id_keys", []):
        if not query.get(key, "").isdigit():
            return False
    return all(k in query for k in pattern.get("query_keys", []))


def canonical_article_url(url: str, pattern: dict) -> str:
    """기사 URL에서 추적용 파라미터를 지워 같은 기사가 한 번만 저장되게 한다."""
    parts = urlsplit(url)
    keep = set(pattern.get("query_keys", []))
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k in keep]
    qs = "&".join(f"{k}={v}" for k, v in sorted(query))
    path = re.sub(r";jsessionid=[^/?]*", "", parts.path, flags=re.I)
    return f"{parts.scheme}://{parts.netloc}{path}" + (f"?{qs}" if qs else "")


def same_site(url: str, domain: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host == domain or host.endswith("." + domain)


# ---------------------------------------------------------------------- 피드·사이트맵

def parse_feed(xml: str) -> list[str]:
    return [u for u, _ in parse_feed_entries(xml)]


def parse_feed_entries(xml: str) -> list[tuple[str, str | None]]:
    """(링크, 발행일 문자열 | None)"""
    soup = BeautifulSoup(xml, "xml")
    out = []
    for item in soup.find_all(["item", "entry"]):
        link = item.find("link")
        if link is None:
            continue
        href = link.get("href") or link.get_text(strip=True)
        if not href:
            continue
        date = item.find(["pubDate", "published", "updated", "dc:date"])
        out.append((href.strip(), date.get_text(strip=True) if date else None))
    return out


def parse_sitemap(xml: str) -> tuple[list[str], list[str]]:
    """(기사 후보 URL, 하위 사이트맵 URL). 각각 lastmod가 최신인 순서."""
    urls, children = parse_sitemap_entries(xml)
    return [u for u, _ in urls], [u for u, _ in children]


def parse_sitemap_entries(xml: str) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """(loc, lastmod) 목록을 최신 순으로. lastmod가 없는 항목은 문서 순서를 지키되 뒤로 보낸다."""
    soup = BeautifulSoup(xml, "xml")
    entries = []
    for i, node in enumerate(soup.find_all(["url", "sitemap"])):
        loc = node.find("loc")
        if loc is None:
            continue
        lastmod = node.find("lastmod")
        entries.append((loc.get_text(strip=True), lastmod.get_text(strip=True) if lastmod else "", i))
    entries.sort(key=lambda e: (e[1] == "", -_lastmod_key(e[1]), e[2]))
    out = [(u, lm) for u, lm, _ in entries]
    if soup.find("sitemapindex"):
        return [], out
    return out, []


def _lastmod_key(lastmod: str) -> float:
    """lastmod 문자열을 비교 가능한 숫자로. 형식이 이상하면 0 (가장 오래된 것으로 취급)."""
    digits = re.sub(r"\D", "", lastmod)[:14]
    return float(digits.ljust(14, "0")) if digits else 0.0


def looks_like_sitemap(text: str) -> bool:
    head = text[:3000].lower()
    return "<urlset" in head or "<sitemapindex" in head


def sitemap_urls(get, xml: str, max_children: int = 3) -> list[str]:
    """사이트맵(또는 인덱스)에서 기사 후보 URL을 최신 순으로. 인덱스면 lastmod가 최신인 하위 사이트맵 몇 개만 연다.

    get: url -> Page | None (탐색은 페이지 한도를 지키는 Discoverer._get, 수집은 fetcher.get)
    """
    urls, children = parse_sitemap_entries(xml)
    out = list(urls)
    for child, _ in children[:max_children]:
        page = get(child)
        if page:
            out += parse_sitemap_entries(page.text)[0]
    out.sort(key=lambda e: (e[1] == "", -_lastmod_key(e[1])))
    return list(dict.fromkeys(u for u, _ in out))


def looks_like_feed(text: str) -> bool:
    head = text[:500].lower()
    return "<rss" in head or "<feed" in head or ("<?xml" in head and ("<channel" in text[:3000].lower()))


# ---------------------------------------------------------------------- 탐색

class Discoverer:
    def __init__(self, fetcher: PoliteFetcher, domain: str, base_url: str, max_pages: int = 40):
        self.f = fetcher
        self.domain = domain
        self.base_url = base_url
        self.max_pages = max_pages
        self.pages = 0
        self.notes: list[str] = []
        self.hints: dict[str, str] = {}  # 후보 URL → 목록·피드에서 본 날짜

    def _get(self, url: str):
        if self.pages >= self.max_pages:
            return None
        self.pages += 1
        try:
            return self.f.get(url)
        except CrawlFailure as e:
            if e.blocking:
                raise
            self.notes.append(f"{url}: {e.code.value}")
            return None

    def run(self) -> DiscoveryResult:
        """가장 유력한 후보 하나. 없으면 DISCOVERY_FAILED."""
        for result in self.candidates():
            return result
        raise CrawlFailure(Code.DISCOVERY_FAILED, stage="DISCOVERY", url=self.base_url)

    def candidates(self) -> Iterator[DiscoveryResult]:
        """후보를 유력한 순서로 낸다 (RSS → 사이트맵 → 홈·섹션 링크, 단계마다 패턴 몇 개).

        호출자(jobs.discover)가 샘플 품질 검사로 걸러 첫 합격을 쓴다. 사이트맵에 게시판 목록만 있고
        글은 섹션 안에만 보이는 공공기관 CMS처럼, 첫 후보가 틀렸을 때 다음 후보로 넘어가기 위한 구조다.
        """
        home = self.f.get(self.base_url)  # 홈은 반드시 열려야 한다
        self.pages += 1
        if not same_site(home.url, self.domain):
            # 등록한 도메인이 다른 호스트로 넘긴다 (motie.go.kr → motir.go.kr). 넘어간 곳을 사이트로 본다
            host = (urlsplit(home.url).hostname or "").lower()
            self.notes.append(f"home redirects to {host}")
            self.domain = host[4:] if host.startswith("www.") else host
            self.base_url = f"{urlsplit(home.url).scheme}://{urlsplit(home.url).netloc}"
        home_entries = [(u, d) for u, d in links_with_dates(home.text, home.url) if same_site(u, self.domain)]
        self._remember(home_entries)
        home_links = [u for u, _ in home_entries]
        sections = self._sections(home.text, home.url)
        excluded = self._excluded(sections)

        # 1. RSS
        feeds = self._find_feeds(home.text, home.url)
        if feeds:
            items = []
            for feed in feeds[:5]:
                page = self._get(feed)
                if page and looks_like_feed(page.text):
                    entries = [(u, d) for u, d in parse_feed_entries(page.text) if same_site(u, self.domain)]
                    self._remember(entries)
                    items += [u for u, _ in entries]
            for pattern in self._ranked_patterns(items, 2):
                yield self._result("RSS", feeds[:5], pattern, sections, excluded, items)

        # 2. 사이트맵 (최신 순: 인덱스면 lastmod가 최신인 하위 사이트맵만 연다)
        for sm in self.f.sitemaps(self.base_url)[:3]:
            page = self._get(sm)
            if not page:
                continue
            urls = [u for u in sitemap_urls(self._get, page.text) if same_site(u, self.domain)]
            # 최근 항목 안에서 패턴을 고른다: 오래된 대용량 카테고리가 최신 기사 패턴을 가리지 않게
            for pattern in self._ranked_patterns(urls[:500], 2):
                yield self._result("SITEMAP", [sm], pattern, sections, excluded, urls)

        # 3. 홈·섹션 링크
        candidates = list(home_links)
        kept = [s for s in sections if s["name"] not in excluded]
        for sec in kept[:8]:
            page = self._get(sec["url"])
            if page:
                entries = [(u, d) for u, d in links_with_dates(page.text, page.url) if same_site(u, self.domain)]
                self._remember(entries)
                candidates += [u for u, _ in entries]
        for pattern in self._ranked_patterns(candidates, 3):
            # 기사 주소 자체는 허브가 아니다
            hubs = [home.url] + [s["url"] for s in kept[:15] if not matches(s["url"], pattern)]
            yield self._result("STATIC", hubs, pattern, sections, excluded, candidates)

    def _remember(self, entries: list[tuple[str, str | None]]) -> None:
        for url, date in entries:
            if date and url not in self.hints:
                self.hints[url] = date

    def _result(self, method: str, hubs: list[str], pattern: dict, sections: list[dict], excluded: list[str],
                urls: list[str]) -> DiscoveryResult:
        samples, dates = self._samples(urls, pattern)
        return DiscoveryResult(method, hubs, pattern, sections, excluded, samples[:10], list(self.notes),
                               self.domain, dates)

    def _samples(self, urls: list[str], pattern: dict) -> tuple[list[str], dict[str, str]]:
        """패턴에 맞는 후보(목록에 날짜가 보이는 것부터)와 그 날짜."""
        seen: set[str] = set()
        hinted, rest, dates = [], [], {}
        for u in urls:
            if not matches(u, pattern):
                continue
            c = canonical_article_url(u, pattern)
            if c in seen:
                continue
            seen.add(c)
            hint = self.hints.get(u)
            if hint:
                dates[c] = hint
                hinted.append(c)
            else:
                rest.append(c)
        return hinted + rest, dates

    def _find_feeds(self, html: str, base: str) -> list[str]:
        found = [u for u in feed_links(html, base) if same_site(u, self.domain)]
        if found:
            return list(dict.fromkeys(found))
        for path in COMMON_FEED_PATHS:
            page = self._get(urljoin(self.base_url, path))
            if not page:
                continue
            if looks_like_feed(page.text):
                return [page.url]
            if "html" in page.content_type:
                # rssIndex.html 같은 피드 목록 페이지
                xmls = [u for u in links(page.text, page.url)
                        if same_site(u, self.domain) and re.search(r"(\.xml|/rss/)", u, re.I)]
                if xmls:
                    return list(dict.fromkeys(xmls))[:5]
        return []

    def _sections(self, html: str, base: str) -> list[dict]:
        soup = BeautifulSoup(html, "lxml")
        by_url, by_name, seen = [], [], set()
        for a in soup.find_all("a", href=True):
            url = urljoin(base, a["href"]).split("#", 1)[0]
            name = a.get_text(" ", strip=True)
            if (not name or len(name) > 20 or url in seen or not same_site(url, self.domain)
                    or SKIP_PATH.search(urlsplit(url).path)):
                continue
            if LIST_HINT.search(url):
                seen.add(url)
                by_url.append({"url": url, "name": name})
            elif (BOARD_NAME_HINT.search(name) and len(name) <= 10 and not re.search(r"\d{3,}", name)
                    and url.rstrip("/") != self.base_url.rstrip("/")):
                # 메뉴 이름은 짧다. "기사 제목 101번 소식" 같은 글 제목은 섹션이 아니다
                seen.add(url)
                by_name.append({"url": url, "name": name})
        # 이름으로 찾은 섹션은 보도자료 → 공지·소식 → 나머지 순으로 연다 (열어 볼 수 있는 수가 제한돼 있다)
        by_name.sort(key=lambda s: 0 if BOARD_NAME_FIRST.search(s["name"]) else 1 if BOARD_NAME_SECOND.search(s["name"]) else 2)
        return (by_url + by_name)[:40]

    @staticmethod
    def _excluded(sections: list[dict]) -> list[str]:
        return [s["name"] for s in sections if any(w in s["name"].lower() for w in EXCLUDED_SECTION_WORDS)]

    @staticmethod
    def _dominant_pattern(urls: list[str]) -> dict | None:
        ranked = Discoverer._ranked_patterns(urls, 1)
        return ranked[0] if ranked else None

    @staticmethod
    def _ranked_patterns(urls: list[str], limit: int = 3) -> list[dict]:
        """링크 묶음에서 기사 상세로 보이는 패턴을 유력한 순서로 고른다."""
        urls = [u for u in urls if not SKIP_PATH.search(urlsplit(u).path)]
        if not urls:
            return []
        sigs = Counter(signature(u) for u in urls)
        scored: list[tuple[float, str]] = []
        for sig, count in sigs.items():
            if ("{n}" not in sig and "{id}" not in sig) or count < 3:
                continue
            path = sig.split("?", 1)[0]
            if LIST_PATH.search(path) and not any(sig.startswith(k.split("?")[0]) for k in KNOWN_ARTICLE_SIGNATURES):
                continue
            score = float(count)
            if any(_sig_matches_known(sig, known) for known in KNOWN_ARTICLE_SIGNATURES):
                score *= 3
            if re.search(r"(view|article|detail|news|read|show)", sig, re.I):
                score *= 1.5
            scored.append((score, sig))
        scored.sort(reverse=True)
        return [pattern_from_signature(sig) for _, sig in scored[:limit]]


def _sig_matches_known(sig: str, known: str) -> bool:
    regex = "^" + re.escape(known).replace(re.escape("{n}"), r"\{n\}").replace(re.escape("*"), r"[^&]*") + "$"
    return re.match(regex, sig) is not None


def group_by_signature(urls: list[str]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = defaultdict(list)
    for u in urls:
        groups[signature(u)].append(u)
    return groups
