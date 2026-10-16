"""DART 오픈API 커넥터: 공시 목록(list.json)과 고유번호 전체 목록(corpCode.xml).

- 인증: crtfc_key 쿼리. 키는 URL에 실리므로 로그·오류·증거 어디에도 남기지 않는다 (mask_secret).
- PoliteFetcher.get_api로 호출해 도메인 간격·내부망 차단·연결 재시도는 지키되 robots 검사는 하지 않는다
  (API 주소는 robots 대상이 아니다).
- 응답 status: "000" 정상, "013" 조회 결과 없음(정상·0건), "010/011/012/901" 키 문제(API_KEY_INVALID),
  그 밖("020" 요청 한도 초과, "100" 잘못된 값, "800" 점검 등)은 API_ERROR로 분류해 다음 주기에 다시 시도한다.
- corpCode.xml: GET https://opendart.fss.or.kr/api/corpCode.xml?crtfc_key= → ZIP 한 개(안에 CORPCODE.xml,
  <result><list><corp_code/><corp_name/><stock_code/><modify_date/></list>...</result>, 약 10만 건). 메모리에서 풀어 파싱한다.
  키가 틀리면 ZIP 대신 XML <result><status>010</status>...이 온다. ZIP은 30MB, 풀린 XML은 300MB까지만 받는다.
"""

from __future__ import annotations

import io
import json
import logging
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode
from xml.etree import ElementTree

from ..codes import Code, CrawlFailure, MESSAGES
from ..fetcher import EVIDENCE_CHARS, PoliteFetcher
from .base import ApiItem, mask_secret

log = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
LIST_URL = "https://opendart.fss.or.kr/api/list.json"
CORP_CODE_URL = "https://opendart.fss.or.kr/api/corpCode.xml"
VIEWER_URL = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"
PAGE_COUNT = 100
# 키가 잘못됐거나 쓸 수 없다: 등록되지 않은 키(010), 사용할 수 없는 키(011), 접근할 수 없는 IP(012), 계정 만료(901)
KEY_ERROR_STATUSES = {"010", "011", "012", "901"}
NO_DATA_STATUS = "013"
# 고유번호 ZIP 크기 상한(실제 약 2MB)과 풀린 XML 상한(실제 약 15MB). 그 이상은 압축 폭탄으로 보고 받지 않는다
CORP_ZIP_MAX_BYTES = 30 * 1024 * 1024
CORP_XML_MAX_BYTES = 300 * 1024 * 1024


@dataclass(frozen=True)
class CorpEntry:
    """corpCode.xml 한 줄. stock_code가 비어 있지 않으면 상장사."""
    corp_code: str
    corp_name: str
    stock_code: str = ""
    modify_date: str = ""

    @property
    def listed(self) -> bool:
        return bool(self.stock_code)


def _ymd(d: date) -> str:
    return d.strftime("%Y%m%d")


class DartConnector:
    provider = "dart"

    def __init__(self, fetcher: PoliteFetcher):
        self.f = fetcher

    # ------------------------------------------------------------------ 고유번호 전체 목록
    def corp_codes(self, secret: str) -> list[CorpEntry]:
        """DART 고유번호 전체 목록. ZIP을 메모리에서 풀어 CORPCODE.xml을 파싱한다 (회사명 → corp_code 매핑용)."""
        url = CORP_CODE_URL + "?" + urlencode({"crtfc_key": secret})
        safe_url = mask_secret(url, secret)
        try:
            page = self.f.get_api_bytes(url, max_bytes=CORP_ZIP_MAX_BYTES)
        except CrawlFailure as e:
            raise CrawlFailure(e.code, mask_secret(str(e), secret), url=safe_url, http_status=e.http_status,
                               retryable=e.retryable, stage="LIST", evidence=mask_secret(e.evidence, secret)) from None
        if page.status != 200:
            raise CrawlFailure(Code.API_ERROR, f"DART corpCode API가 HTTP {page.status}로 응답했습니다.", url=safe_url,
                               http_status=page.status, retryable=page.status >= 500, stage="LIST",
                               evidence=mask_secret(page.text[:EVIDENCE_CHARS], secret) or None)
        if page.truncated:
            raise CrawlFailure(Code.API_ERROR, f"DART corpCode ZIP이 상한({CORP_ZIP_MAX_BYTES // (1024 * 1024)}MB)보다 큽니다.",
                               url=safe_url, http_status=200, retryable=False, stage="LIST")
        if not page.body.startswith(b"PK"):
            # ZIP이 아니다: 키 오류 등은 <result><status>..</status><message>..</message></result>로 온다
            status, message = _xml_status(page.text)
            message = mask_secret(message, secret)
            evidence = mask_secret(page.text[:EVIDENCE_CHARS], secret) or None
            if status in KEY_ERROR_STATUSES:
                raise CrawlFailure(Code.API_KEY_INVALID, f"{MESSAGES[Code.API_KEY_INVALID]} (DART {status}: {message})",
                                   url=safe_url, http_status=200, stage="LIST")
            raise CrawlFailure(Code.API_ERROR, f"DART corpCode 응답이 ZIP이 아닙니다 ({status or '형식 불명'}: {message})",
                               url=safe_url, http_status=200, retryable=True, stage="LIST", evidence=evidence)
        try:
            entries = parse_corp_code_zip(page.body)
        except (zipfile.BadZipFile, ElementTree.ParseError, ValueError) as e:
            raise CrawlFailure(Code.API_ERROR, f"DART corpCode ZIP을 풀거나 읽지 못했습니다: {type(e).__name__}: {e}"[:500],
                               url=safe_url, http_status=200, retryable=True, stage="LIST") from None
        log.info("dart corp codes: %s entries (%s bytes zip)", len(entries), len(page.body))
        return entries

    def default_config(self) -> dict:
        # days: 매 실행에서 오늘부터 며칠 전까지 받을지. corp_codes: 비면 전체 공시, 있으면 그 회사(corp_code)만
        return {"method": "API", "provider": self.provider, "days": 1, "corp_codes": []}

    def verify(self, secret: str) -> None:
        today = datetime.now(KST).date()
        self._call(secret, {"bgn_de": _ymd(today), "end_de": _ymd(today), "page_count": "1"})

    def fetch(self, secret: str, config: dict, since: date, limit: int) -> list[ApiItem]:
        end = datetime.now(KST).date()
        base = {"bgn_de": _ymd(min(since, end)), "end_de": _ymd(end), "page_count": str(PAGE_COUNT)}
        corp_codes = [c for c in (config.get("corp_codes") or []) if c] or [None]
        items: list[ApiItem] = []
        seen: set[str] = set()
        for corp in corp_codes:
            page_no = 1
            while len(items) < limit:
                params = {**base, "page_no": str(page_no)}
                if corp:
                    params["corp_code"] = corp
                data = self._call(secret, params)
                for row in data.get("list") or []:
                    item = _to_item(row)
                    if item is None or item.url in seen:
                        continue
                    seen.add(item.url)
                    items.append(item)
                    if len(items) >= limit:
                        break
                if page_no >= _int(data.get("total_page"), 1):
                    break
                page_no += 1
        return items

    # ------------------------------------------------------------------
    def _call(self, secret: str, params: dict) -> dict:
        url = LIST_URL + "?" + urlencode({"crtfc_key": secret, **params})
        safe_url = mask_secret(url, secret)
        try:
            page = self.f.get_api(url)
        except CrawlFailure as e:
            raise CrawlFailure(e.code, mask_secret(str(e), secret), url=safe_url, http_status=e.http_status,
                               retryable=e.retryable, stage="LIST",
                               evidence=mask_secret(e.evidence, secret)) from None
        evidence = mask_secret(page.text[:EVIDENCE_CHARS], secret) or None
        if page.status != 200:
            raise CrawlFailure(Code.API_ERROR, f"DART API가 HTTP {page.status}로 응답했습니다.", url=safe_url,
                               http_status=page.status, retryable=page.status >= 500, stage="LIST", evidence=evidence)
        try:
            data = json.loads(page.text)
        except ValueError:
            raise CrawlFailure(Code.API_ERROR, "DART API 응답이 JSON이 아닙니다.", url=safe_url, http_status=200,
                               retryable=True, stage="LIST", evidence=evidence) from None
        status = str(data.get("status", "")).strip()
        if status == "000":
            return data
        if status == NO_DATA_STATUS:
            return {**data, "list": [], "total_page": 0}
        message = mask_secret(str(data.get("message", "")).strip(), secret)
        if status in KEY_ERROR_STATUSES:
            raise CrawlFailure(Code.API_KEY_INVALID, f"{MESSAGES[Code.API_KEY_INVALID]} (DART {status}: {message})",
                               url=safe_url, http_status=200, stage="LIST")
        raise CrawlFailure(Code.API_ERROR, f"DART API 오류 {status}: {message}", url=safe_url, http_status=200,
                           retryable=True, stage="LIST", evidence=evidence)


def parse_corp_code_zip(data: bytes) -> list[CorpEntry]:
    """corpCode.xml ZIP(바이트) → 항목 목록. ZIP 안의 첫 .xml(보통 CORPCODE.xml)을 스트리밍 파싱한다.
    풀린 크기가 CORP_XML_MAX_BYTES를 넘으면 ValueError."""
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = [i for i in zf.infolist() if i.filename.lower().endswith(".xml")]
        if not names:
            raise ValueError("ZIP 안에 XML이 없습니다.")
        info = names[0]
        if info.file_size > CORP_XML_MAX_BYTES:
            raise ValueError(f"XML이 너무 큽니다 ({info.file_size} bytes)")
        entries: list[CorpEntry] = []
        with zf.open(info) as fh:
            for _, el in ElementTree.iterparse(fh, events=("end",)):
                if el.tag != "list":
                    continue
                code = (el.findtext("corp_code") or "").strip()
                name = (el.findtext("corp_name") or "").strip()
                if code and name:
                    entries.append(CorpEntry(code, name, (el.findtext("stock_code") or "").strip(),
                                             (el.findtext("modify_date") or "").strip()))
                el.clear()
    return entries


def _xml_status(text: str) -> tuple[str, str]:
    """<result><status>010</status><message>...</message></result> → (status, message). 못 읽으면 ("", 앞부분)."""
    try:
        root = ElementTree.fromstring((text or "").strip())
        return (root.findtext("status") or "").strip(), (root.findtext("message") or "").strip()
    except ElementTree.ParseError:
        return "", (text or "").strip()[:200]


def _to_item(row: dict) -> ApiItem | None:
    rcept_no = str(row.get("rcept_no") or "").strip()
    if not rcept_no:
        return None
    return ApiItem(
        url=VIEWER_URL.format(rcept_no=rcept_no),
        title=(row.get("report_nm") or "").strip() or None,
        published_at=_kst_date(row.get("rcept_dt")),
        author=(row.get("corp_name") or "").strip() or None,
        section=(row.get("flr_nm") or "").strip() or None,
        body=None,
        extra={"rcept_no": rcept_no, "corp_code": str(row.get("corp_code") or "").strip(),
               "rcept_dt": str(row.get("rcept_dt") or "").strip(), "rm": str(row.get("rm") or "").strip()},
    )


def _kst_date(text: str | None) -> datetime | None:
    """rcept_dt "20261015" → 한국 시간 그날 자정."""
    text = (text or "").strip()
    if len(text) != 8 or not text.isdigit():
        return None
    try:
        return datetime(int(text[:4]), int(text[4:6]), int(text[6:]), tzinfo=KST)
    except ValueError:
        return None


def _int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
