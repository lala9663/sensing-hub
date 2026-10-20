"""수집한 문서에 키워드 감시를 대조한다.

키워드 감시(source.kind WATCH)는 원래 네이버 뉴스 검색 API로 기사를 *받아 오는* 소스다. 그런데 우리가 사이트에서
직접 긁어 온 기사(연합뉴스·주간지·공공기관)에도 같은 키워드가 들어 있을 수 있다. 수집이 끝날 때 새 문서의 제목·본문을
모든 감시의 키워드와 대조해, 걸리면 네이버에서 받은 것과 똑같이 document_watch(감시·검색어)로 기록한다.
그러면 문서 화면의 "감시 검색어" 필터와 다이제스트의 감시별 묶음, 회사 신호가 출처를 가리지 않고 동작한다.

- 대소문자·공백 차이는 무시한다 (casefold, 연속 공백 하나로).
- 영문 단어 경계: 키워드가 라틴 문자·숫자로 시작하면 바로 앞이, 라틴 문자·숫자로 끝나면 바로 뒤가 라틴 문자·숫자가 아니어야 한다
  (한글·공백·문장부호·문자열 끝은 경계). "DS"는 "삼성전자 DS부문"·"DS투자증권"·"(DS)"에 걸리고 "applauds"·"MDS테크"·"GADS"·"DSR"에는
  안 걸린다. 한글로 시작·끝나는 쪽은 지금처럼 부분 문자열이다 (조사가 붙으므로 "반도체"는 "반도체가"에 걸린다).
  섞인 키워드("삼성전자 DS")는 영문 쪽 끝에만 규칙을 쓴다. 백엔드 io.sensinghub.watch.KeywordMatch와 같은 규칙이다.
- 제외어도 같은 규칙으로 보고, 제목에 있으면 그 감시에는 걸리지 않는다.
- 한 문서가 여러 감시에 걸릴 수 있고, 한 감시 안에서는 처음 걸린 검색어만 기록한다 (document_watch는 문서·감시 쌍이 유일).
- 본문이 없는 언론 기사는 제목만 본다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

_WS_RE = re.compile(r"\s+")
# 단어 경계로 치지 않는 글자: ASCII 숫자·라틴 문자(대소문자 무시 뒤라 소문자)와 라틴-1·라틴 확장 A·B의 글자(×·÷ 제외)
_LATIN = "0-9a-zÀ-ÖØ-öø-ɏ"
_LATIN_RE = re.compile(f"[{_LATIN}]")
_LEFT = f"(?<![{_LATIN}])"
_RIGHT = f"(?![{_LATIN}])"


@dataclass
class WatchKeywords:
    source_id: int
    keywords: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)


def normalize(text: str | None) -> str:
    """대조용 정규화: 연속 공백 하나로, casefold, 앞뒤 공백 제거."""
    return _WS_RE.sub(" ", (text or "")).casefold().strip()


@lru_cache(maxsize=4096)
def _pattern(keyword: str) -> re.Pattern[str] | None:
    k = normalize(keyword)
    if not k:
        return None
    left = _LEFT if _LATIN_RE.fullmatch(k[0]) else ""
    right = _RIGHT if _LATIN_RE.fullmatch(k[-1]) else ""
    return re.compile(left + re.escape(k) + right)


def latin_edged(keyword: str) -> bool:
    """키워드가 라틴 문자·숫자로 시작하거나 끝나 영문 단어 경계 규칙이 걸리는지 ("DS"·"삼성전자 DS"는 True, "반도체"는 False)."""
    k = normalize(keyword)
    return bool(k) and bool(_LATIN_RE.fullmatch(k[0]) or _LATIN_RE.fullmatch(k[-1]))


def contains(text: str, keyword: str) -> bool:
    """정규화한 text(normalize)에 keyword가 영문 단어 경계 규칙대로 들어 있는지. 빈 키워드는 False."""
    p = _pattern(keyword)
    return p is not None and p.search(text) is not None


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
    norm_title = normalize(title)
    haystack = norm_title + "\n" + normalize(body)
    if not haystack.strip():
        return []
    hits: list[tuple[int, str]] = []
    for w in watches:
        if any(contains(norm_title, e) for e in w.exclude):
            continue
        for keyword in w.keywords:
            if contains(haystack, keyword):
                hits.append((w.source_id, keyword))
                break
    return hits
