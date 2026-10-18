"""링크 모음(포털) 페이지 판정과 언론사 확장 (M3).

사용자가 newsstand.naver.com처럼 다른 언론사로 가는 링크만 모아 둔 페이지를 등록하면 자동 탐색은 당연히 실패한다
(같은 사이트 안에 기사가 없다). 그런 페이지는 관리자 큐로 보내는 대신, 페이지가 가리키는 언론사 도메인을 대신 등록한다.

판정은 홈 HTML만으로 하는 순수 함수라 DB·네트워크 없이 테스트한다.
- outlet_candidates: 홈의 외부 링크를 등록 단위 도메인으로 묶어 언론사로 보이는 것만 (많이 가리킨 순)
- is_portal: 외부 언론사 도메인이 MIN_OUTLETS 이상이고, 외부 링크가 같은 사이트의 기사형 링크보다 많을 때
"""

from __future__ import annotations

import ipaddress
import json
import re
from collections import Counter
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .discovery import has_identifier, same_site, signature
from .policy import NEWS_HINT, NEWS_NAME_HINT

# 외부 언론사 도메인이 이 수 이상이어야 모음 페이지로 본다
MIN_OUTLETS = 10
# 매체명으로 볼 링크 텍스트 길이 상한 (기사 제목은 이보다 길다)
MAX_NAME_CHARS = 24

# 뉴스 모음으로 알려진 포털 (여기서는 외부 링크를 모두 언론사 후보로 본다)
KNOWN_NEWS_PORTALS = frozenset({
    "newsstand.naver.com", "news.naver.com", "n.news.naver.com", "media.naver.com",
    "news.daum.net", "media.daum.net", "v.daum.net", "news.nate.com", "news.zum.com",
    "news.google.com", "news.yahoo.co.jp", "news.kakao.com",
})

# 언론사가 아닌데 뉴스 페이지에서 흔히 링크되는 호스트 (하위 도메인 포함)
DENY_DOMAINS = frozenset({
    "naver.com", "naver.me", "navercorp.com", "pstatic.net", "google.com", "google.co.kr", "googleapis.com", "gstatic.com", "youtube.com", "youtu.be",
    "facebook.com", "fb.com", "instagram.com", "twitter.com", "x.com", "t.co", "kakao.com", "kakaocdn.net", "daum.net",
    "nate.com", "zum.com", "apple.com", "microsoft.com", "linkedin.com", "tiktok.com", "pinterest.com", "line.me",
    "telegram.org", "whatsapp.com", "wikipedia.org", "amazon.com", "cloudflare.com", "w3.org", "schema.org",
})

# 링크 텍스트가 매체명으로 보이는 단서: 정책의 매체명 단서에 방송·통신·TV를 더한다
MEDIA_WORD = re.compile(r"(방송|통신|TV|tv|라디오|경향|한겨레|조선|중앙|동아|매일|국민|서울|세계|문화|한국|연합|머니|이데일리)", re.I)


@dataclass(frozen=True)
class Outlet:
    domain: str      # 등록 단위 도메인 (www. 제거, 소문자)
    base_url: str    # scheme://host[:port]
    name: str        # 링크 텍스트(매체명). 없으면 도메인


# ---------------------------------------------------------------------- 도메인 정규화 (백엔드 UrlNormalizer와 같은 규칙)

_IPV4 = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


def registrable(url: str) -> tuple[str, str] | None:
    """링크 주소 → (도메인, base_url). 내부망·형식 오류·http(s)가 아니면 None."""
    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https") or parts.username or parts.password:
        return None
    try:
        host = (parts.hostname or "").lower().encode("idna").decode("ascii").rstrip(".")
        port = parts.port
    except (UnicodeError, ValueError):
        return None
    if not host or host == "localhost" or host.endswith((".localhost", ".local", ".internal")) or ":" in host:
        return None
    if _IPV4.match(host):
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            return None
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_unspecified:
            return None
    else:
        if "." not in host:
            return None
        labels = host.split(".")
        if not all(_LABEL.match(label) for label in labels):
            return None
        if re.fullmatch(r"\d+|0x[0-9a-f]*", labels[-1]):
            return None
    domain = host[4:] if host.startswith("www.") else host
    default_port = port is None or (scheme == "https" and port == 443) or (scheme == "http" and port == 80)
    base_url = f"{scheme}://{host}" + ("" if default_port else f":{port}")
    return domain, base_url


def denied(domain: str) -> bool:
    return any(domain == d or domain.endswith("." + d) for d in DENY_DOMAINS)


def looks_like_media_name(text: str) -> bool:
    t = " ".join(text.split())
    if not t or len(t) > MAX_NAME_CHARS:
        return False
    return bool(NEWS_NAME_HINT.search(t) or MEDIA_WORD.search(t))


# ---------------------------------------------------------------------- 링크 수집

def _anchor_text(a) -> str:
    """링크 텍스트. 이미지 링크(뉴스스탠드처럼 로고만 있는 경우)는 alt·title을 쓴다."""
    text = a.get_text(" ", strip=True)
    if not text:
        img = a.find("img")
        text = (img.get("alt") or img.get("title") or "") if img else ""
    if not text:
        text = a.get("title") or a.get("aria-label") or ""
    return " ".join(str(text).split())


def page_links(home_html: str, home_url: str) -> list[tuple[str, str]]:
    """(절대 주소, 링크 텍스트) 목록. extract.links_with_dates와 같은 제외 규칙에 텍스트를 더하고,
    인라인 스크립트의 JSON 객체에 적힌 주소(뉴스스탠드 `pressList`처럼 스크립트가 그리는 매체 목록)도 포함한다."""
    soup = BeautifulSoup(home_html, "lxml")
    out = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        out.append((urljoin(home_url, href).split("#", 1)[0], _anchor_text(a)))
    for script in soup.find_all("script", src=False):
        out += script_links(script.string or script.get_text())
    return out


# 인라인 스크립트의 JSON 객체 하나 ({ } 안에 중첩이 없는 것), 그 안의 주소 값과 이름 값
_JSON_OBJECT = re.compile(r"\{[^{}]{1,4000}\}")
_URL_VALUE = re.compile(r'"(?:[A-Za-z_]*(?:url|home|link|href)[A-Za-z_]*)"\s*:\s*"((?:https?:)?\\?/\\?/[^"]+)"', re.I)
_NAME_VALUE = re.compile(r'"(?:nm|name|title|pressName|press_name|shcutTitl|siteName|mediaName|media)"\s*:\s*"([^"]{1,60})"', re.I)


def script_links(text: str) -> list[tuple[str, str]]:
    """스크립트 본문의 JSON 객체에서 (주소, 이름). 주소 키는 url·home·link·href를 포함한 것, 이름은 nm·name·title 같은 키."""
    out = []
    if not text or "http" not in text:
        return out
    for m in _JSON_OBJECT.finditer(text):
        obj = m.group(0)
        urls = _URL_VALUE.findall(obj)
        if not urls:
            continue
        name_match = _NAME_VALUE.search(obj)
        name = _unescape(name_match.group(1)) if name_match else ""
        for raw in urls:
            url = _unescape(raw)
            if url.startswith("//"):
                url = "https:" + url
            out.append((url.split("#", 1)[0], name))
    return out


def _unescape(s: str) -> str:
    try:
        return json.loads(f'"{s}"')
    except ValueError:
        return s.replace("\\/", "/")


def outlet_candidates(home_html: str, home_url: str, domain: str) -> list[Outlet]:
    """홈의 외부 링크 중 언론사로 보이는 도메인. 많이 가리킨 순, 같은 도메인은 한 번."""
    known_portal = domain in KNOWN_NEWS_PORTALS or any(domain.endswith("." + p) for p in KNOWN_NEWS_PORTALS)
    counts: Counter[str] = Counter()
    first_seen: dict[str, int] = {}
    base_urls: dict[str, str] = {}
    names: dict[str, Counter[str]] = {}
    for order, (url, text) in enumerate(page_links(home_html, home_url)):
        if same_site(url, domain):
            continue
        reg = registrable(url)
        if reg is None:
            continue
        outlet_domain, base_url = reg
        if denied(outlet_domain):
            continue
        if not (known_portal or NEWS_HINT.search(outlet_domain) or looks_like_media_name(text)):
            continue
        counts[outlet_domain] += 1
        first_seen.setdefault(outlet_domain, order)
        base_urls.setdefault(outlet_domain, base_url)
        if text:
            names.setdefault(outlet_domain, Counter())[text] += 1
    ranked = sorted(counts, key=lambda d: (-counts[d], first_seen[d]))
    out = []
    for d in ranked:
        name_counter = names.get(d)
        name = name_counter.most_common(1)[0][0] if name_counter else d
        out.append(Outlet(d, base_urls[d], name[:200]))
    return out


def is_portal(home_html: str, home_url: str, domain: str, candidates: list[Outlet]) -> bool:
    """외부 언론사 도메인이 MIN_OUTLETS 이상이고, 외부 링크가 같은 사이트의 기사형 링크보다 많으면 모음 페이지.

    언론사 홈도 다른 매체를 몇 곳 링크하지만, 자기 기사 링크(서명에 식별자가 있는 주소)가 훨씬 많아 여기서 걸린다.
    """
    if len({c.domain for c in candidates}) < MIN_OUTLETS:
        return False
    outlet_domains = {c.domain for c in candidates}
    off_site = 0
    article_like = 0
    for url, _ in page_links(home_html, home_url):
        if same_site(url, domain):
            if has_identifier(signature(url)):
                article_like += 1
            continue
        reg = registrable(url)
        if reg is not None and reg[0] in outlet_domains:
            off_site += 1
    return off_site > article_like


def detect(home_html: str, home_url: str, domain: str) -> list[Outlet]:
    """모음 페이지면 언론사 후보 목록, 아니면 빈 목록."""
    candidates = outlet_candidates(home_html, home_url, domain)
    return candidates if is_portal(home_html, home_url, domain, candidates) else []
