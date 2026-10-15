"""사이트 유형 판정과 기본값. 사용자에게 묻지 않고 자동으로 적용한다."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from .extract import Article

PUBLIC_DOMAIN = re.compile(r"\.(go\.kr|gov|gov\.[a-z]{2}|mil\.kr|or\.kr|assembly\.go\.kr)$", re.I)
NEWS_HINT = re.compile(r"(news|daily|times|herald|ilbo|journal|press|media|신문|일보|뉴스)", re.I)


def guess_site_type(domain: str, home_html: str) -> str:
    if PUBLIC_DOMAIN.search(domain):
        return "PUBLIC"
    low = home_html[:50000].lower()
    if NEWS_HINT.search(domain) or "기사" in home_html[:50000] or 'og:type" content="article' in low \
            or "newsarticle" in low or "ndsoft" in low:
        return "NEWS"
    return "ETC"


def defaults_for(site_type: str) -> dict:
    """주기와 저장 범위 기본값. 언론사는 저작권 때문에 제목·링크·발행일만 저장한다."""
    if site_type == "NEWS":
        return {"interval_minutes": 60, "store_scope": "TITLE_LINK", "collect_attachments": False}
    if site_type == "PUBLIC":
        return {"interval_minutes": 360, "store_scope": "FULL", "collect_attachments": True}
    return {"interval_minutes": 180, "store_scope": "FULL", "collect_attachments": True}


def passes_quality(samples: list[Article], *, now: datetime | None = None, need: int = 4,
                   max_age_days: int = 30) -> tuple[bool, str]:
    """샘플 5건 중 4건 이상이 제목·발행일·본문 200자 이상이고, 최근 30일 안에 발행된 글이 있어야 한다."""
    now = now or datetime.now(timezone.utc)
    if not samples:
        return False, "샘플 기사를 가져오지 못했습니다."
    good = [a for a in samples if a.title and a.published_at and a.body_length >= 200]
    required = min(need, len(samples))
    if len(good) < required:
        return False, f"샘플 {len(samples)}건 중 {len(good)}건만 제목·발행일·본문을 갖췄습니다."
    if not any(a.published_at and a.published_at >= now - timedelta(days=max_age_days) for a in good):
        return False, f"최근 {max_age_days}일 안에 발행된 글이 없습니다."
    return True, f"샘플 {len(samples)}건 중 {len(good)}건 통과"
