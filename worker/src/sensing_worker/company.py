"""회사(거래처) 레이어의 규칙 (M3). DB·HTTP 없이 순수 함수만 두어 단위 테스트로 고정한다.

- 문서 → 회사 연결: 보강 태그(고유명사)가 회사 이름·별칭과 **정확히**(대소문자·공백 무시) 같을 때만 TAG로 연결한다.
  부분 일치·유사도 매칭은 하지 않는다 (오탐으로 엉뚱한 회사에 신호가 붙는 것이 누락보다 나쁘다).
- 회사 신호(company_signal)의 종류·심각도·fingerprint:
  · BIZ_STATUS (국세청 사업자 상태): 폐업 HIGH, 휴업 HIGH, 미등록 MEDIUM, 과세유형 변경 LOW, 영업 재개(휴·폐업 → 계속) LOW.
    fingerprint는 b_stt_cd + end_dt (상태가 같으면 매일 다시 기록하지 않는다), 과세유형 변경은 "tax:" + 새 과세유형 코드.
  · NEWS (회사 감시·태그로 연결된 기사): 보강 토픽이 사건·사고 또는 인사·조직이면 MEDIUM, fingerprint는 url_hash.
  · DISCLOSURE (DART 회사별 공시, M3-2): 보고서명(report_nm)에 든 말로 HIGH(회생절차·파산·영업정지·상장폐지·감사의견 거절·부적정·
    자본잠식·불성실공시·횡령·배임) / MEDIUM(소송·최대주주 변경·대표이사 변경·유상증자·합병·분할·영업양수도·거래정지) /
    LOW(그 밖의 주요사항보고서·정정공시). 일반 정기·수시 공시는 문서만 두고 신호는 없다. fingerprint는 rcept_no.
  · DEBARMENT (조달청 부정당업자, M3-2): 제재가 있으면 항상 HIGH, fingerprint는 제재 시작일 + 기관.
- DART 고유번호 매핑 (M3-2): 회사 이름(별칭 포함)과 corpCode.xml의 corp_name을 공백·"(주)"·"주식회사"·"㈜"를 지우고 소문자로 비교해
  **정확히 일치**하는 항목이 하나면 채운다. 여러 개면 상장사(stock_code 있음)가 하나일 때만 그것을, 그래도 여러 개면 채우지 않는다.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Iterable

from .apis.dart import CorpEntry
from .apis.g2b_debarment import Debarment
from .apis.nts_status import STATUS_CLOSED, STATUS_CONTINUING, STATUS_SUSPENDED, STATUS_UNREGISTERED, BizStatus

# 이 토픽이 붙은 기사는 회사 신호(NEWS)가 된다. 사건·사고(장애·소송·제재·논란), 인사·조직(경영진 교체·조직 개편)
NEWS_SIGNAL_TOPICS: frozenset[str] = frozenset({"사건·사고", "인사·조직"})
NEWS_SEVERITY = "MEDIUM"

_WS_RE = re.compile(r"\s+")


@dataclass
class Signal:
    kind: str
    severity: str
    fingerprint: str
    title: str
    detail: dict = field(default_factory=dict)


# ---------------------------------------------------------------------- 이름 매칭

def normalize_name(text: str | None) -> str:
    """대소문자·공백을 무시한 비교 키. 그 밖의 문자(괄호·점 등)는 그대로 둔다 — '정확히 일치'의 범위를 넓히지 않는다."""
    return _WS_RE.sub("", (text or "")).casefold()


def build_name_index(rows: list[dict]) -> dict[str, int]:
    """[{id, name, aliases}] → {정규화한 이름·별칭: company_id}. 같은 이름이 두 회사에 있으면 먼저 온 회사(id 순)가 이긴다."""
    index: dict[str, int] = {}
    for row in rows:
        for name in [row.get("name"), *(row.get("aliases") or [])]:
            key = normalize_name(name if isinstance(name, str) else None)
            if key and key not in index:
                index[key] = int(row["id"])
    return index


def match_companies_by_tags(tags: list[str] | None, index: dict[str, int]) -> list[int]:
    """태그가 회사 이름·별칭과 정확히 일치하는 회사 id (중복 없이, 태그 순서). 부분 일치는 하지 않는다."""
    out: list[int] = []
    for tag in tags or []:
        company_id = index.get(normalize_name(tag if isinstance(tag, str) else None))
        if company_id is not None and company_id not in out:
            out.append(company_id)
    return out


# ---------------------------------------------------------------------- 신호 규칙

def news_signal(url_hash: str, title: str | None, topics: list[str] | None, url: str | None = None,
                matched_by: str | None = None) -> Signal | None:
    """보강 토픽이 사건·사고·인사·조직이면 NEWS 신호. fingerprint는 url_hash라 같은 기사는 한 번만 남는다."""
    hit = [t for t in (topics or []) if t in NEWS_SIGNAL_TOPICS]
    if not hit:
        return None
    return Signal("NEWS", NEWS_SEVERITY, url_hash, (title or "").strip()[:500] or "(제목 없음)",
                  {"topics": hit, "url": url, "matched_by": matched_by})


def biz_status_signal(status: BizStatus, previous: dict | None) -> Signal | None:
    """국세청 상태 한 건과 이전 확인 결과(company.last_biz_status)로 BIZ_STATUS 신호를 정한다. 없으면 None.

    - 계속사업자가 아니면 (휴업·폐업·미등록) 항상 신호. fingerprint가 같으면 DB가 중복을 막는다.
    - 계속사업자인데 이전 확인이 휴·폐업이었으면 "영업 재개" LOW.
    - 계속사업자이고 과세유형 코드가 이전과 다르면 "과세유형 변경" LOW.
    - 그 밖(계속사업자, 변화 없음)은 신호 없음.
    """
    prev_code = str((previous or {}).get("b_stt_cd") or "") if previous else None
    prev_tax = str((previous or {}).get("tax_type_cd") or "") if previous else None
    detail = status.as_detail()
    fingerprint = (status.status_code or "") + (status.end_dt or "")

    if status.status_code == STATUS_CLOSED:
        when = f" (폐업일 {_ymd(status.end_dt)})" if status.end_dt else ""
        return Signal("BIZ_STATUS", "HIGH", fingerprint, f"폐업자{when}", detail)
    if status.status_code == STATUS_SUSPENDED:
        return Signal("BIZ_STATUS", "HIGH", fingerprint, "휴업자", detail)
    if status.status_code == STATUS_UNREGISTERED:
        return Signal("BIZ_STATUS", "MEDIUM", fingerprint or "unregistered",
                      "국세청에 등록되지 않은 사업자등록번호", detail)
    if status.status_code != STATUS_CONTINUING:
        # 모르는 코드: 바뀐 것만 알린다
        if prev_code is not None and prev_code != status.status_code:
            return Signal("BIZ_STATUS", "MEDIUM", fingerprint, f"사업자 상태 변경: {status.status}", detail)
        return None

    # 계속사업자
    if prev_code is not None and prev_code in (STATUS_SUSPENDED, STATUS_CLOSED, STATUS_UNREGISTERED):
        return Signal("BIZ_STATUS", "LOW", fingerprint, "계속사업자로 전환 (영업 재개)", {**detail, "previous": previous})
    new_tax = status.tax_type_code or ""
    if prev_tax is not None and prev_tax != new_tax:
        return Signal("BIZ_STATUS", "LOW", "tax:" + new_tax,
                      f"과세유형 변경: {(previous or {}).get('tax_type') or prev_tax or '없음'} → {status.tax_type or new_tax or '없음'}",
                      {**detail, "previous": previous})
    return None


def _ymd(text: str | None) -> str:
    t = (text or "").strip()
    return f"{t[:4]}-{t[4:6]}-{t[6:8]}" if len(t) == 8 and t.isdigit() else t


# ---------------------------------------------------------------------- DART 고유번호 매핑 (M3-2)

# 법인 표기와 공백을 지운다. 그 밖의 문자(괄호 안 다른 말, 영문 대소문자 차이)는 casefold만 한다
_CORP_NOISE_RE = re.compile(r"\(주\)|（주）|㈜|주식회사|\s+")


def normalize_corp_name(text: str | None) -> str:
    """DART corp_name과 회사 이름·별칭을 비교하는 키: 공백·"(주)"·"주식회사"·"㈜" 제거, 소문자."""
    return _CORP_NOISE_RE.sub("", text or "").casefold()


def build_corp_index(entries: Iterable[CorpEntry]) -> dict[str, list[CorpEntry]]:
    """corpCode.xml 항목 → {정규화한 이름: [항목...]}. 같은 이름의 회사가 여럿이면 모두 둔다 (pick_corp_code가 고른다)."""
    index: dict[str, list[CorpEntry]] = {}
    for e in entries:
        key = normalize_corp_name(e.corp_name)
        if key:
            index.setdefault(key, []).append(e)
    return index


@dataclass
class CorpPick:
    """매핑 결과. entry가 None이고 candidates가 둘 이상이면 모호(채우지 않음), 비어 있으면 일치 없음."""
    entry: CorpEntry | None
    candidates: list[CorpEntry] = field(default_factory=list)
    matched_on: str | None = None     # "name" | "alias:<별칭>"

    @property
    def ambiguous(self) -> bool:
        return self.entry is None and len(self.candidates) > 1


def pick_corp_code(name: str | None, aliases: list | None, index: dict[str, list[CorpEntry]]) -> CorpPick:
    """이름을 먼저, 없으면 별칭 순서대로 본다. 정확히 하나면 그것, 여럿이면 상장사가 하나일 때만 그것, 그래도 여럿이면 모호로 멈춘다
    (모호한 이름을 두고 별칭으로 넘어가지 않는다 — 엉뚱한 회사에 붙는 것이 비어 있는 것보다 나쁘다)."""
    labels = [("name", name)] + [(f"alias:{a}", a) for a in (aliases or []) if isinstance(a, str)]
    for label, text in labels:
        key = normalize_corp_name(text if isinstance(text, str) else None)
        if not key:
            continue
        seen: set[str] = set()
        candidates = [c for c in index.get(key, []) if not (c.corp_code in seen or seen.add(c.corp_code))]
        if not candidates:
            continue
        if len(candidates) == 1:
            return CorpPick(candidates[0], candidates, label)
        listed = [c for c in candidates if c.listed]
        if len(listed) == 1:
            return CorpPick(listed[0], candidates, label)
        return CorpPick(None, candidates, label)
    return CorpPick(None, [], None)


# ---------------------------------------------------------------------- DART 공시 신호 (M3-2)

# 보고서명에서 공백을 지운 뒤 포함 여부로 본다. 순서는 HIGH → MEDIUM → LOW
DISCLOSURE_HIGH = ("회생절차", "파산", "영업정지", "상장폐지", "의견거절", "감사의견거절", "부적정", "자본잠식", "불성실공시", "횡령", "배임")
DISCLOSURE_MEDIUM = ("소송", "최대주주변경", "대표이사변경", "유상증자", "합병", "분할", "영업양수", "영업양도", "거래정지")
DISCLOSURE_LOW = ("주요사항보고서", "기재정정", "정정")


def disclosure_severity(report_nm: str | None) -> tuple[str | None, list[str]]:
    """보고서명 → (심각도 | None, 걸린 말). None이면 일반 공시(문서만, 신호 없음)."""
    text = _WS_RE.sub("", report_nm or "")
    if not text:
        return None, []
    for severity, words in (("HIGH", DISCLOSURE_HIGH), ("MEDIUM", DISCLOSURE_MEDIUM), ("LOW", DISCLOSURE_LOW)):
        hit = [w for w in words if w in text]
        if hit:
            return severity, hit
    return None, []


def disclosure_signal(rcept_no: str, report_nm: str | None, *, corp_name: str | None = None, rcept_dt: str | None = None,
                      url: str | None = None, flr_nm: str | None = None) -> Signal | None:
    """DART 공시 한 건 → DISCLOSURE 신호. 일반 공시는 None. fingerprint는 rcept_no라 같은 공시는 한 번만 남는다."""
    severity, hit = disclosure_severity(report_nm)
    if severity is None:
        return None
    return Signal("DISCLOSURE", severity, (rcept_no or "").strip()[:200], (report_nm or "").strip()[:500] or "(제목 없음)",
                  {"rcept_no": rcept_no, "report_nm": report_nm, "corp_name": corp_name, "rcept_dt": rcept_dt,
                   "flr_nm": flr_nm, "url": url, "matched": hit})


# ---------------------------------------------------------------------- 조달청 부정당업자 신호 (M3-2)

def debarment_signal(d: Debarment) -> Signal:
    """입찰참가자격 제한 한 건 → DEBARMENT HIGH. fingerprint는 제재 시작일 + 기관 (둘 다 없으면 원본의 해시)."""
    fingerprint = f"{d.start_date or ''}:{d.agency or ''}"
    if fingerprint == ":":
        fingerprint = "raw:" + hashlib.sha256(json.dumps(d.raw, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:40]
    title = f"입찰참가자격 제한 ({d.period}, {d.agency or '기관 미상'})"
    return Signal("DEBARMENT", "HIGH", fingerprint, title,
                  {"biz_no": d.biz_no, "company_name": d.company_name, "reason": d.reason, "start_date": d.start_date,
                   "end_date": d.end_date, "agency": d.agency, "raw": d.raw})
