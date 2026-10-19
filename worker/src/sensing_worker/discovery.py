"""자동 탐색: 도메인만 받아 새 기사가 나오는 입구(허브)와 기사 URL 패턴을 찾는다.

앞 단계에서 찾으면 뒤로 가지 않는다.
1. RSS/Atom  2. 사이트맵  3. 홈·섹션 링크 패턴 판정 (CMS 패턴 라이브러리 우선)
첫 합격 패턴(대표 종류)을 낸 단계에서 다른 상세 URL 모양도 기사 종류 후보로 본다 (rank_kinds, jobs.discover_kinds).
"""

from __future__ import annotations

import re
from collections import ChainMap, Counter, defaultdict
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from html import unescape
from urllib.parse import parse_qsl, urljoin, urlsplit

from bs4 import BeautifulSoup

from .codes import Code, CrawlFailure
from .extract import Article, feed_links, links, links_with_dates
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
    # 추출 셀렉터 {title, date, body, attachment}. 비어 있으면 메타·휴리스틱으로 뽑는다. LLM 레시피가 채운다
    selectors: dict = field(default_factory=dict)
    date_format: str | None = None                 # 상세 페이지 날짜 텍스트의 strptime 형식 (LLM 레시피)
    recipe_source: str = "heuristic"               # heuristic | llm
    recipe_notes: str = ""                         # LLM이 남긴 설명
    llm_model: str | None = None                   # "제공자 모델" (recipe_source=llm일 때)
    # 기사 종류(상세 URL 모양) 목록. 첫 항목이 대표 종류(= article_pattern). jobs.discover_kinds가 채운다
    kinds: list[dict] = field(default_factory=list)

    def to_config(self) -> dict:
        config = {"method": self.method, "hubs": self.hubs, "article_pattern": self.article_pattern,
                  "sections": self.sections, "excluded_sections": self.excluded_sections,
                  "site_domain": self.domain, "selectors": self.selectors, "date_format": self.date_format,
                  "recipe_source": self.recipe_source, "recipe_notes": self.recipe_notes, "llm_model": self.llm_model}
        if self.kinds:
            config["article_patterns"] = self.kinds
        return config


# ---------------------------------------------------------------------- URL 서명

ID_SEGMENT_RE = re.compile(r"^(?=.*\d.*\d.*\d)[A-Za-z0-9_-]{6,}$")  # 숫자 3개 이상 섞인 6자 이상 토큰
# 하이픈으로 이어진 소문자 단어 3개 이상 (samsung-lays-out-ai-memory-roadmap). 영문 매체의 숫자 없는 기사 슬러그
SLUG_SEGMENT_RE = re.compile(r"^(?=.*[a-z])[a-z0-9]+(?:-[a-z0-9]+){2,}$")
SLUG_REGEX = r"[a-z0-9]+(?:-[a-z0-9]+){2,}"
WILDCARD_REGEX = r"[^/]+"                      # {*}: 경로 조각 하나 (섹션 이름처럼 기사마다 다른 고정 단어)
ID_PLACEHOLDERS = ("{n}", "{id}", "{slug}")     # 기사를 구분하는 자리. 이 중 하나는 있어야 기사 패턴이다


def _generalize_segment(seg: str) -> str:
    if seg.isdigit():
        return "{n}"
    if SLUG_SEGMENT_RE.match(seg):
        return "{slug}"
    if ID_SEGMENT_RE.match(seg) and not seg.isalpha():
        return "{id}"
    return re.sub(r"\d+", "{n}", seg)


def has_identifier(sig: str) -> bool:
    return any(p in sig for p in ID_PLACEHOLDERS)


def signature(url: str) -> str:
    """URL을 패턴 서명으로 바꾼다. 숫자는 {n}, 영숫자 식별자는 {id}, 소문자 단어 3개 이상의 슬러그는 {slug},
    쿼리 값은 숫자면 {n} 아니면 *. (서명끼리 합칠 때 생기는 {*}는 signature()가 직접 내지 않는다)"""
    parts = urlsplit(url)
    path = "/".join(_generalize_segment(seg) for seg in (parts.path or "/").split("/"))
    q = sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not TRACKING_PARAM.match(k))
    if q:
        qs = "&".join(f"{k}={'{n}' if v.isdigit() else '*'}" for k, v in q)
        return f"{path}?{qs}"
    return path


def pattern_from_signature(sig: str) -> dict:
    """서명 → 패턴. 쿼리 값이 {n}·*가 아닌 글자 그대로면(bbsId=B01, 게시판별 종류) 그 값이어야 맞는다 (query_values)."""
    path, _, query = sig.partition("?")
    regex = ("^" + re.escape(path).replace(re.escape("{n}"), r"\d+")
             .replace(re.escape("{id}"), r"[A-Za-z0-9_-]*\d[A-Za-z0-9_-]*")
             .replace(re.escape("{slug}"), SLUG_REGEX)
             .replace(re.escape("{*}"), WILDCARD_REGEX) + "$")
    pairs = [kv.partition("=")[::2] for kv in query.split("&") if kv]
    pattern = {"path": regex, "query_keys": [k for k, _ in pairs], "id_keys": [k for k, v in pairs if v == "{n}"],
               "signature": sig}
    values = {k: v for k, v in pairs if v not in ("{n}", "*")}
    if values:
        pattern["query_values"] = values
    return pattern


def matches(url: str, pattern: dict) -> bool:
    parts = urlsplit(url)
    if not re.match(pattern["path"], parts.path or "/"):
        return False
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    for key in pattern.get("id_keys", []):
        if not query.get(key, "").isdigit():
            return False
    for key, value in (pattern.get("query_values") or {}).items():
        if query.get(key) != value:
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


def samples_with_dates(urls: list[str], pattern: dict,
                       hints: Mapping[str, str]) -> tuple[list[str], dict[str, str]]:
    """패턴에 맞는 후보(목록에 날짜가 보이는 것부터)와 그 날짜. hints: 원래 주소 → 목록·피드에서 본 날짜."""
    seen: set[str] = set()
    hinted, rest, dates = [], [], {}
    for u in urls:
        if not matches(u, pattern):
            continue
        c = canonical_article_url(u, pattern)
        if c in seen:
            continue
        seen.add(c)
        hint = hints.get(u)
        if hint:
            dates[c] = hint
            hinted.append(c)
        else:
            rest.append(c)
    return hinted + rest, dates


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
    return [u for u, _ in sitemap_entries(get, xml, max_children)]


def sitemap_entries(get, xml: str, max_children: int = 3) -> list[tuple[str, str | None]]:
    """sitemap_urls와 같되 (주소, lastmod | None). lastmod는 상세 페이지에 발행일이 전혀 없을 때만 쓰는 약한 날짜 힌트다
    (수정일이라 발행일보다 늦을 수 있다 — 정책브리핑 부처 브리핑처럼 상세에 날짜가 없는 종류를 위해)."""
    urls, children = parse_sitemap_entries(xml)
    out = list(urls)
    for child, _ in children[:max_children]:
        page = get(child)
        if page:
            out += parse_sitemap_entries(page.text)[0]
    out.sort(key=lambda e: (e[1] == "", -_lastmod_key(e[1])))
    first: dict[str, str | None] = {}
    for u, lm in out:
        if u not in first:
            first[u] = lm or None
    return list(first.items())


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
        self.weak_hints: dict[str, str] = {}  # 후보 URL → 사이트맵 lastmod (목록·피드 날짜가 없을 때만 쓴다)
        self.home = None                  # 홈 페이지(Page). LLM 레시피 생성이 다시 받지 않고 쓴다
        self.sections: list[dict] = []
        self.excluded: list[str] = []
        # 기사 종류 찾기의 재료: 입구 단계별 (허브, 그 허브에서 본 주소들, 이름표). 사이트맵은 kind_pools가 하위를 열어 만든다
        self.pools: dict[str, list[tuple[str, list[str], str]]] = {"RSS": [], "STATIC": []}
        self._sitemap_pages: dict[str, object] = {}   # 이미 연 사이트맵(색인·하위) 주소 → Page
        self.kind_pages = 0                           # 종류를 찾으려고 더 연 하위 사이트맵 수

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

    def _get_sitemap(self, url: str):
        """사이트맵은 한 번만 받는다 (입구 단계에서 연 하위를 종류 찾기가 다시 받지 않게)."""
        if url not in self._sitemap_pages:
            self._sitemap_pages[url] = self._get(url)
        return self._sitemap_pages[url]

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
        self.home, self.sections, self.excluded = home, sections, excluded

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
                    self.pools["RSS"].append((feed, [u for u, _ in entries], ""))
            for pattern in self._ranked_patterns(items, 2):
                yield self._result("RSS", feeds[:5], pattern, sections, excluded, items)

        # 2. 사이트맵 (최신 순: 인덱스면 lastmod가 최신인 하위 사이트맵만 연다)
        for sm in self.f.sitemaps(self.base_url)[:3]:
            page = self._get_sitemap(sm)
            if not page:
                continue
            entries = [(u, lm) for u, lm in sitemap_entries(self._get_sitemap, page.text) if same_site(u, self.domain)]
            self._remember(entries, weak=True)
            urls = [u for u, _ in entries]
            # 최근 항목 안에서 패턴을 고른다: 오래된 대용량 카테고리가 최신 기사 패턴을 가리지 않게
            for pattern in self._ranked_patterns(urls[:500], 2):
                yield self._result("SITEMAP", [sm], pattern, sections, excluded, urls)

        # 3. 홈·섹션 링크
        candidates = list(home_links)
        self.pools["STATIC"].append((home.url, list(home_links), ""))
        kept = [s for s in sections if s["name"] not in excluded]
        for sec in kept[:8]:
            page = self._get(sec["url"])
            if page:
                entries = [(u, d) for u, d in links_with_dates(page.text, page.url) if same_site(u, self.domain)]
                self._remember(entries)
                candidates += [u for u, _ in entries]
                self.pools["STATIC"].append((sec["url"], [u for u, _ in entries], ""))
        for pattern in self._ranked_patterns(candidates, 3):
            # 기사 주소 자체는 허브가 아니다
            hubs = [home.url] + [s["url"] for s in kept[:15] if not matches(s["url"], pattern)]
            yield self._result("STATIC", hubs, pattern, sections, excluded, candidates)

    def _remember(self, entries: list[tuple[str, str | None]], weak: bool = False) -> None:
        """목록·피드에 보인 날짜를 기억한다. weak: 사이트맵 lastmod — 목록·피드 날짜가 없는 주소에만 쓴다."""
        target = self.weak_hints if weak else self.hints
        for url, date in entries:
            if date and url not in target:
                target[url] = date

    def kind_pools(self, result: DiscoveryResult) -> list[tuple[str, list[str], str]]:
        """result를 낸 입구 단계에서 본 (허브, 그 허브의 주소들, 이름표) 묶음. 기사 종류 후보의 재료다.

        RSS·STATIC은 입구 단계에서 이미 연 피드·홈·섹션 그대로다. 사이트맵은 색인의 하위를 이름 모양마다 하나씩
        (최대 MAX_KIND_SITEMAPS개, 입구 단계에서 연 것은 다시 받지 않는다) 열어 하위별로 묶는다. 이름이 하나뿐인 하위
        (sitemap_policy.xml)는 그 자체가 허브이고 이름표(policy)를 단다. 날짜·쪽수로 나뉜 하위(sitemap-{n}.xml 여럿)는
        새 하위가 계속 생기므로 색인을 허브로 둔다.
        """
        if result.method != "SITEMAP":
            return list(self.pools.get(result.method, []))
        pools: list[tuple[str, list[str], str]] = []
        for root in result.hubs[:1]:
            page = self._get_sitemap(root)
            if not page:
                continue
            urls, children = parse_sitemap_entries(page.text)
            if not children:
                self._remember(urls, weak=True)
                pools.append((root, [u for u, _ in urls if same_site(u, self.domain)], ""))
                continue
            groups: dict[str, list[str]] = {}
            for child, _ in children:
                groups.setdefault(signature(child), []).append(child)
            for group in list(groups.values())[:MAX_KIND_SITEMAPS]:
                child_page = self._get_kind_sitemap(group[0])
                if not child_page:
                    continue
                child_entries = parse_sitemap_entries(child_page.text)[0]
                self._remember(child_entries, weak=True)
                entries = [u for u, _ in child_entries if same_site(u, self.domain)]
                single = len(group) == 1
                pools.append((group[0] if single else root, entries, sitemap_label(group[0]) if single else ""))
        return pools

    def _get_kind_sitemap(self, url: str):
        """종류 찾기용 하위 사이트맵. 입구 단계의 쪽수 한도와 따로 MAX_KIND_SITEMAPS개까지 연다."""
        if url in self._sitemap_pages:
            return self._sitemap_pages[url]
        if self.kind_pages >= MAX_KIND_SITEMAPS:
            return None
        self.kind_pages += 1
        try:
            page = self.f.get(url)
        except CrawlFailure as e:
            if e.blocking:
                raise
            self.notes.append(f"{url}: {e.code.value}")
            page = None
        self._sitemap_pages[url] = page
        return page

    def _result(self, method: str, hubs: list[str], pattern: dict, sections: list[dict], excluded: list[str],
                urls: list[str]) -> DiscoveryResult:
        samples, dates = self._samples(urls, pattern)
        return DiscoveryResult(method, hubs, pattern, sections, excluded, samples[:10], list(self.notes),
                               self.domain, dates)

    def _samples(self, urls: list[str], pattern: dict) -> tuple[list[str], dict[str, str]]:
        return samples_with_dates(urls, pattern, ChainMap(self.hints, self.weak_hints))

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
        article_like = {sig: count for sig, count in sigs.items() if has_identifier(sig) and not _is_list_signature(sig)}
        # 섹션 이름만 다른 같은 형태의 서명(/news/economy/{n} + /news/society/{n})은 {*}로 합친 서명도 후보에 넣는다.
        # 합친 서명은 원래 것들의 횟수를 모두 더해 받으므로 보통 앞에 온다. limit는 원래 서명에만 적용해 병합이
        # 틀려도 전과 같은 후보가 남고, 품질 검사가 그중에서 고른다
        originals = _score_signatures(article_like)[:limit]
        merged = _score_signatures(merge_signatures(article_like))[:MAX_MERGED_PATTERNS]
        ranked = sorted(originals + merged, reverse=True)
        return [pattern_from_signature(sig) for _, sig in ranked]


MAX_MERGED_PATTERNS = 2


def _score_signatures(counts: dict[str, int]) -> list[tuple[float, str]]:
    scored: list[tuple[float, str]] = []
    for sig, count in counts.items():
        if count < 3:
            continue
        score = float(count)
        if any(_sig_matches_known(sig, known) for known in KNOWN_ARTICLE_SIGNATURES):
            score *= 3
        if re.search(r"(view|article|detail|news|read|show)", sig, re.I):
            score *= 1.5
        scored.append((score, sig))
    scored.sort(reverse=True)
    return scored


def _is_list_signature(sig: str) -> bool:
    path = sig.split("?", 1)[0]
    return bool(LIST_PATH.search(path)) and not any(sig.startswith(k.split("?")[0]) for k in KNOWN_ARTICLE_SIGNATURES)


def _is_placeholder_piece(piece: str) -> bool:
    return "{" in piece or piece == ""


def merge_signatures(counts: dict[str, int]) -> dict[str, int]:
    """조각 수가 같고 정확히 한 조각(또는 이어진 두 조각)만 다른 서명 쌍을 {*}로 합친 서명과, 그 서명이 덮는 횟수의 합.

    다른 조각은 양쪽 모두 고정 단어여야 하고(식별자 자리 {n}·{id}·{slug}나 빈 조각이 다르면 합치지 않는다), 섹션 이름은
    기사 식별자보다 앞에 오므로 그 뒤 경로에 식별자 자리가 있어야 한다. /kor/{n}/headquarters + /kor/{n}/sitemap 같은
    메뉴 페이지(번호 뒤에 페이지 이름)나 쿼리에만 식별자가 있는 /a/view.do?id={n} + /b/view.do?id={n}은 합치지 않는다.
    쿼리가 있으면 같아야 한다. 조각이 하나뿐인 /{*}처럼 식별자가 남지 않는 서명은 만들지 않는다.
    예) /news/economy/{n} + /news/society/{n} → /news/{*}/{n}
        /tech-industry/semiconductors/{slug} + /pc-components/dram/{slug} → /{*}/{*}/{slug}
    """
    split = {sig: (sig.partition("?")[0].split("/"), sig.partition("?")[2]) for sig in counts}
    templates: set[str] = set()
    sigs = list(counts)
    for i, a in enumerate(sigs):
        pa, qa = split[a]
        for b in sigs[i + 1:]:
            pb, qb = split[b]
            if qa != qb or len(pa) != len(pb):
                continue
            diff = [k for k in range(len(pa)) if pa[k] != pb[k]]
            if not 1 <= len(diff) <= 2 or (len(diff) == 2 and diff[1] != diff[0] + 1):
                continue
            if any(_is_placeholder_piece(pa[k]) or _is_placeholder_piece(pb[k]) for k in diff):
                continue
            if not any(has_identifier(piece) for piece in pa[diff[-1] + 1:]):
                continue
            merged_path = "/".join("{*}" if k in diff else pa[k] for k in range(len(pa)))
            merged = merged_path + (f"?{qa}" if qa else "")
            if not has_identifier(merged) or merged_path.strip("/") == "{*}":
                continue
            templates.add(merged)
    out: dict[str, int] = {}
    for merged in templates:
        mp, mq = merged.partition("?")[0].split("/"), merged.partition("?")[2]
        covered = sum(count for sig, count in counts.items()
                      if split[sig][1] == mq and len(split[sig][0]) == len(mp)
                      and all(t == "{*}" or t == p for t, p in zip(mp, split[sig][0])))
        out[merged] = covered
    return out


def _sig_matches_known(sig: str, known: str) -> bool:
    regex = "^" + re.escape(known).replace(re.escape("{n}"), r"\{n\}").replace(re.escape("*"), r"[^&]*") + "$"
    return re.match(regex, sig) is not None


def group_by_signature(urls: list[str]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = defaultdict(list)
    for u in urls:
        groups[signature(u)].append(u)
    return groups


# ---------------------------------------------------------------------- 기사 종류 (사이트 하나에 상세 URL 모양 여러 개)
#
# 정책브리핑(korea.kr)처럼 사이트 하나에 보도자료·정책뉴스·연설문 같은 기사 종류가 따로 있고 종류마다 상세 주소 모양이
# 다르다. 설정의 article_patterns가 종류 목록이다: {name, signature, path, query_keys, id_keys, hubs, enabled, count,
# quality, note}. 첫 항목이 대표 종류이고 article_pattern·hubs와 같다(예전 워커와 예전 설정을 위해 둘 다 남긴다).

MAX_KIND_SITEMAPS = 20       # 종류를 찾으려고 여는 하위 사이트맵 수 상한 (하위 이름 모양마다 하나)
MAX_LISTED_KINDS = 20        # 설정에 적는 종류 수 상한 (켜지 않은 것 포함)
# 서명 순위에서 볼 원래 서명 수. 목록 상한과 같게 둔다: 작은 종류(정책브리핑 오피니언 등)도 꺼진 채로는 목록에 남아 관리자가 켤 수 있다
MAX_KIND_CANDIDATES = MAX_LISTED_KINDS
MIN_KIND_URLS = 3            # 종류 후보가 되려면 이만큼의 서로 다른 주소가 보여야 한다

# 기사가 아닌 종류: 경로·하위 사이트맵 이름의 단어나 종류 이름이 이것이면 기본 꺼짐 (켜는 것은 관리자가 고른다)
NON_ARTICLE_WORDS = frozenset({
    "cartoon", "cartoons", "comic", "comics", "toon", "webtoon", "photo", "photos", "gallery", "picture", "pictures",
    "image", "images", "audio", "podcast", "radio", "video", "videos", "vod", "movie", "movies", "tv", "visual",
    "media", "multi", "multimedia", "recruit", "recruitment", "job", "jobs", "career", "careers", "archive",
    "archives", "event", "events"})
NON_ARTICLE_NAME = re.compile(r"(카툰|만화|만평|웹툰|포토|사진|화보|갤러리|오디오|팟캐스트|라디오|영상|비디오|멀티미디어|"
                              r"채용|구인|이벤트|운세|연예|국정신문)")
NON_ARTICLE_NOTE = "기사가 아닌 종류(사진·영상·카툰·채용·옛 자료 등)로 보여 기본으로 끕니다."
# <title>의 "글 제목 - 종류 | 상위 메뉴 | 사이트 이름"을 나누는 구분자 (하이픈·꺾쇠는 앞뒤에 공백이 있어야 한다)
TITLE_SEP = re.compile(r"\s+[-–—<>:·»]\s+|\s*[|｜]\s*")
VIEW_WORD = re.compile(r"(view|detail|read|show)", re.I)
# 공공기관 게시판: 상세 경로는 같고 게시판 파라미터만 다르다 (/bbs/view.do?bbsId=B01&nttId=…). 이 값으로 종류를 나눈다
BOARD_PARAM = re.compile(r"^(bbs_?(id|cd|code|no|nm)?|board_?(id|cd|code|no|nm)?|bo_table|menu_?(id|no|cd|code)?|m_?id|"
                         r"cate(gory)?_?(id|cd|code|no)?|tbl|table)$", re.I)
BOARD_VALUE = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")
# 사이트 홈으로 보는 주소 경로 (/, /index.do, /main.do, /kor/ …). 백엔드 RegistrationService.SITE_ROOT_PATH와 같다
SITE_ROOT_PATH = re.compile(r"^/?((ko|kr|kor|en|eng)/?)?((index|main|home|default)(\.[a-z]+)?)?/?$", re.I)


@dataclass
class KindCandidate:
    pattern: dict
    hubs: list[str]
    urls: list[str]                                   # 이 모양에 맞는 주소 (허브 순서, 정규 주소 기준 중복 없음)
    labels: list[str] = field(default_factory=list)   # 허브 이름표 (하위 사이트맵 이름)


def rank_kinds(pools: list[tuple[str, list[str], str]]) -> list[KindCandidate]:
    """허브별 주소 묶음에서 기사 상세로 보이는 모양(서명)을 고르고, 모양마다 그 주소가 보인 허브를 모은다. 많이 보인 순서."""
    all_urls = [u for _, urls, _ in pools for u in urls]
    out: list[KindCandidate] = []
    for pattern in Discoverer._ranked_patterns(all_urls, MAX_KIND_CANDIDATES):
        hubs, labels, seen = [], [], {}
        for hub, urls, label in pools:
            hit = [u for u in urls if matches(u, pattern)]
            if not hit:
                continue
            hubs.append(hub)
            if label:
                labels.append(label)
            for u in hit:
                # 원래 주소를 남긴다: 목록 행 날짜(Discoverer.hints)가 원래 주소로 적혀 있다
                seen.setdefault(canonical_article_url(u, pattern), u)
        if len(seen) >= MIN_KIND_URLS:
            out += split_by_board(KindCandidate(pattern, list(dict.fromkeys(hubs)), list(seen.values()), labels), pools)
    out.sort(key=lambda c: -len(c.urls))
    return out


def board_value(url: str, key: str) -> str:
    return dict(parse_qsl(urlsplit(url).query, keep_blank_values=True)).get(key, "")


def with_board(pattern: dict, key: str, value: str) -> dict:
    """서명의 게시판 파라미터 자리를 그 값으로 고정한 패턴: /bbs/view.do?bbsId=*&nttId={n} → bbsId=B01"""
    path, _, query = pattern["signature"].partition("?")
    parts = [f"{key}={value}" if kv.partition("=")[0] == key else kv for kv in query.split("&") if kv]
    return pattern_from_signature(path + "?" + "&".join(parts))


def split_by_board(cand: KindCandidate, pools: list[tuple[str, list[str], str]]) -> list[KindCandidate]:
    """같은 상세 경로를 게시판 파라미터(bbsId·boardId·menuId …) 값만 바꿔 쓰는 공공기관 게시판이면 값마다 종류로 나눈다.
    값마다 주소가 MIN_KIND_URLS개 이상인 게시판이 둘 이상일 때만 나누고(작은 게시판은 버린다), 아니면 그대로 둔다."""
    fixed = cand.pattern.get("query_values") or {}
    for key in [k for k in cand.pattern.get("query_keys", []) if BOARD_PARAM.match(k) and k not in fixed]:
        groups: dict[str, list[str]] = {}
        for u in cand.urls:
            groups.setdefault(board_value(u, key), []).append(u)
        big = sorted(((v, us) for v, us in groups.items() if BOARD_VALUE.match(v) and len(us) >= MIN_KIND_URLS),
                     key=lambda g: -len(g[1]))
        if len(big) < 2:
            continue
        out = []
        for value, urls in big:
            pattern = with_board(cand.pattern, key, value)
            out.append(KindCandidate(pattern, hubs_for(pattern, pools) or cand.hubs, urls, cand.labels))
        return out
    return [cand]


def is_site_root(url: str) -> bool:
    """등록 주소가 사이트 홈인가 (/, /index.do, /main.do, /kor/ …, 추적 파라미터만 붙은 것 포함). 홈이 아니면 목록(또는 기사) 주소다."""
    u = (url or "").strip()
    parts = urlsplit(u if "://" in u else "https://" + u)
    query = [k for k, _ in parse_qsl(parts.query, keep_blank_values=True) if not TRACKING_PARAM.match(k)]
    return not query and bool(SITE_ROOT_PATH.match(parts.path or "/"))


def hubs_for(pattern: dict, pools: list[tuple[str, list[str], str]]) -> list[str]:
    """이 모양의 주소가 보인 허브들."""
    return list(dict.fromkeys(hub for hub, urls, _ in pools if any(matches(u, pattern) for u in urls)))


def covered(urls: list[str], patterns: list[dict], share: float = 0.5) -> bool:
    """후보 주소의 절반 이상이 이미 고른 종류에 맞으면 같은 종류로 본다 ({*}로 합친 서명과 원래 서명 같은 경우)."""
    sample = urls[:200]
    if not sample:
        return True
    hit = sum(1 for u in sample if any(matches(u, p) for p in patterns))
    return hit >= len(sample) * share


def _words(text: str) -> set[str]:
    """경로·파일 이름을 단어로: cartoonView.do → {cartoon, view, do}, sitemap_visual.xml → {sitemap, visual, xml}"""
    spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", text or "")
    return {w.lower() for w in re.split(r"[^A-Za-z0-9]+", spaced) if w}


def is_non_article_kind(pattern: dict, name: str | None, labels: list[str] = ()) -> bool:
    words = _words(pattern.get("signature", "").split("?", 1)[0])
    for label in labels:
        words |= _words(label)
    return bool(words & NON_ARTICLE_WORDS) or bool(NON_ARTICLE_NAME.search(name or ""))


def page_title(html: str) -> str | None:
    m = re.search(r"<title[^>]*>(.*?)</title>", html[:20000], re.I | re.S)
    return " ".join(unescape(m.group(1)).split()) if m else None


def title_kind_piece(title: str | None, article_title: str | None) -> str | None:
    """<title>이 "글 제목 - 종류 | 상위 메뉴 | 사이트 이름" 꼴이면 글 제목 바로 뒤 조각(종류). 꼬리가 사이트 이름 하나뿐이면 None."""
    if not title or not article_title:
        return None
    t, a = " ".join(title.split()), " ".join(article_title.split())
    i = t.find(a)
    if i < 0:
        return None
    tail = t[i + len(a):]
    if not TITLE_SEP.match(tail):
        return None
    pieces = [p.strip() for p in TITLE_SEP.split(tail) if p.strip()]
    return pieces[0][:40] if len(pieces) >= 2 else None


def sitemap_label(url: str) -> str:
    """하위 사이트맵 이름표: sitemap_policy.xml → policy, news-sitemap.xml → news"""
    stem = re.sub(r"\.xml(\.gz)?$", "", urlsplit(url).path.rsplit("/", 1)[-1], flags=re.I)
    return re.sub(r"(?i)^sitemap[-_.]?|[-_.]?sitemap$", "", stem).strip("-_.")[:40]


GENERIC_STEMS = frozenset({"index", "view", "detail", "read", "show", "article", "articleview", "default"})


def path_stem(pattern: dict) -> str:
    """서명 경로의 마지막 고정 조각: /news/policyNewsView.do?newsId={n} → policyNewsView, /video/index.html → video"""
    sig = pattern.get("signature") or pattern.get("path") or ""
    pieces = [re.sub(r"\.[A-Za-z0-9]+$", "", p) for p in sig.split("?", 1)[0].split("/") if p and "{" not in p]
    while len(pieces) > 1 and pieces[-1].lower() in GENERIC_STEMS:
        pieces.pop()
    stem = pieces[-1] if pieces else ""
    return stem[:40] or sig[:40] or "기사"


def _menu_name(pattern: dict, sections: list[dict]) -> str | None:
    """홈 메뉴에 이 상세의 목록 주소(…View.do ↔ …List.do)가 있으면 그 메뉴 이름. 게시판별 종류(bbsId=B01)는 같은 게시판
    파라미터 값을 단 메뉴 주소(/bbs/list.do?bbsId=B01)의 이름."""
    path = (pattern.get("signature") or "").split("?", 1)[0]
    fixed = pattern.get("query_values") or {}
    want = VIEW_WORD.sub("list", path).lower() if VIEW_WORD.search(path) else None
    for s in sections:
        parts = urlsplit(s["url"])
        if fixed:
            query = dict(parse_qsl(parts.query, keep_blank_values=True))
            hit = all(query.get(k) == v for k, v in fixed.items())
        else:
            hit = want is not None and parts.path.lower() == want
        if hit:
            name = re.sub(r"\s*더\s*보기$", "", s["name"]).strip()
            if name:
                return name[:40]
    return None


def kind_name(pattern: dict, samples: list[tuple[str | None, Article]] = (), sections: list[dict] = (),
              labels: list[str] = ()) -> str:
    """종류 이름. 표본 상세의 article:section → <title>의 종류 조각(둘 다 표본 절반 이상이 같을 때) → 홈 메뉴 이름
    → 하위 사이트맵 이름표 → 경로 끝 조각 순서로 고른다."""
    n = len(samples)
    if n:
        secs = Counter(" ".join(a.section.split()) for _, a in samples if a.section and a.section.strip())
        pieces = Counter(p for p in (title_kind_piece(t, a.title) for t, a in samples) if p)
        for counts in (secs, pieces):
            if counts:
                name, c = counts.most_common(1)[0]
                if c * 2 >= n:
                    return name[:40]
    return _menu_name(pattern, sections) or next((label for label in labels if label), None) or path_stem(pattern)


PATTERN_KEYS = ("signature", "path", "query_keys", "id_keys", "query_values")


def kind_entry(pattern: dict, name: str, hubs: list[str], enabled: bool, count: int | None,
               quality: str | None = None, note: str | None = None) -> dict:
    """설정의 article_patterns 항목. 서명 필드(path·query_keys·id_keys·query_values·signature)를 그대로 두어 matches()에 바로 쓴다."""
    return {"name": name, **{k: pattern[k] for k in PATTERN_KEYS if k in pattern},
            "hubs": hubs, "enabled": enabled, "count": count, "quality": quality, "note": note}


def unique_names(kinds: list[dict]) -> None:
    """이름이 겹치면 뒤에 온 종류에 경로 끝 조각(게시판별 종류면 게시판 값)을 붙여 구분한다. 앞의 이름은 그대로 둔다
    (이미 문서 section에 쓰인 이름이 바뀌지 않게)."""
    seen: set[str] = set()
    for k in kinds:
        if k["name"] in seen:
            tag = ",".join((k.get("query_values") or {}).values()) or path_stem(k)
            k["name"] = f"{k['name']} ({tag})"
        seen.add(k["name"])


def article_kinds(config: dict) -> list[dict]:
    """설정의 켜진 기사 종류. 종류 목록(article_patterns)이 없는 예전 설정은 article_pattern·hubs 하나짜리 이름 없는 종류로
    읽는다 — 설정 버전을 다시 쓰지 않아도 예전과 똑같이 수집한다."""
    kinds = config.get("article_patterns")
    if not kinds:
        return [{**config["article_pattern"], "name": None, "hubs": list(config.get("hubs") or []), "enabled": True}]
    return [{**k, "hubs": list(k.get("hubs") or config.get("hubs") or [])} for k in kinds if k.get("enabled", True)]


def share_quota(groups: list[list[str]], limit: int) -> list[tuple[int, str]]:
    """종류별 새 주소 목록에서 한 실행 상한(limit)만큼을 돌아가며 하나씩 고른다 (라운드로빈). 한 종류가 상한을 다 쓰지 않고,
    새 글이 적은 종류가 남긴 몫은 아직 남은 종류가 가져간다. (종류 번호, 주소)를 고른 순서대로 돌려준다."""
    out: list[tuple[int, str]] = []
    depth = 0
    while len(out) < limit:
        took = False
        for i, group in enumerate(groups):
            if depth < len(group):
                out.append((i, group[depth]))
                took = True
                if len(out) >= limit:
                    break
        if not took:
            break
        depth += 1
    return out
