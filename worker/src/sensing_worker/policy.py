"""사이트 유형 판정과 기본값. 사용자에게 묻지 않고 자동으로 적용한다."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from .extract import Article

PUBLIC_DOMAIN = re.compile(r"(\.(go\.kr|gov|gov\.[a-z]{2}|mil\.kr|or\.kr|assembly\.go\.kr)|^korea\.kr)$", re.I)
NEWS_HINT = re.compile(r"(news|daily|times|herald|ilbo|journal|press|media|신문|일보|뉴스)", re.I)
# 매체 이름(og:site_name·title)에서만 보는 단서. 도메인에는 쓰지 않는다 (economy.go.kr 같은 곳 때문)
NEWS_NAME_HINT = re.compile(r"(신문|일보|뉴스|경제|데일리|투데이|타임스|타임즈|헤럴드|저널|미디어|news|daily|times|herald)", re.I)
NEWS_MARKUP = ("newsmediaorganization", '"newsarticle"', 'og:type" content="article', "ndsoft", "newspaper")
ARTICLE_HREF = re.compile(r"""href=["'][^"']*/(?:article|news|articleview)[s/.?]""", re.I)
SITE_NAME_RE = re.compile(r"""<meta[^>]+(?:og:site_name|application-name)[^>]+content=["']([^"']+)""", re.I)
# 본문이 한두 줄인 정형 단신 ("[부고] 김OO씨 부친상", "[인사] OO은행"). 언론사가 몇 건씩 몰아 올리므로 최신 글 5건이
# 이것뿐일 수 있다. 품질 검사 표본에서는 빼고 다음 후보로 채운다 (수집은 그대로 한다)
ROUTINE_NOTICE_TITLE = re.compile(r"^\s*[\[【〈<(]\s*(부고|부음|인사|동정|게시판|알림|모집|결혼|화촉|포토|사진|화보|오늘의\s*운세|운세|날씨)"
                                  r"\s*[\]】〉>)]")


def guess_site_type(domain: str, home_html: str) -> str:
    if PUBLIC_DOMAIN.search(domain):
        return "PUBLIC"
    low = home_html.lower()
    if NEWS_HINT.search(domain) or any(m in low for m in NEWS_MARKUP):
        return "NEWS"
    if "기사" in home_html and len(ARTICLE_HREF.findall(home_html)) >= 10:
        return "NEWS"
    name = SITE_NAME_RE.search(home_html)
    if name and NEWS_NAME_HINT.search(name.group(1)) and len(ARTICLE_HREF.findall(home_html)) >= 10:
        return "NEWS"
    return "ETC"


def defaults_for(site_type: str) -> dict:
    """주기와 저장 범위 기본값. 언론사는 저작권 때문에 제목·링크·발행일만 저장한다."""
    if site_type == "NEWS":
        return {"interval_minutes": 60, "store_scope": "TITLE_LINK", "collect_attachments": False}
    if site_type == "PUBLIC":
        return {"interval_minutes": 360, "store_scope": "FULL", "collect_attachments": True}
    return {"interval_minutes": 180, "store_scope": "FULL", "collect_attachments": True}


def next_interval(current: int, base: int, new_counts: list[int], *, window: int = 10, min_new: float = 0.5,
                  max_articles: int = 40, max_interval: int = 1440, locked: bool = False) -> tuple[int, str | None]:
    """주기 자동 조정. (새 주기, 바꾼 이유) — 바꾸지 않으면 (현재 주기, None).

    - 사람이 정한 주기(locked)는 건드리지 않는다.
    - 최근 window회 주기 수집이 다 모이기 전에는 판단하지 않는다 (등록 직후 몇 번은 지켜본다).
    - 새 문서 평균이 min_new 미만이면 주기를 두 배로 늘린다 (최대 max_interval분). 조용한 사이트에 요청을 덜 보낸다.
    - 새 문서 평균이 실행당 상한(max_articles)의 절반을 넘으면 사이트 유형 기본값(base)으로 되돌린다 — 더 자주 봐야 놓치지 않는다.
    """
    if locked or window <= 0 or len(new_counts) < window:
        return current, None
    recent = new_counts[:window]
    avg = sum(recent) / len(recent)
    if avg < min_new and current < max_interval:
        new = min(current * 2, max_interval)
        return new, f"주기 자동 조정 {current}→{new}분 (최근 {len(recent)}회 평균 새 문서 {avg:.1f}건)"
    if avg > max_articles / 2 and current > base:
        return base, f"주기 자동 조정 {current}→{base}분 (최근 {len(recent)}회 평균 새 문서 {avg:.1f}건, 기본값으로)"
    return current, None


def is_routine_notice(article: Article) -> bool:
    """정형 단신(부고·인사·포토 등)인지. 제목 머리의 말머리로만 판단한다."""
    return bool(article.title and ROUTINE_NOTICE_TITLE.match(article.title))


def passes_quality(samples: list[Article], *, now: datetime | None = None, need: int = 4,
                   max_age_days: int = 30, attachments_ok: bool = False) -> tuple[bool, str]:
    """샘플 5건 중 4건 이상이 제목·발행일·본문 200자 이상이고, 최근 30일 안에 발행된 글이 있어야 한다.

    attachments_ok: 본문 대신 첨부파일(보도자료 HWP·PDF)에 내용을 두는 공공기관 게시판은 제목·발행일·첨부로도 통과한다.
    """
    now = now or datetime.now(timezone.utc)
    if not samples:
        return False, "샘플 기사를 가져오지 못했습니다."
    good = [a for a in samples if a.title and a.published_at and not a.is_hub_like
            and (a.body_length >= 200 or (attachments_ok and a.attachments))]
    required = min(need, len(samples))
    if len(good) < required:
        return False, f"샘플 {len(samples)}건 중 {len(good)}건만 제목·발행일·본문을 갖췄습니다."
    if not any(a.published_at and a.published_at >= now - timedelta(days=max_age_days) for a in good):
        return False, f"최근 {max_age_days}일 안에 발행된 글이 없습니다."
    return True, f"샘플 {len(samples)}건 중 {len(good)}건 통과"
