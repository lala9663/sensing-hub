"""기사 페이지에서 제목·발행일·기자·본문·첨부를 뽑는다.

사이트별 셀렉터 없이 공통으로 처리한다.
1. 메타: og:title, article:published_time, JSON-LD(NewsArticle 등), <time datetime>
2. 본문: trafilatura(설치 시) → <article>·본문 컨테이너 → 가장 긴 문단 묶음
설정(selectors)이 있으면 그 값을 우선한다.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Comment
from dateutil import parser as dateparser

try:  # 선택 의존성
    import trafilatura  # type: ignore
except ImportError:  # pragma: no cover
    trafilatura = None

ATTACHMENT_EXT = re.compile(r"\.(pdf|hwp|hwpx|docx?|xlsx?|pptx?|zip)(\?|$)", re.I)
ATTACHMENT_HINT = re.compile(r"(filedown|file_down|download|atchfile|attach)", re.I)
KST = timezone.utc  # 저장은 UTC. 시간대 없는 날짜는 KST로 해석한다(아래 _parse_date).
# og:title이 없거나 사이트 이름일 때 본문 안에서 제목을 찾는 자리 (국내 게시판 CMS에 흔한 클래스)
TITLE_CANDIDATES = (".detail-tit", ".view_head h2", ".board_view h2", ".view_tit", ".tit_view", ".bbs_tit", ".board_tit",
                    "[class*=subject]", "[class*=view-tit]", "[class*=view_tit]", "[class*=detail-tit]",
                    "article h1", "article h2", "article h3", "h1")
TITLE_SEPARATORS = (" < ", " | ", " - ", " :: ", " : ", " > ")
DATE_LABELS = ("입력", "등록일", "작성일", "게시일", "게재일", "승인", "발행일", "보도일", "배포일", "일자", "날짜")
ROW_TAGS = ("li", "tr", "dl", "article", "div")
BODY_CANDIDATES = ("article", "[itemprop=articleBody]", "#article-view-content-div", "#articleBody",
                   "#articeBody", ".article_body", ".article-body", ".news_body", ".view_con", ".bbs_view",
                   ".board_view", ".view-content", "#content")


@dataclass
class Article:
    url: str
    title: str | None = None
    published_at: datetime | None = None
    author: str | None = None
    section: str | None = None
    body: str = ""
    attachments: list[tuple[str, str]] = field(default_factory=list)  # (url, 파일명)
    is_article_like: bool = False
    # 자기 주소 아래의 하위 페이지(/board/ATCL1 → /board/ATCL1/172296/view)로 3개 이상 링크하면 목록 페이지로 본다
    is_hub_like: bool = False

    @property
    def body_length(self) -> int:
        return len(self.body or "")


def extract_article(html: str, url: str, selectors: dict | None = None, date_hint: str | None = None,
                    date_format: str | None = None) -> Article:
    """date_hint: 목록 행이나 피드에서 본 날짜 문자열. 상세 페이지에 날짜가 없을 때만 쓴다 (공공기관 게시판에 흔하다).
    date_format: 날짜 텍스트의 strptime 형식 (LLM 레시피). 맞지 않으면 공통 해석으로 넘어간다."""
    soup = BeautifulSoup(html, "lxml")
    selectors = selectors or {}
    art = Article(url=url)

    meta = _meta(soup)
    ld = _json_ld(soup)
    site_names = _site_names(meta, soup)

    og_title = meta.get("og:title")
    if og_title and _is_site_name(og_title, site_names):
        og_title = None  # 사이트 이름을 og:title로 내보내는 CMS: 본문 안의 제목을 찾는다
    art.title = (_select_text(soup, selectors.get("title"))
                 or og_title or ld.get("headline") or _title_in_content(soup, site_names)
                 or (soup.title.get_text(strip=True) if soup.title else None))
    if art.title:
        art.title = _clean_title(art.title)

    date_raw = (_select_text(soup, selectors.get("date"))
                or meta.get("article:published_time") or ld.get("datePublished")
                or meta.get("og:article:published_time") or meta.get("pubdate") or meta.get("date")
                or _time_tag(soup) or _date_in_text(soup) or date_hint)
    art.published_at = _parse_date(date_raw, date_format)

    author = meta.get("article:author") or meta.get("author") or ld.get("author")
    art.author = author[:200] if isinstance(author, str) else None
    art.section = meta.get("article:section") or ld.get("articleSection")

    art.body = (_select_text(soup, selectors.get("body"), joiner="\n") or _trafilatura(html)
                or _body_fallback(soup))
    art.body = re.sub(r"\n{3,}", "\n\n", (art.body or "").strip())

    art.attachments = _attachments(soup, url, selectors.get("attachment"))
    art.is_hub_like = _links_to_children(soup, url) >= 3
    art.is_article_like = (meta.get("og:type") == "article" or bool(ld.get("datePublished"))
                           or (art.published_at is not None and art.body_length >= 200)) and not art.is_hub_like
    return art


def links(html: str, base_url: str) -> list[str]:
    """페이지의 모든 링크(절대 주소)."""
    return [u for u, _ in links_with_dates(html, base_url)]


def links_with_dates(html: str, base_url: str) -> list[tuple[str, str | None]]:
    """(링크, 그 링크가 놓인 목록 행에 보이는 날짜 | None).

    공공기관 게시판은 상세 페이지에 날짜가 없고 목록 행에만 "2026. 10. 15"처럼 적힌 경우가 많다.
    """
    soup = BeautifulSoup(html, "lxml")
    out: list[tuple[str, str | None]] = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        out.append((urljoin(base_url, href).split("#", 1)[0], _row_date(a)))
    return out


def _row_date(a) -> str | None:
    node = a
    for _ in range(4):
        node = node.parent
        if node is None or node.name in ("body", "html"):
            return None
        if node.name in ROW_TAGS:
            text = node.get_text(" ", strip=True)
            if len(text) > 400:  # 행이 아니라 큰 묶음
                return None
            m = _DATE_RE.search(text)
            if m:
                return m.group(0)
            if node.name in ("li", "tr"):  # 행인데 날짜가 없다
                return None
    return None


def feed_links(html: str, base_url: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    out = []
    for link in soup.find_all("link", attrs={"rel": True, "href": True}):
        rel = " ".join(link.get("rel", [])).lower()
        typ = (link.get("type") or "").lower()
        if "alternate" in rel and ("rss" in typ or "atom" in typ):
            out.append(urljoin(base_url, link["href"]))
    return out


# ---------------------------------------------------------------------- helpers

def _meta(soup: BeautifulSoup) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in soup.find_all("meta"):
        key = (m.get("property") or m.get("name") or m.get("itemprop") or "").strip().lower()
        val = m.get("content")
        if key and val and key not in out:
            out[key] = val.strip()
    return out


def _json_ld(soup: BeautifulSoup) -> dict:
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        items = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            typ = item.get("@type")
            types = typ if isinstance(typ, list) else [typ]
            if any(t in ("NewsArticle", "Article", "BlogPosting", "ReportageNewsArticle") for t in types):
                author = item.get("author")
                if isinstance(author, list):
                    author = author[0] if author else None
                if isinstance(author, dict):
                    author = author.get("name")
                return {"headline": item.get("headline"), "datePublished": item.get("datePublished"),
                        "author": author, "articleSection": item.get("articleSection")}
    return {}


def _select_text(soup: BeautifulSoup, selector: str | None, joiner: str = " ") -> str | None:
    if not selector:
        return None
    nodes = soup.select(selector)
    if not nodes:
        return None
    text = joiner.join(n.get_text(" ", strip=True) for n in nodes).strip()
    return text or None


def _time_tag(soup: BeautifulSoup) -> str | None:
    t = soup.find("time", attrs={"datetime": True})
    return t["datetime"] if t else None


_DATE_RE = re.compile(r"(20\d{2})[.\-/년]\s?(\d{1,2})[.\-/월]\s?(\d{1,2})일?(?:\s+(\d{1,2}):(\d{2}))?")


def _date_in_text(soup: BeautifulSoup) -> str | None:
    """"등록일 2026.10.15" 또는 <li><em>등록일</em><span>2026-10-15</span></li> 형태.

    라벨이 든 요소와 그 부모까지만 본다. 목록 페이지의 표 머리글 "등록일"이 아래 행의 날짜를 집어
    게시판 페이지를 기사로 오인하지 않게 하려는 것이다.
    """
    for label in DATE_LABELS:
        for node in soup.find_all(string=re.compile(label))[:5]:
            if isinstance(node, Comment) or node.parent is None:
                continue
            for scope in (node.parent, node.parent.parent):
                if scope is None:
                    continue
                text = scope.get_text(" ", strip=True)
                if len(text) > 200:
                    break
                m = _DATE_RE.search(text)
                if m:
                    return m.group(0)
    return None


def _links_to_children(soup: BeautifulSoup, url: str) -> int:
    """같은 호스트에서 이 페이지 경로 아래로 이어지는 링크 수 (게시판 → 글 처럼)."""
    parts = urlsplit(url)
    prefix = parts.path.rstrip("/") + "/"
    seen = set()
    for a in soup.find_all("a", href=True):
        target = urlsplit(urljoin(url, a["href"]))
        if target.hostname == parts.hostname and target.path.startswith(prefix) and len(target.path) > len(prefix):
            seen.add(target.path)
    return len(seen)


def _site_names(meta: dict[str, str], soup: BeautifulSoup) -> set[str]:
    names = {meta.get("og:site_name", ""), meta.get("application-name", "")}
    if soup.title:
        title = re.sub(r"\s+", " ", soup.title.get_text(" ", strip=True))
        for sep in TITLE_SEPARATORS:
            if sep in title:
                names.add(title.rsplit(sep, 1)[-1])
    return {_norm(n) for n in names if n and n.strip()}


def _is_site_name(text: str, site_names: set[str]) -> bool:
    return _norm(text) in site_names


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _title_in_content(soup: BeautifulSoup, site_names: set[str]) -> str | None:
    for sel in TITLE_CANDIDATES:
        for node in soup.select(sel)[:3]:
            text = node.get_text(" ", strip=True)
            if 5 <= len(text) <= 300 and not _is_site_name(text, site_names):
                return text
    return None


def _parse_date(raw, date_format: str | None = None) -> datetime | None:
    if not raw or not isinstance(raw, str):
        return None
    raw = raw.strip()
    dt: datetime | None = None
    if date_format:
        try:
            dt = datetime.strptime(raw, date_format)
        except ValueError:
            dt = None
    if dt is not None:
        return _to_utc(dt)
    m = _DATE_RE.search(raw)
    try:
        if m and not re.match(r"^\d{4}-\d{2}-\d{2}T", raw):
            y, mo, d, hh, mm = m.groups()
            dt = datetime(int(y), int(mo), int(d), int(hh or 0), int(mm or 0))
        else:
            dt = dateparser.parse(raw)
    except (ValueError, OverflowError):
        return None
    if dt is None:
        return None
    return _to_utc(dt)


def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        # 시간대가 없으면 한국 시간으로 본다
        from datetime import timedelta
        dt = dt.replace(tzinfo=timezone(timedelta(hours=9)))
    return dt.astimezone(timezone.utc)


def _clean_title(title: str) -> str:
    # 이중 인코딩된 엔티티(korea.kr의 og:title "&amp;middot;" → 파서가 "&middot;"로 돌려준다)를 한 번 더 푼다.
    # 두 번까지만: 정상 제목에 든 "&amp;"를 더 풀어 뜻이 바뀌는 일은 두 번째 반복에서 끝난다
    for _ in range(2):
        unescaped = html.unescape(title)
        if unescaped == title:
            break
        title = unescaped
    title = re.sub(r"\s+", " ", title).strip()
    # "제목 < 섹션 < 기사본문 - 매체명" 형태에서 앞부분만
    for sep in (" < ", " | ", " - "):
        if sep in title and len(title.split(sep)[0]) >= 8:
            title = title.split(sep)[0].strip()
            break
    return title[:1000]


def _trafilatura(html: str) -> str | None:
    if trafilatura is None:
        return None
    try:
        return trafilatura.extract(html, include_comments=False, include_tables=True, favor_precision=True)
    except Exception:  # pragma: no cover - 외부 라이브러리 오류는 기본 추출기로 넘긴다
        return None


def _body_fallback(soup: BeautifulSoup) -> str:
    for bad in soup(["script", "style", "nav", "header", "footer", "aside", "form", "noscript"]):
        bad.decompose()
    for sel in BODY_CANDIDATES:
        node = soup.select_one(sel)
        if node:
            text = node.get_text("\n", strip=True)
            if len(text) >= 100:
                return text
    paragraphs = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
    return "\n".join(p for p in paragraphs if len(p) >= 30)


def _attachments(soup: BeautifulSoup, base_url: str, selector: str | None) -> list[tuple[str, str]]:
    host = urlsplit(base_url).hostname
    anchors = soup.select(selector) if selector else soup.find_all("a")
    out, seen = [], set()
    for a in anchors:
        href = (a.get("href") or "").strip()
        onclick = (a.get("onclick") or "").strip()
        name = a.get_text(" ", strip=True) or (a.get("title") or "").strip()
        if href and not href.startswith(("javascript:", "#")):
            absolute = urljoin(base_url, href)
            if not (ATTACHMENT_EXT.search(absolute) or ATTACHMENT_EXT.search(name) or ATTACHMENT_HINT.search(absolute)):
                continue
            if urlsplit(absolute).hostname not in (host, None) and not ATTACHMENT_EXT.search(absolute):
                continue
        elif onclick and (ATTACHMENT_HINT.search(onclick) or ATTACHMENT_EXT.search(onclick)):
            # 스크립트로 내려받는 첨부(fn_download('55308','1','hwpx')): 주소를 만들 수 없어 페이지 주소에 표시만 남긴다.
            # 실제 내려받기는 M2 동적 수집에서 한다. 파일명은 보통 바로 앞 요소에 있다.
            if not ATTACHMENT_EXT.search(name):
                for prev in a.find_all_previous(["a", "span", "strong", "em", "td", "dd"], limit=6):
                    prev_text = prev.get_text(" ", strip=True)
                    if ATTACHMENT_EXT.search(prev_text) and len(prev_text) <= 300:
                        name = prev_text
                        break
            absolute = f"{base_url.split('#', 1)[0]}#js:{onclick}"
        else:
            continue
        name = name or absolute.rsplit("/", 1)[-1]
        if absolute not in seen:
            seen.add(absolute)
            out.append((absolute, name[:500]))
    return out


