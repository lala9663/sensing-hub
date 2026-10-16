"""테스트용 가짜 DART 오픈API (httpx.MockTransport 핸들러). 실제 opendart.fss.or.kr에는 요청하지 않는다."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx

GOOD_KEY = "good-key-0123456789abcdef"
KST = timezone(timedelta(hours=9))


def disclosures(n: int = 3, corp: str = "테스트전자") -> list[dict]:
    today = datetime.now(KST).date()
    return [{"corp_code": "00126380", "corp_name": corp, "stock_code": "005930", "corp_cls": "Y",
             "report_nm": f"주요사항보고서(유상증자결정) {i}", "rcept_no": f"2026101500000{i}", "flr_nm": corp,
             "rcept_dt": (today - timedelta(days=i % 2)).strftime("%Y%m%d"), "rm": ""} for i in range(1, n + 1)]


def dart_handler(items: list[dict] | None = None, *, server_status: int | None = None, api_status: str | None = None,
                 per_page: int = 100):
    """crtfc_key가 GOOD_KEY면 정상 목록, 아니면 010(등록되지 않은 키). server_status/api_status로 장애를 흉내 낸다.
    핸들러 객체의 calls에 받은 요청 URL을 모은다 (키 노출 검사용이 아니라 호출 횟수·파라미터 확인용)."""
    rows = disclosures() if items is None else items

    def handler(request: httpx.Request) -> httpx.Response:
        handler.calls.append(str(request.url))
        if request.url.host != "opendart.fss.or.kr" or request.url.path != "/api/list.json":
            return httpx.Response(404, text="not found")
        if server_status is not None:
            return httpx.Response(server_status, text="<html><body>DART 점검 중</body></html>")
        if api_status is not None:
            return httpx.Response(200, json={"status": api_status, "message": f"상태 {api_status}"})
        params = dict(request.url.params)
        if params.get("crtfc_key") != GOOD_KEY:
            return httpx.Response(200, json={"status": "010", "message": "등록되지 않은 키입니다."})
        if not rows:
            return httpx.Response(200, json={"status": "013", "message": "조회된 데이타가 없습니다."})
        page_no, count = int(params.get("page_no", "1")), min(int(params.get("page_count", "100")), per_page)
        total_page = max(1, -(-len(rows) // count))
        page = rows[(page_no - 1) * count: page_no * count]
        body = {"status": "000", "message": "정상", "page_no": page_no, "page_count": count, "total_count": len(rows),
                "total_page": total_page, "list": page}
        return httpx.Response(200, text=json.dumps(body, ensure_ascii=False),
                              headers={"content-type": "application/json;charset=UTF-8"})

    handler.calls = []
    return handler
