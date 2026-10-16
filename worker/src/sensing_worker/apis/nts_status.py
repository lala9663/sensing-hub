"""국세청 사업자등록 상태조회 커넥터 (공공데이터포털, M3 첫 공급사 리스크 소스).

- POST https://api.odcloud.kr/api/nts-businessman/v1/status?serviceKey=<서비스키>  본문 {"b_no": ["1234567890", ...]} (최대 100건)
- 응답 {"status_code":"OK","data":[{"b_no","b_stt","b_stt_cd","tax_type","tax_type_cd","end_dt","utcc_yn","tax_type_change_dt",
  "invoice_apply_dt"}]}. b_stt는 계속사업자(01)·휴업자(02)·폐업자(03). 등록되지 않은 번호는 b_stt가 비고
  tax_type에 "국세청에 등록되지 않은 사업자등록번호입니다."가 온다 (여기서는 b_stt_cd "" → status UNREGISTERED).
- 서비스키는 URL 쿼리에 실리므로 로그·오류·증거 어디에도 남기지 않는다 (mask_secret: 값과 serviceKey= 패턴 모두).
- HTTP 401/403 → API_KEY_INVALID, 429·5xx → API_ERROR(재시도), 그 밖 → API_ERROR.
- 이 커넥터는 문서를 만들지 않는다 (fetch는 빈 목록). 점검 소스(kind CHECK)의 _check_job이 check()를 부른다.
- 공공데이터포털 서비스키 하나를 여러 API(국세청·조달청)가 공유하므로 제공자 id는 data_go_kr이다.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date
from urllib.parse import urlencode

from ..codes import Code, CrawlFailure, MESSAGES
from ..fetcher import EVIDENCE_CHARS, PoliteFetcher
from .base import ApiItem, mask_secret

log = logging.getLogger(__name__)

STATUS_URL = "https://api.odcloud.kr/api/nts-businessman/v1/status"
BATCH_SIZE = 100
# 시험 호출용 번호 (형식만 맞는 값. 등록되지 않은 번호면 응답에 그렇게 나오고, 키가 틀리면 401/403이 온다)
VERIFY_BIZ_NO = "0000000000"

STATUS_CONTINUING = "01"
STATUS_SUSPENDED = "02"
STATUS_CLOSED = "03"
STATUS_UNREGISTERED = ""      # 국세청에 등록되지 않은 번호: b_stt·b_stt_cd가 빈 문자열

STATUS_NAMES = {STATUS_CONTINUING: "계속사업자", STATUS_SUSPENDED: "휴업자", STATUS_CLOSED: "폐업자", STATUS_UNREGISTERED: "미등록"}


@dataclass
class BizStatus:
    """사업자 한 건의 상태."""
    biz_no: str
    status_code: str                 # "01" 계속 | "02" 휴업 | "03" 폐업 | "" 미등록
    status: str                      # b_stt 그대로 (미등록이면 "미등록")
    tax_type: str | None = None      # 과세 유형 (부가가치세 일반과세자 등)
    tax_type_code: str | None = None
    end_dt: str | None = None        # 폐업일 YYYYMMDD
    tax_type_change_dt: str | None = None
    utcc_yn: str | None = None       # 단위과세전환폐업 여부

    @property
    def registered(self) -> bool:
        return self.status_code != STATUS_UNREGISTERED

    @property
    def continuing(self) -> bool:
        return self.status_code == STATUS_CONTINUING

    def as_detail(self) -> dict:
        """company.last_biz_status / company_signal.detail에 넣는 형태."""
        return {"b_no": self.biz_no, "b_stt": self.status, "b_stt_cd": self.status_code, "tax_type": self.tax_type,
                "tax_type_cd": self.tax_type_code, "end_dt": self.end_dt, "tax_type_change_dt": self.tax_type_change_dt,
                "utcc_yn": self.utcc_yn}


def normalize_biz_no(value: str | None) -> str | None:
    """하이픈·공백을 지운 숫자 10자리. 형식이 아니면 None."""
    digits = "".join(ch for ch in (value or "") if ch.isdigit())
    return digits if len(digits) == 10 else None


class NtsStatusConnector:
    provider = "data_go_kr"

    def __init__(self, fetcher: PoliteFetcher):
        self.f = fetcher
        self.calls = 0

    def default_config(self) -> dict:
        return {"method": "API", "provider": self.provider, "check": "nts_status"}

    def verify(self, secret: str) -> None:
        """더미 번호 1건으로 호출해 200이면 통과. 키가 틀리면 API_KEY_INVALID."""
        self._call(secret, [VERIFY_BIZ_NO], stage="PROBE")

    def fetch(self, secret: str, config: dict, since: date, limit: int) -> list[ApiItem]:
        """상태조회는 문서를 만들지 않는다. 점검 소스는 check()를 쓴다."""
        return []

    def check(self, secret: str, biz_nos: list[str]) -> list[BizStatus]:
        """사업자번호 목록의 상태. 100건씩 묶어 부르고 응답 순서와 무관하게 요청한 순서로 돌려준다.

        형식이 틀린 번호는 보내지 않고 건너뛴다 (백엔드가 정규화하므로 보통 없다). 응답에 없는 번호는 결과에도 없다.
        """
        wanted = [n for n in (normalize_biz_no(b) for b in biz_nos) if n]
        wanted = list(dict.fromkeys(wanted))
        by_no: dict[str, BizStatus] = {}
        for i in range(0, len(wanted), BATCH_SIZE):
            batch = wanted[i:i + BATCH_SIZE]
            data = self._call(secret, batch, stage="LIST")
            for row in data.get("data") or []:
                st = _to_status(row)
                if st is not None:
                    by_no[st.biz_no] = st
        log.info("nts status check: requested=%s answered=%s calls=%s", len(wanted), len(by_no), self.calls)
        return [by_no[n] for n in wanted if n in by_no]

    # ------------------------------------------------------------------
    def _call(self, secret: str, biz_nos: list[str], *, stage: str) -> dict:
        key = (secret or "").strip()
        url = STATUS_URL + "?" + urlencode({"serviceKey": key, "returnType": "JSON"})
        safe_url = mask_secret(url, key)
        mask = lambda text: mask_secret(text, key)   # noqa: E731
        self.calls += 1
        try:
            page = self.f.post_api(url, {"b_no": biz_nos}, headers={"Accept": "application/json"})
        except CrawlFailure as e:
            raise CrawlFailure(e.code, mask(str(e)), url=safe_url, http_status=e.http_status, retryable=e.retryable,
                               stage=stage, evidence=mask(e.evidence)) from None
        evidence = mask(page.text[:EVIDENCE_CHARS]) or None
        data: dict = {}
        try:
            parsed = json.loads(page.text) if page.text else {}
            if isinstance(parsed, dict):
                data = parsed
        except ValueError:
            pass
        detail = mask(str(data.get("msg") or data.get("message") or data.get("status_code") or "").strip()) or f"HTTP {page.status}"

        if page.status in (401, 403):
            raise CrawlFailure(Code.API_KEY_INVALID, f"{MESSAGES[Code.API_KEY_INVALID]} (공공데이터포털 {detail})",
                               url=safe_url, http_status=page.status, stage=stage)
        if page.status == 429:
            raise CrawlFailure(Code.API_ERROR, f"공공데이터포털 호출 한도를 넘었습니다 ({detail}). 다음 주기에 다시 시도합니다.",
                               url=safe_url, http_status=429, retryable=True, stage=stage, evidence=evidence)
        if page.status != 200:
            raise CrawlFailure(Code.API_ERROR, f"국세청 사업자 상태조회 API 오류 ({detail})", url=safe_url,
                               http_status=page.status, retryable=page.status >= 500, stage=stage, evidence=evidence)
        if not data or str(data.get("status_code", "OK")).upper() != "OK" or "data" not in data:
            raise CrawlFailure(Code.API_ERROR, f"국세청 사업자 상태조회 응답이 예상한 JSON이 아닙니다 ({detail})", url=safe_url,
                               http_status=200, retryable=True, stage=stage, evidence=evidence)
        return data


def _to_status(row: dict) -> BizStatus | None:
    biz_no = normalize_biz_no(str(row.get("b_no") or ""))
    if not biz_no:
        return None
    code = str(row.get("b_stt_cd") or "").strip()
    status = str(row.get("b_stt") or "").strip() or STATUS_NAMES.get(code, "미등록" if not code else code)
    return BizStatus(
        biz_no=biz_no,
        status_code=code,
        status=status,
        tax_type=_text(row.get("tax_type")),
        tax_type_code=_text(row.get("tax_type_cd")),
        end_dt=_text(row.get("end_dt")),
        tax_type_change_dt=_text(row.get("tax_type_change_dt")),
        utcc_yn=_text(row.get("utcc_yn")),
    )


def _text(value) -> str | None:
    text = str(value or "").strip()
    return text or None
