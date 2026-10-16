"""테스트용 가짜 DART 오픈API (httpx.MockTransport 핸들러). 실제 opendart.fss.or.kr에는 요청하지 않는다.

- /api/list.json: 공시 목록. corp_code 파라미터가 있으면 그 회사 것만 (회사별 공시 점검용).
- /api/corpCode.xml: 고유번호 전체 목록 ZIP (안에 CORPCODE.xml). 키가 틀리면 ZIP 대신 <result><status>010</status>…</result>.
"""

from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime, timedelta, timezone

import httpx

GOOD_KEY = "good-key-0123456789abcdef"
KST = timezone(timedelta(hours=9))

# (corp_code, corp_name, stock_code). 가전자는 상장사 1 + 비상장 1 (상장사 우선), 다중복은 비상장 2 (모호 → 미채움)
CORPS: list[tuple[str, str, str]] = [
    ("00126380", "테스트전자", "005930"),
    ("00000001", "나부품", ""),
    ("00000002", "가전자", "001234"),
    ("00000003", "가전자", ""),
    ("00000004", "다중복", ""),
    ("00000005", "다중복", ""),
    ("00000006", "(주)라물류", ""),
]


def disclosures(n: int = 3, corp: str = "테스트전자") -> list[dict]:
    today = datetime.now(KST).date()
    return [{"corp_code": "00126380", "corp_name": corp, "stock_code": "005930", "corp_cls": "Y",
             "report_nm": f"주요사항보고서(유상증자결정) {i}", "rcept_no": f"2026101500000{i}", "flr_nm": corp,
             "rcept_dt": (today - timedelta(days=i % 2)).strftime("%Y%m%d"), "rm": ""} for i in range(1, n + 1)]


def disclosure(corp_code: str, corp_name: str, report_nm: str, rcept_no: str, days_ago: int = 0, stock_code: str = "") -> dict:
    """공시 한 건 (회사별 공시 테스트용)."""
    day = datetime.now(KST).date() - timedelta(days=days_ago)
    return {"corp_code": corp_code, "corp_name": corp_name, "stock_code": stock_code, "corp_cls": "Y" if stock_code else "E",
            "report_nm": report_nm, "rcept_no": rcept_no, "flr_nm": corp_name, "rcept_dt": day.strftime("%Y%m%d"), "rm": ""}


def corp_zip(corps: list[tuple[str, str, str]] | None = None, *, xml_name: str = "CORPCODE.xml") -> bytes:
    """corpCode.xml ZIP 바이트. DART 실제 형식(<result><list>…</list></result>)을 흉내 낸다."""
    rows = CORPS if corps is None else corps
    body = ["<?xml version=\"1.0\" encoding=\"UTF-8\"?>", "<result>"]
    for code, name, stock in rows:
        body.append(f"  <list>\n    <corp_code>{code}</corp_code>\n    <corp_name>{name}</corp_name>\n"
                    f"    <stock_code>{stock or ' '}</stock_code>\n    <modify_date>20260101</modify_date>\n  </list>")
    body.append("</result>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(xml_name, "\n".join(body))
    return buf.getvalue()


def dart_handler(items: list[dict] | None = None, *, server_status: int | None = None, api_status: str | None = None,
                 per_page: int = 100, corps: list[tuple[str, str, str]] | None = None):
    """crtfc_key가 GOOD_KEY면 정상 목록, 아니면 010(등록되지 않은 키). server_status/api_status로 장애를 흉내 낸다.
    핸들러 객체의 calls에 받은 요청 URL을 모은다 (키 노출 검사용이 아니라 호출 횟수·파라미터 확인용)."""
    rows = disclosures() if items is None else items

    def handler(request: httpx.Request) -> httpx.Response:
        handler.calls.append(str(request.url))
        if request.url.host != "opendart.fss.or.kr":
            return httpx.Response(404, text="not found")
        if server_status is not None:
            return httpx.Response(server_status, text="<html><body>DART 점검 중</body></html>")
        params = dict(request.url.params)
        if request.url.path == "/api/corpCode.xml":
            if params.get("crtfc_key") != GOOD_KEY:
                return httpx.Response(200, text="<result><status>010</status><message>등록되지 않은 키입니다.</message></result>",
                                      headers={"content-type": "application/xml;charset=UTF-8"})
            return httpx.Response(200, content=corp_zip(corps), headers={"content-type": "application/x-msdownload"})
        if request.url.path != "/api/list.json":
            return httpx.Response(404, text="not found")
        if api_status is not None:
            return httpx.Response(200, json={"status": api_status, "message": f"상태 {api_status}"})
        if params.get("crtfc_key") != GOOD_KEY:
            return httpx.Response(200, json={"status": "010", "message": "등록되지 않은 키입니다."})
        wanted = rows if not params.get("corp_code") else [r for r in rows if r.get("corp_code") == params["corp_code"]]
        if not wanted:
            return httpx.Response(200, json={"status": "013", "message": "조회된 데이타가 없습니다."})
        page_no, count = int(params.get("page_no", "1")), min(int(params.get("page_count", "100")), per_page)
        total_page = max(1, -(-len(wanted) // count))
        page = wanted[(page_no - 1) * count: page_no * count]
        body = {"status": "000", "message": "정상", "page_no": page_no, "page_count": count, "total_count": len(wanted),
                "total_page": total_page, "list": page}
        return httpx.Response(200, text=json.dumps(body, ensure_ascii=False),
                              headers={"content-type": "application/json;charset=UTF-8"})

    handler.calls = []
    return handler
