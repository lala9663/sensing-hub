"""조달청 나라장터 부정당업자(입찰참가자격 제한) 조회 커넥터 (공공데이터포털, 서비스키는 data_go_kr 공용).

스펙 확인 결과 (2026-10-16, data.go.kr 조회):
- 공공데이터포털에 "조달청_부정당업자제재정보서비스"라는 이름의 오픈API는 **없다**. "부정당업자"·"부정당제재"·"입찰참가자격제한"으로
  오픈API 유형을 검색하면 0건이고, 파일데이터 "조달청_나라장터 부정당제재 내역"(data/15137996, CSV, 항목: 계약법구분·기관·법인등록번호·
  사업자등록번호·소관구분·시행규칙76조별표2(명)·업체·제재근거법률·제재기간월수·제재기간일수·제재시작일자·제재입력일시·제재종료일자·
  조달업무영역·조문명·조항호·처분상태)만 있다. 그 유의사항에 "부정당제재내역은 집계데이터로 Open API에서 개방 중"이라고 적혀 있다.
- 그 Open API로 보이는 것은 "조달청_나라장터 사용자정보 서비스"(data/15129466)의 오퍼레이션 **getUnptRsttCorpInfo02**다.
  · 엔드포인트(확인): https://apis.data.go.kr/1230000/ao/UsrInfoService02/getUnptRsttCorpInfo02
  · 요청 파라미터(확인): ServiceKey(필수), numOfRows(필수), pageNo(필수), type=json(선택), inqryDiv(필수, 조회구분, 예시 1),
    bizno(inqryDiv=1일 때 사업자등록번호), inqryBgnDt·inqryEndDt(선택, YYYYMMDDHHMM — 조회 기간 모드용)
  · 응답(미확인): 포털 상세 화면에는 header·body까지만 보이고 items의 항목명은 보이지 않는다. 사업자등록번호·업체명·주소가 들어 있다고만
    설명한다. **items 항목명은 "확인 필요"** — 아래 FIELD_CANDIDATES는 나라장터 API의 관례(bizno, ...Nm, ...BgnDt/...EndDt)에서 고른
    후보이며, 활용신청 후 미리보기로 실제 항목명을 확인해 점검 소스 config["fields"]로 바꿔 넣는다.
  · 공공데이터포털 게이트웨이 오류(확인, HTTP 200으로도 온다): <OpenAPI_ServiceResponse><cmmMsgHeader><returnReasonCode>
    20 SERVICE_KEY_IS_NULL, 22 LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS(한도 초과), 30 SERVICE_KEY_IS_NOT_REGISTERED,
    31 DEADLINE_HAS_EXPIRED, 32 UNREGISTERED_IP. 정상 응답은 {"response":{"header":{"resultCode":"00",...},"body":{"items":...,"totalCount":N}}}.

조회 방식 선택: **회사별 조회(사업자번호, inqryDiv=1)** 를 기본으로 한다. 확인된 조회 모드가 bizno뿐이고(기간 조회의 inqryDiv 값은
미확인), 제재 건수는 회사 수보다 훨씬 적어 전체 목록을 매번 받을 이유도 없다. 회사가 수백 곳이어도 하루 한 번 수백 번 호출이면
포털 일 한도(보통 1,000~10,000건) 안이다. 기간 모드의 inqryDiv 값을 확인하면 config {"mode": "period", "period_inqry_div": "<값>"}로
바꿔 최근 N일 전체를 받아 메모리에서 사업자번호·정규화한 회사명으로 대조할 수 있다 (사업자번호 없는 회사는 이 모드에서만 이름으로 본다).

- 서비스키는 URL 쿼리에 실리므로 로그·오류·증거 어디에도 남기지 않는다 (mask_secret).
- HTTP 401/403 또는 returnReasonCode 30/31/32 → API_KEY_INVALID, 429·22 → API_ERROR(재시도), 그 밖 → API_ERROR.
- 문서를 만들지 않는다 (fetch는 빈 목록). 점검 소스(check g2b_debarment)의 _check가 check()를 부른다.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode
from xml.etree import ElementTree

from ..codes import Code, CrawlFailure, MESSAGES
from ..fetcher import EVIDENCE_CHARS, PoliteFetcher
from .base import ApiItem, mask_secret
from .nts_status import normalize_biz_no

log = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
CHECK = "g2b_debarment"
DEFAULT_ENDPOINT = "https://apis.data.go.kr/1230000/ao/UsrInfoService02/getUnptRsttCorpInfo02"
PAGE_SIZE = 100
MAX_PAGES = 50
# 공공데이터포털 게이트웨이 코드 (확인됨). 키 문제 → API_KEY_INVALID, 한도 → 재시도
GATEWAY_KEY_ERRORS = {"20", "30", "31", "32", "33"}
GATEWAY_LIMIT_ERROR = "22"
# 응답 items 항목명 후보 (확인 필요). 앞에서부터 처음 있는 키를 쓴다. 점검 소스 config["fields"]로 덮어쓴다
FIELD_CANDIDATES: dict[str, list[str]] = {
    "biz_no": ["bizno", "bizNo", "bsnsRgstNo"],
    "company_name": ["corpNm", "bsnsNm", "bizNm", "prcrmntCorpNm", "entrpsNm"],
    "reason": ["unptRsttRsn", "rsttRsn", "sanctnRsn", "dsctnRsn", "rsn"],
    "start_date": ["unptRsttBgnDt", "rsttBgnDt", "lmtBgnDt", "sanctnBgnDt", "bgnDt"],
    "end_date": ["unptRsttEndDt", "rsttEndDt", "lmtEndDt", "sanctnEndDt", "endDt"],
    "agency": ["rsttInsttNm", "dminsttNm", "prcrmntInsttNm", "insttNm", "orgNm"],
}
# 후보에도 없을 때 이름 패턴으로 찾는다 (…BgnDt → 시작일, …EndDt → 종료일, …Rsn → 사유, …InsttNm → 기관)
FIELD_PATTERNS: dict[str, re.Pattern] = {
    "start_date": re.compile(r"bgndt$", re.I), "end_date": re.compile(r"enddt$", re.I),
    "reason": re.compile(r"rsn$", re.I), "agency": re.compile(r"instt(nm)?$", re.I),
}
# 사업자번호 확인용 시험 호출 (형식만 맞는 값. 키가 틀리면 401/403 또는 게이트웨이 30이 온다)
VERIFY_BIZ_NO = "0000000000"


@dataclass
class Debarment:
    """입찰참가자격 제한 한 건."""
    biz_no: str | None
    company_name: str | None
    reason: str | None
    start_date: str | None          # YYYYMMDD (응답 형식이 YYYY-MM-DD·YYYYMMDDHHMM이면 날짜 8자리로 줄인다)
    end_date: str | None
    agency: str | None
    raw: dict = field(default_factory=dict)

    @property
    def period(self) -> str:
        return f"{_dash(self.start_date)}~{_dash(self.end_date)}" if (self.start_date or self.end_date) else "기간 미상"


class G2bDebarmentConnector:
    provider = "data_go_kr"
    check = CHECK

    def __init__(self, fetcher: PoliteFetcher):
        self.f = fetcher
        self.calls = 0

    def default_config(self) -> dict:
        return {"method": "API", "provider": self.provider, "check": CHECK, "days": 7, "mode": "bizno",
                "endpoint": DEFAULT_ENDPOINT}

    def verify(self, secret: str) -> None:
        """더미 사업자번호 1건 조회. 키가 틀리면 API_KEY_INVALID (활용신청이 안 된 키도 게이트웨이가 거부한다)."""
        self._call(secret, {"inqryDiv": "1", "bizno": VERIFY_BIZ_NO, "numOfRows": "1", "pageNo": "1"}, {}, stage="PROBE")

    def fetch(self, secret: str, config: dict, since: date, limit: int) -> list[ApiItem]:
        """문서를 만들지 않는다. 점검 소스는 check()를 쓴다."""
        return []

    def check(self, secret: str, companies: list[dict], config: dict | None = None) -> dict[int, list[Debarment]]:
        """회사별 제재 목록 {company_id: [Debarment]}. companies는 {id, name, aliases, biz_no} 행.

        mode "bizno"(기본): 사업자번호가 있는 회사마다 inqryDiv=1&bizno=로 조회. 번호가 없는 회사는 건너뛴다 (로그).
        mode "period": inqryDiv=config["period_inqry_div"]와 최근 days 기간으로 전체를 받아 사업자번호 → 정규화한 이름(별칭 포함) 순으로 대조.
        """
        config = config or {}
        mode = str(config.get("mode") or "bizno")
        if mode == "period":
            return self._check_period(secret, companies, config)
        out: dict[int, list[Debarment]] = {}
        skipped = 0
        for c in companies:
            biz_no = normalize_biz_no(c.get("biz_no"))
            if not biz_no:
                skipped += 1
                continue
            rows = self._pages(secret, {"inqryDiv": "1", "bizno": biz_no}, config)
            found = [d for d in (_to_debarment(r, config) for r in rows) if d is not None]
            if found:
                out[int(c["id"])] = found
        log.info("g2b debarment check: companies=%s skipped(no biz_no)=%s sanctioned=%s calls=%s",
                 len(companies), skipped, len(out), self.calls)
        return out

    # ------------------------------------------------------------------
    def _check_period(self, secret: str, companies: list[dict], config: dict) -> dict[int, list[Debarment]]:
        from ..company import normalize_corp_name   # 순환 import 방지
        div = str(config.get("period_inqry_div") or "").strip()
        if not div:
            raise CrawlFailure(Code.API_ERROR, "기간 조회 모드(period)에는 config.period_inqry_div(조회구분 값)가 필요합니다. "
                               "공공데이터포털에서 확인해 넣어 주세요.", retryable=False, stage="LIST")
        days = max(0, int(config.get("days", 7) or 0))
        end = datetime.now(KST)
        begin = end - timedelta(days=days)
        rows = self._pages(secret, {"inqryDiv": div, "inqryBgnDt": begin.strftime("%Y%m%d0000"),
                                    "inqryEndDt": end.strftime("%Y%m%d2359")}, config)
        items = [d for d in (_to_debarment(r, config) for r in rows) if d is not None]
        by_no: dict[str, int] = {}
        by_name: dict[str, int] = {}
        for c in companies:
            n = normalize_biz_no(c.get("biz_no"))
            if n and n not in by_no:
                by_no[n] = int(c["id"])
            for name in [c.get("name"), *(c.get("aliases") or [])]:
                key = normalize_corp_name(name if isinstance(name, str) else None)
                if key and key not in by_name:
                    by_name[key] = int(c["id"])
        out: dict[int, list[Debarment]] = {}
        for d in items:
            cid = by_no.get(d.biz_no or "") if d.biz_no else None
            if cid is None:
                cid = by_name.get(normalize_corp_name(d.company_name))
            if cid is not None:
                out.setdefault(cid, []).append(d)
        log.info("g2b debarment period check: rows=%s matched companies=%s calls=%s", len(items), len(out), self.calls)
        return out

    def _pages(self, secret: str, params: dict, config: dict) -> list[dict]:
        size = int(config.get("page_size") or PAGE_SIZE)
        rows: list[dict] = []
        page_no = 1
        while page_no <= MAX_PAGES:
            body = self._call(secret, {**params, "numOfRows": str(size), "pageNo": str(page_no)}, config, stage="LIST")
            items = _items(body)
            rows.extend(items)
            total = _int(body.get("totalCount"), len(rows))
            if not items or len(rows) >= total:
                break
            page_no += 1
        return rows

    def _call(self, secret: str, params: dict, config: dict, *, stage: str) -> dict:
        """한 번 호출해 response.body(dict)를 돌려준다. 게이트웨이·HTTP 오류는 분류해 CrawlFailure."""
        key = (secret or "").strip()
        endpoint = str(config.get("endpoint") or DEFAULT_ENDPOINT)
        url = endpoint + "?" + urlencode({"ServiceKey": key, "type": "json", **params})
        safe_url = mask_secret(url, key)
        mask = lambda text: mask_secret(text, key)   # noqa: E731
        self.calls += 1
        try:
            page = self.f.get_api(url, headers={"Accept": "application/json"})
        except CrawlFailure as e:
            raise CrawlFailure(e.code, mask(str(e)), url=safe_url, http_status=e.http_status, retryable=e.retryable,
                               stage=stage, evidence=mask(e.evidence)) from None
        evidence = mask(page.text[:EVIDENCE_CHARS]) or None
        if page.status in (401, 403):
            raise CrawlFailure(Code.API_KEY_INVALID, f"{MESSAGES[Code.API_KEY_INVALID]} (공공데이터포털 HTTP {page.status})",
                               url=safe_url, http_status=page.status, stage=stage)
        if page.status == 429:
            raise CrawlFailure(Code.API_ERROR, "공공데이터포털 호출 한도를 넘었습니다 (HTTP 429). 다음 주기에 다시 시도합니다.",
                               url=safe_url, http_status=429, retryable=True, stage=stage, evidence=evidence)
        if page.status != 200:
            raise CrawlFailure(Code.API_ERROR, f"조달청 부정당업자 조회 API 오류 (HTTP {page.status})", url=safe_url,
                               http_status=page.status, retryable=page.status >= 500, stage=stage, evidence=evidence)

        text = (page.text or "").strip()
        if text.startswith("<"):
            # 게이트웨이 오류(키·한도)는 type=json이어도 XML로 온다
            code, msg = _gateway_error(text)
            msg = mask(msg)
            if code in GATEWAY_KEY_ERRORS:
                raise CrawlFailure(Code.API_KEY_INVALID, f"{MESSAGES[Code.API_KEY_INVALID]} (공공데이터포털 {code}: {msg})",
                                   url=safe_url, http_status=200, stage=stage)
            if code == GATEWAY_LIMIT_ERROR:
                raise CrawlFailure(Code.API_ERROR, f"공공데이터포털 호출 한도를 넘었습니다 ({code}: {msg}). 다음 주기에 다시 시도합니다.",
                                   url=safe_url, http_status=200, retryable=True, stage=stage, evidence=evidence)
            raise CrawlFailure(Code.API_ERROR, f"조달청 부정당업자 조회 API 오류 ({code or 'XML'}: {msg})", url=safe_url,
                               http_status=200, retryable=True, stage=stage, evidence=evidence)
        try:
            data = json.loads(text) if text else {}
        except ValueError:
            raise CrawlFailure(Code.API_ERROR, "조달청 부정당업자 조회 응답이 JSON이 아닙니다.", url=safe_url, http_status=200,
                               retryable=True, stage=stage, evidence=evidence) from None
        resp = data.get("response") if isinstance(data, dict) else None
        if not isinstance(resp, dict):
            raise CrawlFailure(Code.API_ERROR, "조달청 부정당업자 조회 응답에 response가 없습니다.", url=safe_url, http_status=200,
                               retryable=True, stage=stage, evidence=evidence)
        header = resp.get("header") or {}
        result = str(header.get("resultCode", "00")).strip()
        if result not in ("00", "0", ""):
            raise CrawlFailure(Code.API_ERROR, f"조달청 부정당업자 조회 API 오류 ({result}: {mask(str(header.get('resultMsg') or ''))})",
                               url=safe_url, http_status=200, retryable=True, stage=stage, evidence=evidence)
        body = resp.get("body")
        return body if isinstance(body, dict) else {}


# ---------------------------------------------------------------------- 응답 해석

def _items(body: dict) -> list[dict]:
    items = body.get("items")
    if isinstance(items, dict):            # XML→JSON 변환형 {"item": [...]} 또는 {"item": {...}}
        items = items.get("item")
    if isinstance(items, dict):
        items = [items]
    return [i for i in (items or []) if isinstance(i, dict)]


def _gateway_error(text: str) -> tuple[str, str]:
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError:
        return "", text[:200]
    code = root.findtext(".//returnReasonCode") or root.findtext(".//resultCode") or ""
    msg = root.findtext(".//returnAuthMsg") or root.findtext(".//errMsg") or root.findtext(".//resultMsg") or ""
    return code.strip(), msg.strip()


def field_names(config: dict | None) -> dict[str, list[str]]:
    """항목명 후보: 기본 후보 앞에 config["fields"]{"biz_no": "bizno" | [...]}를 둔다."""
    out = {k: list(v) for k, v in FIELD_CANDIDATES.items()}
    for k, v in ((config or {}).get("fields") or {}).items():
        if k in out and v:
            names = [v] if isinstance(v, str) else [str(x) for x in v if x]
            out[k] = names + [n for n in out[k] if n not in names]
    return out


def _pick(row: dict, names: list[str], pattern: re.Pattern | None) -> str | None:
    for n in names:
        if n in row and str(row[n] or "").strip():
            return str(row[n]).strip()
    if pattern is not None:
        for k, v in row.items():
            if pattern.search(str(k)) and str(v or "").strip():
                return str(v).strip()
    return None


def _to_debarment(row: dict, config: dict | None) -> Debarment | None:
    names = field_names(config)
    biz_no = normalize_biz_no(_pick(row, names["biz_no"], None))
    company = _pick(row, names["company_name"], None)
    if not biz_no and not company:
        return None
    return Debarment(
        biz_no=biz_no, company_name=company,
        reason=_pick(row, names["reason"], FIELD_PATTERNS["reason"]),
        start_date=_ymd8(_pick(row, names["start_date"], FIELD_PATTERNS["start_date"])),
        end_date=_ymd8(_pick(row, names["end_date"], FIELD_PATTERNS["end_date"])),
        agency=_pick(row, names["agency"], FIELD_PATTERNS["agency"]),
        raw=dict(row),
    )


def _ymd8(text: str | None) -> str | None:
    digits = "".join(ch for ch in (text or "") if ch.isdigit())
    return digits[:8] if len(digits) >= 8 else (text or None)


def _dash(text: str | None) -> str:
    t = (text or "").strip()
    return f"{t[:4]}-{t[4:6]}-{t[6:8]}" if len(t) == 8 and t.isdigit() else (t or "?")


def _int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
