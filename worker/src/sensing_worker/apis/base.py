"""API 커넥터 공통 형식."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Protocol

# 쿼리에 실리는 키 이름들. 비밀 값을 몰라도 가릴 수 있게 패턴으로도 지운다
_KEY_PARAM_RE = re.compile(r"((?:crtfc_key|servicekey|api_key|apikey|key|token)=)[^&\s'\"]+", re.I)


def mask_secret(text: str | None, secret: str | None = None) -> str | None:
    """로그·오류 메시지·URL에서 비밀을 '***'로 가린다. 값 자체와 키 파라미터 패턴을 모두 지운다."""
    if not text:
        return text
    if secret:
        text = text.replace(secret, "***")
    return _KEY_PARAM_RE.sub(r"\1***", text)


@dataclass
class ApiItem:
    """API 응답 한 건. document 테이블의 열과 같다 (body가 None이면 제목·링크만 저장)."""
    url: str
    title: str | None
    published_at: datetime | None
    author: str | None = None
    section: str | None = None
    body: str | None = None
    attachments: list[tuple[str, str]] = field(default_factory=list)   # (url, file_name)


class ApiConnector(Protocol):
    provider: str

    def verify(self, secret: str) -> None:
        """시험 호출. 키가 잘못됐으면 CrawlFailure(API_KEY_INVALID), 그 밖의 장애는 API_ERROR."""

    def default_config(self) -> dict:
        """설정 버전에 저장할 기본 config (method는 항상 "API")."""

    def fetch(self, secret: str, config: dict, since: date, limit: int) -> list[ApiItem]:
        """since 이후 항목을 최신순으로 최대 limit건."""
