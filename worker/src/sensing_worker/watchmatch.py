"""수집한 문서에 키워드 감시를 대조한다.

키워드 감시(source.kind WATCH)는 원래 네이버 뉴스 검색 API로 기사를 *받아 오는* 소스다. 그런데 우리가 사이트에서
직접 긁어 온 기사(연합뉴스·주간지·공공기관)에도 같은 키워드가 들어 있을 수 있다. 수집이 끝날 때 새 문서의 제목·본문을
모든 감시의 키워드와 대조해, 걸리면 네이버에서 받은 것과 똑같이 document_watch(감시·검색어)로 기록한다.
그러면 문서 화면의 "감시 검색어" 필터와 다이제스트의 감시별 묶음, 회사 신호가 출처를 가리지 않고 동작한다.

- 대소문자·공백 차이는 무시한다 (casefold, 연속 공백 하나로).
- 제외어가 제목에 있으면 그 감시에는 걸리지 않는다.
- 한 문서가 여러 감시에 걸릴 수 있고, 한 감시 안에서는 처음 걸린 검색어만 기록한다 (document_watch는 문서·감시 쌍이 유일).
- 본문이 없는 언론 기사는 제목만 본다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_WS_RE = re.compile(r"\s+")


@dataclass
class WatchKeywords:
    source_id: int
    keywords: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)


def _norm(text: str | None) -> str:
    return _WS_RE.sub(" ", (text or "")).casefold().strip()


def from_configs(rows: list[dict]) -> list[WatchKeywords]:
    """db.load_watch_keywords 결과({"id", "config"}) → WatchKeywords (빈 키워드 감시는 뺀다)."""
    out: list[WatchKeywords] = []
    for r in rows:
        config = r.get("config") or {}
        keywords = [str(k).strip() for k in (config.get("keywords") or []) if str(k).strip()]
        if not keywords:
            continue
        exclude = [str(e).strip() for e in (config.get("exclude") or []) if str(e).strip()]
        out.append(WatchKeywords(int(r["id"]), keywords, exclude))
    return out


def match_keywords(title: str | None, body: str | None, watches: list[WatchKeywords]) -> list[tuple[int, str]]:
    """(감시 source_id, 걸린 검색어) 목록. 감시마다 최대 하나(키워드 순서대로 처음 걸린 것)."""
    norm_title = _norm(title)
    haystack = norm_title + "\n" + _norm(body)
    if not haystack.strip():
        return []
    hits: list[tuple[int, str]] = []
    for w in watches:
        if any(_norm(e) and _norm(e) in norm_title for e in w.exclude):
            continue
        for keyword in w.keywords:
            k = _norm(keyword)
            if k and k in haystack:
                hits.append((w.source_id, keyword))
                break
    return hits
