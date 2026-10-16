"""LLM이 쓰는 사이트 수집 레시피: 홈·샘플 기사 페이지를 보여 주고 허브·기사 URL 서명·추출 셀렉터를 JSON으로 받는다.

받은 레시피는 데이터일 뿐이다. 스키마 검증 → 셀렉터 문법·서명 형식 검사 → 허브에서 서명에 맞는 링크 3개 이상 →
기존 샘플 품질 검사(jobs.check_quality)를 모두 통과해야 설정 버전이 된다. 실패하면 휴리스틱 탐색 결과로 돌아간다.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, Comment

from .codes import Code, CrawlFailure
from .config import Settings
from .discovery import (SKIP_PATH, Discoverer, DiscoveryResult, canonical_article_url, looks_like_feed,
                        looks_like_sitemap, matches, parse_feed_entries, pattern_from_signature, same_site, signature,
                        sitemap_urls)
from .extract import links_with_dates
from .fetcher import PoliteFetcher
from .llm import Generation, LlmClient, describe, generate_recipe

log = logging.getLogger(__name__)

MIN_MATCHES = 3          # 허브에서 서명에 맞는 기사 링크가 이만큼은 보여야 한다
MAX_HUBS = 6
MAX_SIGNATURE_ROWS = 30

_NULLABLE_STRING = {"type": ["string", "null"]}
RECIPE_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["hubs", "article_signature", "selectors", "date_format", "notes"],
    "properties": {
        "hubs": {"type": "array", "items": {"type": "string"},
                 "description": "새 글이 가장 먼저 보이는 목록 페이지나 RSS 주소 (절대 URL, 같은 사이트)"},
        "article_signature": {"type": "string",
                              "description": "기사 상세 URL 서명. 숫자는 {n}, 영숫자 식별자는 {id}, 쿼리 값은 숫자면 {n} 아니면 *"},
        "selectors": {
            "type": "object", "additionalProperties": False,
            "required": ["title", "date", "body", "attachment"],
            "properties": {"title": _NULLABLE_STRING, "date": _NULLABLE_STRING,
                           "body": _NULLABLE_STRING, "attachment": _NULLABLE_STRING},
            "description": "상세 페이지용 CSS 셀렉터. 확실하지 않으면 null",
        },
        "date_format": {**_NULLABLE_STRING, "description": "date 셀렉터 텍스트의 strptime 형식 (예 %Y.%m.%d) 또는 null"},
        "notes": {"type": "string", "description": "판단 근거 한두 문장"},
    },
}

EXAMPLE_RECIPE = {
    "hubs": ["https://www.example-news.co.kr/", "https://www.example-news.co.kr/news/articleList.html?sc_section_code=S1N1"],
    "article_signature": "/news/articleView.html?idxno={n}",
    "selectors": {"title": "h3.heading", "date": "ul.infomation li:nth-of-type(2)", "body": "#article-view-content-div",
                  "attachment": None},
    "date_format": None,
    "notes": "ndsoft CMS. 섹션 목록이 최신순이고 기사 상세는 idxno 쿼리로 식별된다.",
}

SYSTEM_PROMPT = f"""당신은 한국 언론사·공공기관 게시판 사이트에서 기사 목록 입구(허브)와 상세 페이지 추출 규칙을 찾는 분석가입니다.
홈 페이지와 샘플 상세 페이지의 HTML을 보고, 수집 시스템이 그대로 쓸 수 있는 레시피를 JSON 객체 하나로 작성합니다.

규칙
- JSON 객체만 출력합니다. 설명 문장, 코드, 마크다운을 덧붙이지 않습니다. 프로그램 코드를 쓰지 않습니다.
- 아래 스키마의 필드만 채웁니다. 모르는 값은 null로 둡니다 (hubs·article_signature·notes는 비울 수 없습니다).
- hubs: 새 글이 가장 먼저 나타나는 목록 페이지(보도자료·공지·뉴스 섹션)나 RSS 주소를 2~{MAX_HUBS}개. 절대 URL이고 같은 사이트여야 합니다.
  기사 상세 주소는 허브가 아닙니다.
- article_signature: 기사 상세 URL을 일반화한 서명입니다. 경로에서 숫자만으로 된 조각은 {{n}}, 숫자가 3개 이상 섞인
  6자 이상의 영숫자 식별자는 {{id}}, 그 밖의 조각 안의 숫자는 {{n}}으로 바꿉니다. 쿼리는 키를 알파벳순으로 정렬하고
  값이 숫자면 {{n}}, 아니면 *로 씁니다 (utm_*·page 같은 추적·쪽수 파라미터는 뺍니다).
  예) /news/articleView.html?idxno={{n}}  /bbs/board.php?bo_table=*&wr_id={{n}}  /kor/article/{{id}}/{{n}}/view?mno=*
  "링크 서명 통계"에 있는 서명을 그대로 고르는 것이 가장 안전합니다. 목록(list·section) 주소를 고르지 마세요.
- selectors: BeautifulSoup.select(soupsieve) 문법의 CSS 셀렉터. title은 글 제목 텍스트만, date는 발행일·등록일 텍스트가
  든 요소, body는 본문 컨테이너, attachment는 첨부파일 <a> 요소를 가리켜야 합니다. 샘플 페이지에서 실제로 확인한 것만 쓰고
  확실하지 않으면 null로 둡니다.
- date_format: date 셀렉터가 가리키는 텍스트가 "2026.10.15"처럼 고정 형식이면 그 strptime 형식(%Y.%m.%d), 아니면 null.
- notes: 판단 근거를 한두 문장으로.

출력 예시 (형식 참고용이며 값은 이 사이트와 무관합니다)
{json.dumps(EXAMPLE_RECIPE, ensure_ascii=False, indent=2)}
"""


@dataclass
class RecipeOutcome:
    result: DiscoveryResult | None
    detail: str                        # 채택이면 품질 검사 설명(+LLM 정보), 아니면 거부 사유
    generation: Generation | None = None


# ---------------------------------------------------------------------- 입력 만들기

def clean_html(html: str, limit: int) -> str:
    """script/style/svg/noscript와 주석을 지우고 공백을 줄인 뒤 limit 글자로 자른다."""
    soup = BeautifulSoup(html, "lxml")
    for bad in soup(["script", "style", "svg", "noscript", "iframe"]):
        bad.decompose()
    for c in soup.find_all(string=lambda s: isinstance(s, Comment)):
        c.extract()
    text = re.sub(r"\s+", " ", str(soup))
    text = re.sub(r"> <", "><", text)
    if len(text) > limit:
        return text[:limit] + " ...(잘림)"
    return text


def signature_rows(urls: list[str], limit: int = MAX_SIGNATURE_ROWS) -> list[tuple[str, int, str]]:
    """(서명, 횟수, 예시 URL)을 많이 보인 순서로. 식별자가 없는 서명(고정 페이지)은 뺀다."""
    groups: dict[str, list[str]] = {}
    for u in urls:
        if SKIP_PATH.search(urlsplit(u).path):
            continue
        groups.setdefault(signature(u), []).append(u)
    rows = [(sig, len(us), us[0]) for sig, us in groups.items() if "{n}" in sig or "{id}" in sig]
    rows.sort(key=lambda r: -r[1])
    return rows[:limit]


def build_user_prompt(base_url: str, domain: str, site_type: str, home_html: str,
                      samples: list[tuple[str, str]], candidates: list[DiscoveryResult],
                      rows: list[tuple[str, int, str]], page_chars: int) -> str:
    parts = [f"사이트: {base_url} (도메인 {domain}, 추정 유형 {site_type})"]
    if candidates:
        parts.append("\n[휴리스틱 탐색이 낸 후보 (참고용이며 틀릴 수 있습니다)]")
        for i, c in enumerate(candidates[:4], 1):
            sig = c.article_pattern.get("signature") or c.article_pattern.get("path")
            parts.append(f"- 후보 {i}: 방식 {c.method}, 허브 {c.hubs[:4]}, 서명 {sig}")
    if rows:
        parts.append("\n[링크 서명 통계: 홈·샘플 페이지의 링크를 서명으로 묶은 것 (서명 | 링크 수 | 예시)]")
        parts += [f"- {sig} | {n} | {example}" for sig, n, example in rows]
    parts.append(f"\n[홈 페이지 HTML: {base_url}]\n{clean_html(home_html, page_chars)}")
    for i, (url, html) in enumerate(samples, 1):
        parts.append(f"\n[샘플 페이지 {i}: {url}]\n{clean_html(html, page_chars)}")
    parts.append("\n위 자료로 레시피 JSON 객체를 작성하세요.")
    return "\n".join(parts)


def pick_sample_urls(candidates: list[DiscoveryResult], home_entries: list[tuple[str, str | None]],
                     limit: int) -> list[str]:
    """LLM에 보여 줄 상세 페이지 후보: 휴리스틱 샘플 → 홈에서 날짜가 붙은 링크 → 식별자가 있는 링크 순."""
    out: list[str] = []
    for c in candidates:
        out += c.sample_urls
    dated = [u for u, d in home_entries if d]
    ided = [u for u, _ in home_entries if ("{n}" in signature(u) or "{id}" in signature(u))
            and not SKIP_PATH.search(urlsplit(u).path)]
    return list(dict.fromkeys(out + dated + ided))[:limit]


# ---------------------------------------------------------------------- 검증

def _selector_error(name: str, sel) -> str | None:
    if sel is None:
        return None
    if not sel.strip():
        return f"selectors.{name}: 빈 문자열 대신 null을 쓰세요"
    try:
        BeautifulSoup("<p></p>", "lxml").select(sel)
    except Exception as e:  # soupsieve.SelectorSyntaxError 등
        return f"selectors.{name}: CSS 셀렉터 문법 오류 ({sel!r}: {str(e).splitlines()[0][:120]})"
    return None


def quick_check(data: dict, domain: str) -> list[str]:
    """네트워크 없이 하는 검사. 오류 메시지를 돌려주면 재요청 프롬프트에 들어간다."""
    errors: list[str] = []
    hubs = data.get("hubs") or []
    if not hubs:
        errors.append("hubs: 허브를 1개 이상 적어야 합니다")
    for h in hubs:
        parts = urlsplit(h)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            errors.append(f"hubs: 절대 URL이어야 합니다 ({h!r})")
        elif not same_site(h, domain):
            errors.append(f"hubs: 같은 사이트({domain})의 주소여야 합니다 ({h!r})")
    sig = data.get("article_signature") or ""
    if not sig.startswith("/") or re.search(r"\s", sig):
        errors.append(f"article_signature: '/'로 시작하는 경로 서명이어야 합니다 ({sig!r})")
    elif "{n}" not in sig and "{id}" not in sig:
        errors.append("article_signature: 기사를 구분하는 {n} 또는 {id} 자리가 있어야 합니다")
    else:
        try:
            re.compile(pattern_from_signature(sig)["path"])
        except re.error as e:
            errors.append(f"article_signature: 패턴으로 바꿀 수 없습니다 ({e})")
    for name, sel in (data.get("selectors") or {}).items():
        err = _selector_error(name, sel)
        if err:
            errors.append(err)
    fmt = data.get("date_format")
    if fmt is not None and "%" not in fmt:
        errors.append(f"date_format: strptime 형식(%Y.%m.%d 등)이거나 null이어야 합니다 ({fmt!r})")
    return errors


def _prune_selectors(selectors: dict, pages: list[tuple[str, str]], pattern: dict) -> tuple[dict, list[str]]:
    """서명에 맞는 샘플 페이지가 있으면, 그 어디에서도 매칭되지 않는 셀렉터는 버린다 (틀린 셀렉터가 첨부 수집을 막지 않게)."""
    soups = [BeautifulSoup(html, "lxml") for url, html in pages if matches(url, pattern)]
    if not soups:
        return selectors, []
    kept, dropped = {}, []
    for name, sel in selectors.items():
        if any(s.select(sel) for s in soups):
            kept[name] = sel
        else:
            dropped.append(f"{name}={sel!r}")
    return kept, dropped


# ---------------------------------------------------------------------- 생성 → 검증 → 결과

def propose_recipe(f: PoliteFetcher, llm: LlmClient, settings: Settings, disc: Discoverer,
                   candidates: list[DiscoveryResult], site_type: str,
                   quality: Callable[[DiscoveryResult], str]) -> RecipeOutcome:
    """LLM 레시피를 한 번 만들어 검증한다. LlmUnavailable은 그대로 올라간다 (호출자가 휴리스틱으로 돌아간다).

    disc: 휴리스틱 탐색을 마친 Discoverer (홈 페이지·섹션·날짜 힌트를 다시 받지 않고 쓴다).
    quality: 샘플 품질 검사. 통과하면 설명을 돌려주고 아니면 QUALITY_FAILED를 낸다 (jobs.check_quality).
    """
    domain, base_url = disc.domain, disc.base_url
    home = disc.home if disc.home is not None else f.get(base_url)
    home_entries = [(u, d) for u, d in links_with_dates(home.text, home.url) if same_site(u, domain)]

    # 1) 입력: 홈 + 샘플 상세 페이지 몇 장 + 링크 서명 통계
    samples: list[tuple[str, str]] = []
    for url in pick_sample_urls(candidates, home_entries, settings.llm_max_sample_pages):
        try:
            page = f.get(url)
        except CrawlFailure as e:
            if e.blocking:
                raise
            continue
        if "html" in page.content_type:
            samples.append((page.url, page.text))
    link_pool = [u for u, _ in home_entries]
    for url, html in samples:
        link_pool += [u for u, _ in links_with_dates(html, url) if same_site(u, domain)]
    rows = signature_rows(link_pool)
    user = build_user_prompt(base_url, domain, site_type, home.text, samples, candidates, rows, settings.llm_page_chars)

    # 2) 생성 (스키마·빠른 검사 실패 시 한 번 재요청)
    gen = generate_recipe(llm, SYSTEM_PROMPT, user, RECIPE_SCHEMA, check=lambda d: quick_check(d, domain))
    who = describe(llm)
    if gen.data is None:
        return RecipeOutcome(None, f"LLM 레시피 검증 실패 ({who}, {gen.attempts}회): " + "; ".join(gen.errors)[:300], gen)
    data = gen.data

    # 3) 허브에서 서명에 맞는 기사 링크 모으기 (3개 이상)
    pattern = pattern_from_signature(data["article_signature"])
    hubs, kinds, found, dates = [], set(), [], {}
    for hub in list(dict.fromkeys(data["hubs"]))[:MAX_HUBS]:
        try:
            page = f.get(hub)
        except CrawlFailure as e:
            if e.blocking:
                raise
            log.info("LLM hub skipped %s: %s", hub, e.code.value)
            continue
        if looks_like_feed(page.text):
            kinds.add("RSS")
            entries = parse_feed_entries(page.text)
        elif looks_like_sitemap(page.text):
            kinds.add("SITEMAP")
            entries = [(u, None) for u in sitemap_urls(f.get, page.text)[:500]]
        else:
            kinds.add("STATIC")
            entries = links_with_dates(page.text, page.url)
        hubs.append(hub)
        for u, date in entries:
            if same_site(u, domain) and matches(u, pattern):
                c = canonical_article_url(u, pattern)
                found.append(c)
                hint = date or disc.hints.get(u)
                if hint and c not in dates:
                    dates[c] = hint
    found = list(dict.fromkeys(found))
    if len(found) < MIN_MATCHES:
        return RecipeOutcome(None, f"LLM 레시피 거부 ({who}): 허브 {len(hubs)}곳에서 서명 {data['article_signature']}에 맞는 "
                                   f"링크가 {len(found)}개뿐입니다 (최소 {MIN_MATCHES}개)", gen)
    method = kinds.pop() if len(kinds) == 1 else "STATIC"

    # 4) 셀렉터 정리 → 결과 → 기존 품질 검사
    selectors = {k: v for k, v in (data.get("selectors") or {}).items() if v}
    selectors, dropped = _prune_selectors(selectors, samples, pattern)
    notes = list(disc.notes) + [f"llm recipe by {who}"] + ([f"selectors dropped (no match): {', '.join(dropped)}"] if dropped else [])
    result = DiscoveryResult(method, hubs, pattern, disc.sections, disc.excluded, found[:10], notes, domain, dates,
                             selectors=selectors, date_format=data.get("date_format"), recipe_source="llm",
                             recipe_notes=(data.get("notes") or "")[:500], llm_model=who)
    try:
        detail = quality(result)
    except CrawlFailure as e:
        if e.code != Code.QUALITY_FAILED:
            raise
        return RecipeOutcome(None, f"LLM 레시피 품질 불합격 ({who}): {e}", gen)
    usage = gen.usage_text()
    return RecipeOutcome(result, f"{detail}; LLM 레시피 {who}" + (f", {usage}" if usage else ""), gen)
