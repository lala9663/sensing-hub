"""자동 탐색: 도메인만 받아 새 기사가 나오는 입구(허브)와 기사 URL 패턴을 찾는다.

앞 단계에서 찾으면 뒤로 가지 않는다.
1. RSS/Atom  2. 사이트맵  3. 홈·섹션 링크 패턴 판정 (CMS 패턴 라이브러리 우선)
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urljoin, urlsplit

from bs4 import BeautifulSoup

from .codes import Code, CrawlFailure
from .extract import feed_links, links
from .fetcher import PoliteFetcher

COMMON_FEED_PATHS = ("/rss", "/rss.xml", "/feed", "/rss/allArticle.xml", "/rss/S1N1.xml", "/rssIndex.html")
# 섹션(목록) 페이지로 보이는 주소
LIST_HINT = re.compile(r"(list|section|category|catalog|board(?!.*(wr_id|idx|seq|no=|view|detail))|bbs.*list)", re.I)
# 기사 패턴 후보에서 제외할 목록형 경로
LIST_PATH = re.compile(r"(list|section|category|catalog)", re.I)
TRACKING_PARAM = re.compile(r"^(utm_.*|fbclid|gclid|ref|referrer|from|sns|share)$", re.I)
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

    def to_config(self) -> dict:
        return {"method": self.method, "hubs": self.hubs, "article_pattern": self.article_pattern,
                "sections": self.sections, "excluded_sections": self.excluded_sections}


# ---------------------------------------------------------------------- URL 서명

def signature(url: str) -> str:
    """URL을 패턴 서명으로 바꾼다. 숫자는 {n}, 쿼리 값은 숫자면 {n} 아니면 *."""
    parts = urlsplit(url)
    path = re.sub(r"\d+", "{n}", parts.path or "/")
    q = sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not TRACKING_PARAM.match(k))
    if q:
        qs = "&".join(f"{k}={'{n}' if v.isdigit() else '*'}" for k, v in q)
        return f"{path}?{qs}"
    return path


def pattern_from_signature(sig: str) -> dict:
    path, _, query = sig.partition("?")
    regex = "^" + re.escape(path).replace(re.escape("{n}"), r"\d+") + "$"
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
    soup = BeautifulSoup(xml, "xml")
    out = []
    for item in soup.find_all(["item", "entry"]):
        link = item.find("link")
        if link is None:
            continue
        href = link.get("href") or link.get_text(strip=True)
        if href:
            out.append(href.strip())
    return out


def parse_sitemap(xml: str) -> tuple[list[str], list[str]]:
    """(기사 후보 URL, 하위 사이트맵 URL)"""
    soup = BeautifulSoup(xml, "xml")
    if soup.find("sitemapindex"):
        return [], [loc.get_text(strip=True) for loc in soup.find_all("loc")]
    return [loc.get_text(strip=True) for loc in soup.find_all("loc")], []


def looks_like_feed(text: str) -> bool:
    head = text[:500].lower()
    return "<rss" in head or "<feed" in head or ("<?xml" in head and ("<channel" in text[:3000].lower()))


# ---------------------------------------------------------------------- 탐색

class Discoverer:
    def __init__(self, fetcher: PoliteFetcher, domain: str, base_url: str, max_pages: int = 25):
        self.f = fetcher
        self.domain = domain
        self.base_url = base_url
        self.max_pages = max_pages
        self.pages = 0
        self.notes: list[str] = []

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
        home = self.f.get(self.base_url)  # 홈은 반드시 열려야 한다
        self.pages += 1
        home_links = [u for u in links(home.text, home.url) if same_site(u, self.domain)]
        sections = self._sections(home.text, home.url)

        # 1. RSS
        feeds = self._find_feeds(home.text, home.url)
        if feeds:
            items = []
            for feed in feeds[:5]:
                page = self._get(feed)
                if page and looks_like_feed(page.text):
                    items += [u for u in parse_feed(page.text) if same_site(u, self.domain)]
            pattern = self._dominant_pattern(items)
            if pattern:
                return DiscoveryResult("RSS", feeds[:5], pattern, sections, self._excluded(sections),
                                       _samples(items, pattern)[:10], self.notes)

        # 2. 사이트맵
        for sm in self.f.sitemaps(self.base_url)[:3]:
            page = self._get(sm)
            if not page:
                continue
            urls, children = parse_sitemap(page.text)
            for child in children[-2:]:  # 최신 하위 사이트맵이 보통 뒤에 있다
                sub = self._get(child)
                if sub:
                    urls += parse_sitemap(sub.text)[0]
            urls = [u for u in urls if same_site(u, self.domain)]
            pattern = self._dominant_pattern(urls)
            if pattern:
                return DiscoveryResult("SITEMAP", [sm], pattern, sections, self._excluded(sections),
                                       _samples(urls, pattern)[-10:], self.notes)

        # 3. 홈·섹션 링크 패턴
        candidates = list(home_links)
        kept_sections = [s for s in sections if s["name"] not in self._excluded(sections)]
        for sec in kept_sections[:8]:
            page = self._get(sec["url"])
            if page:
                candidates += [u for u in links(page.text, page.url) if same_site(u, self.domain)]
        pattern = self._dominant_pattern(candidates)
        if not pattern:
            raise CrawlFailure(Code.DISCOVERY_FAILED, stage="DISCOVERY", url=self.base_url)
        hubs = [home.url] + [s["url"] for s in kept_sections[:15]]
        return DiscoveryResult("STATIC", hubs, pattern, sections, self._excluded(sections),
                               _samples(candidates, pattern)[:10], self.notes)

    # ------------------------------------------------------------------
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
        out, seen = [], set()
        for a in soup.find_all("a", href=True):
            url = urljoin(base, a["href"]).split("#", 1)[0]
            name = a.get_text(" ", strip=True)
            if not name or len(name) > 20 or not same_site(url, self.domain) or SKIP_PATH.search(urlsplit(url).path):
                continue
            if LIST_HINT.search(url) and url not in seen:
                seen.add(url)
                out.append({"url": url, "name": name})
        return out[:40]

    @staticmethod
    def _excluded(sections: list[dict]) -> list[str]:
        return [s["name"] for s in sections if any(w in s["name"].lower() for w in EXCLUDED_SECTION_WORDS)]

    @staticmethod
    def _dominant_pattern(urls: list[str]) -> dict | None:
        """링크 묶음에서 기사 상세로 보이는 패턴 하나를 고른다."""
        urls = [u for u in urls if not SKIP_PATH.search(urlsplit(u).path)]
        if not urls:
            return None
        sigs = Counter(signature(u) for u in urls)
        scored: list[tuple[float, str]] = []
        for sig, count in sigs.items():
            if "{n}" not in sig or count < 3:
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
        if not scored:
            return None
        scored.sort(reverse=True)
        return pattern_from_signature(scored[0][1])


def _samples(urls: list[str], pattern: dict) -> list[str]:
    return list(dict.fromkeys(canonical_article_url(u, pattern) for u in urls if matches(u, pattern)))


def _sig_matches_known(sig: str, known: str) -> bool:
    regex = "^" + re.escape(known).replace(re.escape("{n}"), r"\{n\}").replace(re.escape("*"), r"[^&]*") + "$"
    return re.match(regex, sig) is not None


def group_by_signature(urls: list[str]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = defaultdict(list)
    for u in urls:
        groups[signature(u)].append(u)
    return groups
