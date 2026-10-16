"""회사(거래처) 레이어의 규칙 (M3). DB·HTTP 없이 순수 함수만 두어 단위 테스트로 고정한다.

- 문서 → 회사 연결: 보강 태그(고유명사)가 회사 이름·별칭과 **정확히**(대소문자·공백 무시) 같을 때만 TAG로 연결한다.
  부분 일치·유사도 매칭은 하지 않는다 (오탐으로 엉뚱한 회사에 신호가 붙는 것이 누락보다 나쁘다).
- 회사 신호(company_signal)의 종류·심각도·fingerprint:
  · BIZ_STATUS (국세청 사업자 상태): 폐업 HIGH, 휴업 HIGH, 미등록 MEDIUM, 과세유형 변경 LOW, 영업 재개(휴·폐업 → 계속) LOW.
    fingerprint는 b_stt_cd + end_dt (상태가 같으면 매일 다시 기록하지 않는다), 과세유형 변경은 "tax:" + 새 과세유형 코드.
  · NEWS (회사 감시·태그로 연결된 기사): 보강 토픽이 사건·사고 또는 인사·조직이면 MEDIUM, fingerprint는 url_hash.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

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
