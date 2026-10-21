"""누락 점검: 사이트가 공개한 기사 목록에 보이는 기사 주소 가운데 켜진 기사 종류(패턴)에 맞지 않는 비율 (pattern_check, V21).

등록할 때 본 주소로 패턴을 고정하면 다른 주소 모양을 조용히 놓친다. 2026-10-21 점검에서 서울신문은 작은 분류가 붙은 주소(49%),
한국경제는 끝에 영문자가 붙은 번호(46%), 동아일보(설정 v1)는 사회면 밖 전부, 수원시 소식은 mode 없는 상세 링크를 놓치고 있었다.
주기 수집이 이미 연 허브의 주소에 robots가 허용한 사이트맵 하나(색인이면 최신 하위 하나)를 더해 하루 한 번 잰다. 요청은 PoliteFetcher로만
보내 robots·도메인 간격을 지키고, 사이트맵은 최대 SITEMAP_REQUESTS번만 연다.

- 맞음: 켜진 종류에 맞는 주소. 꺼짐: 꺼 둔 종류(사진·영상 등)에 맞는 주소 — 누락이 아니다.
- 누락 후보: 어느 종류에도 맞지 않고 기사처럼 보이는 주소 (서명에 번호·ID·슬러그 자리가 있고, 목록·로그인·이미지 경로가 아님).
  HTML 목록에서 본 주소는 메뉴·목록 링크가 섞이므로 켜진 종류와 가까운 것만 센다: 앞 경로(/news, /arti …)가 같거나, 쿼리로
  글을 가리키는 종류면 같은 경로에 식별자 키(reqIdx 등)를 모두 가진 주소. 피드·사이트맵은 기사 목록이라 그대로 센다.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlsplit

from .codes import CrawlFailure
from .discovery import (SKIP_PATH, _is_list_signature, has_identifier, looks_like_sitemap, matches, same_site,
                        signature, sitemap_entries)

log = logging.getLogger(__name__)

SITEMAP_REQUESTS = 3          # 점검 한 번에 사이트맵을 여는 최대 횟수 (색인 + 하위 하나, 첫 후보가 비면 다음 후보 하나)
TOP_UNMATCHED = 5
NON_ARTICLE_SITEMAP = re.compile(r"(image|video|photo|vod|movie)", re.I)

FEED, SITEMAP, LIST = "feed", "sitemap", "list"   # 주소를 본 곳


@dataclass
class PatternMeasure:
    article: int = 0
    matched: int = 0
    off: int = 0
    unmatched: int = 0
    top: list[dict] = field(default_factory=list)   # [{signature, count, example}]

    @property
    def ratio(self) -> float:
        return round(self.unmatched / self.article, 4) if self.article else 0.0


def config_kinds(config: dict) -> tuple[list[dict], list[dict]]:
    """(켜진 종류, 꺼진 종류). 종류 목록이 없는 예전 설정은 article_pattern 하나가 켜진 종류다."""
    kinds = config.get("article_patterns")
    if not kinds:
        return ([config["article_pattern"]] if config.get("article_pattern") else []), []
    return [k for k in kinds if k.get("enabled", True)], [k for k in kinds if not k.get("enabled", True)]


def near(url: str, kind: dict) -> bool:
    """HTML 목록의 링크가 이 종류와 가까운가: 쿼리로 글을 가리키는 종류는 같은 경로에 식별자 키를 모두 가졌는지,
    경로로 가리키는 종류는 첫 자리 표시 앞의 고정 경로(/news, /arti …)가 같은지."""
    parts = urlsplit(url)
    path, _, query = (kind.get("signature") or "").partition("?")
    if query and kind.get("id_keys"):
        q = dict(parse_qsl(parts.query, keep_blank_values=True))
        return bool(re.match(kind["path"], parts.path or "/")) and all(q.get(k, "").isdigit() for k in kind["id_keys"])
    prefix = []
    for piece in path.split("/")[1:]:
        if "{" in piece:
            break
        prefix.append(piece)
    return bool(prefix) and (parts.path or "/").split("/")[1:1 + len(prefix)] == prefix


def measure(entries: list[tuple[str, str]], config: dict, domain: str) -> PatternMeasure:
    """entries: (주소, 본 곳 FEED|SITEMAP|LIST). 같은 주소는 한 번만 센다."""
    on, off = config_kinds(config)
    m = PatternMeasure()
    groups: Counter[str] = Counter()
    examples: dict[str, str] = {}
    seen: set[str] = set()
    for url, origin in entries:
        url = url.split("#", 1)[0]
        if url in seen or not same_site(url, domain) or SKIP_PATH.search(urlsplit(url).path):
            continue
        seen.add(url)
        if any(matches(url, k) for k in on):
            m.matched += 1
        elif any(matches(url, k) for k in off):
            m.off += 1
        else:
            sig = signature(url)
            if not has_identifier(sig) or _is_list_signature(sig):
                continue
            if origin == LIST and not any(near(url, k) for k in on):
                continue
            m.unmatched += 1
            groups[sig] += 1
            examples.setdefault(sig, url)
    m.article = m.matched + m.off + m.unmatched
    m.top = [{"signature": sig, "count": n, "example": examples[sig]} for sig, n in groups.most_common(TOP_UNMATCHED)]
    return m


def sitemap_reference(get, sitemaps: list[str], domain: str, skip: set[str]) -> tuple[str | None, list[str]]:
    """robots.txt의 Sitemap 줄에서 기사 사이트맵 하나를 골라 (그 주소, 같은 사이트 기사 주소들). 이미지·영상 사이트맵과 이미 허브로 연
    사이트맵은 건너뛴다. get: url → Page (fetcher.get — robots·간격·차단 분류는 평소와 같다). 비차단 실패는 다음 후보로 넘어가고,
    차단 신호(봇 확인·429·로그인)는 그대로 올린다. 사이트맵 요청은 SITEMAP_REQUESTS번까지."""
    budget = [SITEMAP_REQUESTS]

    def limited(url: str):
        if budget[0] <= 0:
            return None
        budget[0] -= 1
        try:
            return get(url)
        except CrawlFailure as e:
            if e.blocking:
                raise
            log.info("pattern check sitemap skipped %s: %s", url, e.code.value)
            return None

    for sm in sitemaps:
        if sm in skip or NON_ARTICLE_SITEMAP.search(urlsplit(sm).path):
            continue
        page = limited(sm)
        if page is None:
            if budget[0] <= 0:
                break
            continue
        if not looks_like_sitemap(page.text):
            continue
        urls = [u for u, _ in sitemap_entries(limited, page.text, max_children=1) if same_site(u, domain)]
        if urls:
            return sm, urls
    return None, []
