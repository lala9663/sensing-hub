"""기사 페이지에서 제목·발행일·기자·본문·첨부를 뽑는다.

사이트별 셀렉터 없이 공통으로 처리한다.
1. 메타: og:title, article:published_time, JSON-LD(NewsArticle 등), <time datetime>
2. 본문: trafilatura(설치 시) → <article>·본문 컨테이너 → 가장 긴 문단 묶음
설정(selectors)이 있으면 그 값을 우선한다.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from dateutil import parser as dateparser

try:  # 선택 의존성
    import trafilatura  # type: ignore
except ImportError:  # pragma: no cover
    trafilatura = None

ATTACHMENT_EXT = re.compile(r"\.(pdf|hwp|hwpx|docx?|xlsx?|pptx?|zip)(\?|$)", re.I)
ATTACHMENT_HINT = re.compile(r"(filedown|file_down|download|atchfile|attach)", re.I)
KST = timezone.utc  # 저장은 UTC. 시간대 없는 날짜는 KST로 해석한다(아래 _parse_date).
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

    @property
    def body_length(self) -> int:
        return len(self.body or "")


def extract_article(html: str, url: str, selectors: dict | None = None) -> Article:
    soup = BeautifulSoup(html, "lxml")
    selectors = selectors or {}
    art = Article(url=url)

    meta = _meta(soup)
    ld = _json_ld(soup)

    art.title = (_select_text(soup, selectors.get("title"))
                 or meta.get("og:title") or ld.get("headline")
                 or (soup.title.get_text(strip=True) if soup.title else None))
    if art.title:
        art.title = _clean_title(art.title)

    date_raw = (_select_text(soup, selectors.get("date"))
                or meta.get("article:published_time") or ld.get("datePublished")
                or meta.get("og:article:published_time") or meta.get("pubdate") or meta.get("date")
                or _time_tag(soup) or _date_in_text(soup))
    art.published_at = _parse_date(date_raw)

    author = meta.get("article:author") or meta.get("author") or ld.get("author")
    art.author = author[:200] if isinstance(author, str) else None
    art.section = meta.get("article:section") or ld.get("articleSection")

    art.body = (_select_text(soup, selectors.get("body"), joiner="\n") or _trafilatura(html)
                or _body_fallback(soup))
    art.body = re.sub(r"\n{3,}", "\n\n", (art.body or "").strip())

    art.attachments = _attachments(soup, url, selectors.get("attachment"))
    art.is_article_like = (meta.get("og:type") == "article" or bool(ld.get("datePublished"))
                           or (art.published_at is not None and art.body_length >= 200))
    return art


def links(html: str, base_url: str) -> list[str]:
    """페이지의 모든 링크(절대 주소). JS 함수 링크는 onclick 인자에서 숫자 ID만 기록한다."""
    soup = BeautifulSoup(html, "lxml")
    out: list[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        out.append(urljoin(base_url, href).split("#", 1)[0])
    return out


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
    # 국내 게시판에 흔한 "등록일 2026.10.15 18:54" 형태
    for label in ("입력", "등록일", "작성일", "게시일", "승인", "발행일"):
        node = soup.find(string=re.compile(label))
        if node:
            m = _DATE_RE.search(node.parent.get_text(" ", strip=True) if node.parent else str(node))
            if m:
                return m.group(0)
    return None


def _parse_date(raw) -> datetime | None:
    if not raw or not isinstance(raw, str):
        return None
    raw = raw.strip()
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
    if dt.tzinfo is None:
        # 시간대가 없으면 한국 시간으로 본다
        from datetime import timedelta
        dt = dt.replace(tzinfo=timezone(timedelta(hours=9)))
    return dt.astimezone(timezone.utc)


def _clean_title(title: str) -> str:
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
    anchors = soup.select(selector) if selector else soup.find_all("a", href=True)
    out, seen = [], set()
    for a in anchors:
        href = (a.get("href") or "").strip()
        if not href or href.startswith(("javascript:", "#")):
            continue
        absolute = urljoin(base_url, href)
        name = a.get_text(" ", strip=True) or absolute.rsplit("/", 1)[-1]
        if not (ATTACHMENT_EXT.search(absolute) or ATTACHMENT_EXT.search(name) or ATTACHMENT_HINT.search(absolute)):
            continue
        if urlsplit(absolute).hostname not in (host, None) and not ATTACHMENT_EXT.search(absolute):
            continue
        if absolute not in seen:
            seen.add(absolute)
            out.append((absolute, name[:500]))
    return out
