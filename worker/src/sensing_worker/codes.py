"""실패 코드. 스펙 문서 5.3과 같은 목록이다."""

from __future__ import annotations

from enum import Enum


class Code(str, Enum):
    DNS_FAIL = "DNS_FAIL"
    TIMEOUT = "TIMEOUT"
    NETWORK_BLOCKED = "NETWORK_BLOCKED"
    HTTP_4XX = "HTTP_4XX"
    HTTP_5XX = "HTTP_5XX"
    RATE_LIMITED = "RATE_LIMITED"
    ROBOTS_DISALLOWED = "ROBOTS_DISALLOWED"
    BOT_CHALLENGE = "BOT_CHALLENGE"
    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    DISCOVERY_FAILED = "DISCOVERY_FAILED"
    QUALITY_FAILED = "QUALITY_FAILED"
    PARSE_EMPTY = "PARSE_EMPTY"
    ANOMALY_DROP = "ANOMALY_DROP"
    ATTACHMENT_FAIL = "ATTACHMENT_FAIL"
    TLS_ERROR = "TLS_ERROR"
    INTERNAL_ADDRESS = "INTERNAL_ADDRESS"
    UNEXPECTED = "UNEXPECTED"
    # 공개 API 소스 (M2). source.status_reason에 그대로 쓰고, 백엔드 관리자 큐가 API_KEY 종류로 모은다
    API_KEY_REQUIRED = "API_KEY_REQUIRED"
    API_KEY_INVALID = "API_KEY_INVALID"
    API_ERROR = "API_ERROR"


# 이 코드가 나오면 소스를 BLOCKED로 바꾸고 즉시 멈춘다 (재시도·우회 없음)
BLOCKING_CODES = {Code.BOT_CHALLENGE, Code.LOGIN_REQUIRED, Code.RATE_LIMITED, Code.NETWORK_BLOCKED}

# 관리자가 API 키를 (다시) 등록하면 자동으로 재진행되는 코드
API_KEY_CODES = {Code.API_KEY_REQUIRED, Code.API_KEY_INVALID}

# 사람이 읽는 설명 (등록 요청 결과 메시지에 쓴다)
MESSAGES = {
    Code.DNS_FAIL: "도메인을 찾을 수 없습니다.",
    Code.TIMEOUT: "사이트가 응답하지 않습니다.",
    Code.NETWORK_BLOCKED: "사내 네트워크에서 접속이 차단되어 있습니다.",
    Code.HTTP_4XX: "페이지를 찾을 수 없습니다.",
    Code.HTTP_5XX: "사이트 서버 오류가 발생했습니다.",
    Code.RATE_LIMITED: "사이트가 요청 횟수를 제한했습니다.",
    Code.ROBOTS_DISALLOWED: "사이트의 robots.txt가 자동 수집을 금지합니다.",
    Code.BOT_CHALLENGE: "사이트가 자동 접근을 차단합니다 (봇 확인 화면).",
    Code.LOGIN_REQUIRED: "로그인이 필요한 사이트입니다.",
    Code.DISCOVERY_FAILED: "기사 목록을 자동으로 찾지 못했습니다. 관리자가 확인합니다.",
    Code.QUALITY_FAILED: "기사 내용을 충분히 추출하지 못했습니다. 관리자가 확인합니다.",
    Code.PARSE_EMPTY: "기사 목록이 비어 있습니다. 사이트 구조가 바뀐 것으로 보입니다.",
    Code.ANOMALY_DROP: "수집량이 평소보다 크게 줄었습니다.",
    Code.ATTACHMENT_FAIL: "첨부파일 처리에 실패했습니다.",
    Code.TLS_ERROR: "보안 연결(TLS)에 실패했습니다.",
    Code.INTERNAL_ADDRESS: "내부망 주소로 연결되어 수집할 수 없습니다.",
    Code.UNEXPECTED: "알 수 없는 오류가 발생했습니다.",
    Code.API_KEY_REQUIRED: "공개 API 키가 필요합니다. 관리자가 키를 등록하면 자동으로 진행됩니다.",
    Code.API_KEY_INVALID: "등록된 API 키가 유효하지 않습니다. 관리자가 키를 다시 등록하면 자동으로 진행됩니다.",
    Code.API_ERROR: "공개 API 호출에 실패했습니다.",
}


class CrawlFailure(Exception):
    """수집 중 분류된 실패."""

    def __init__(self, code: Code, message: str | None = None, *, url: str | None = None,
                 http_status: int | None = None, retryable: bool = False, stage: str = "LIST",
                 evidence: str | None = None):
        super().__init__(message or MESSAGES.get(code, code.value))
        self.code = code
        self.url = url
        self.http_status = http_status
        self.retryable = retryable
        self.stage = stage
        self.evidence = evidence

    @property
    def blocking(self) -> bool:
        return self.code in BLOCKING_CODES
