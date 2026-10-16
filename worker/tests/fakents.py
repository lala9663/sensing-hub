"""테스트용 가짜 국세청 사업자등록 상태조회 API (httpx.MockTransport 핸들러). 실제 api.odcloud.kr에는 요청하지 않는다."""

from __future__ import annotations

import json

import httpx

GOOD_KEY = "dataGoKr/serviceKey+abc123=="   # 공공데이터포털 키(디코딩 형식)처럼 URL에서 인코딩이 필요한 문자가 섞여 있다
UNREGISTERED_TAX_TYPE = "국세청에 등록되지 않은 사업자등록번호입니다."


def continuing(b_no: str, tax_type: str = "부가가치세 일반과세자", tax_type_cd: str = "01") -> dict:
    return {"b_no": b_no, "b_stt": "계속사업자", "b_stt_cd": "01", "tax_type": tax_type, "tax_type_cd": tax_type_cd,
            "end_dt": "", "utcc_yn": "N", "tax_type_change_dt": "", "invoice_apply_dt": ""}


def suspended(b_no: str) -> dict:
    return {"b_no": b_no, "b_stt": "휴업자", "b_stt_cd": "02", "tax_type": "부가가치세 일반과세자", "tax_type_cd": "01",
            "end_dt": "", "utcc_yn": "N", "tax_type_change_dt": "", "invoice_apply_dt": ""}


def closed(b_no: str, end_dt: str = "20260930") -> dict:
    return {"b_no": b_no, "b_stt": "폐업자", "b_stt_cd": "03", "tax_type": "부가가치세 일반과세자", "tax_type_cd": "01",
            "end_dt": end_dt, "utcc_yn": "N", "tax_type_change_dt": "", "invoice_apply_dt": ""}


def unregistered(b_no: str) -> dict:
    return {"b_no": b_no, "b_stt": "", "b_stt_cd": "", "tax_type": UNREGISTERED_TAX_TYPE, "tax_type_cd": "",
            "end_dt": "", "utcc_yn": "", "tax_type_change_dt": "", "invoice_apply_dt": ""}


def nts_handler(statuses: dict[str, dict] | None = None, *, http_status: int | None = None):
    """serviceKey가 GOOD_KEY면 요청한 b_no마다 statuses에서 찾아 답하고(없으면 미등록), 아니면 401.
    http_status로 429·5xx 장애를 흉내 낸다. handler.calls에 (b_no 목록, 요청 메서드)를, handler.urls에 URL 문자열을 모은다."""
    by_no = dict(statuses or {})

    def handler(request: httpx.Request) -> httpx.Response:
        handler.urls.append(str(request.url))
        if request.url.host != "api.odcloud.kr" or request.url.path != "/api/nts-businessman/v1/status":
            return httpx.Response(404, text="not found")
        if request.method != "POST":
            return httpx.Response(405, json={"msg": "POST only"})
        body = json.loads(request.content or b"{}")
        b_nos = list(body.get("b_no") or [])
        handler.calls.append((b_nos, request.method))
        if http_status is not None:
            return httpx.Response(http_status, json={"status_code": "ERROR", "msg": f"상태 {http_status}"})
        key = request.url.params.get("serviceKey", "")   # 커넥터가 인코딩해 보낸 것을 서버처럼 디코딩해 비교한다
        if key != GOOD_KEY:
            # 실제 API는 키를 되돌려 주지 않지만, 커넥터가 응답 본문도 가리는지 보려고 일부러 넣는다
            return httpx.Response(401, json={"code": -4, "msg": f"등록되지 않은 인증키입니다. (key={key})"})
        if len(b_nos) > 100:
            return httpx.Response(400, json={"status_code": "BAD_JSON_REQUEST", "msg": "최대 100건"})
        data = [dict(by_no.get(n) or unregistered(n)) for n in b_nos]
        return httpx.Response(200, json={"status_code": "OK", "request_cnt": len(b_nos), "match_cnt": len(data), "data": data})

    handler.calls = []
    handler.urls = []
    return handler
