"""테스트용 가짜 조달청 나라장터 부정당업자 조회 API (httpx.MockTransport 핸들러). 실제 apis.data.go.kr에는 요청하지 않는다.

응답 봉투는 공공데이터포털 1230000 서비스의 관례({"response":{"header":{"resultCode","resultMsg"},"body":{"items","totalCount",…}}})를
따르고, 게이트웨이 오류(키 미등록 30, 한도 초과 22)는 실제처럼 HTTP 200 + XML로 보낸다. items의 항목명은 실제 스펙이 확인되지
않았으므로(apis/g2b_debarment.py 참고) 커넥터의 후보 목록 중 하나와, 후보에 없는 이름(config fields로 바꾸는 검사용)을 섞어 쓴다.
"""

from __future__ import annotations

import json

import httpx

GOOD_KEY = "dataGoKr/serviceKey+abc123=="
PATH = "/1230000/ao/UsrInfoService02/getUnptRsttCorpInfo02"
GATEWAY_XML = ("<OpenAPI_ServiceResponse><cmmMsgHeader><errMsg>SERVICE ERROR</errMsg><returnAuthMsg>{msg}</returnAuthMsg>"
               "<returnReasonCode>{code}</returnReasonCode></cmmMsgHeader></OpenAPI_ServiceResponse>")


def sanction(biz_no: str, company: str, *, start: str = "20260101", end: str = "20260630", agency: str = "조달청",
             reason: str = "계약의 이행을 하지 아니한 자") -> dict:
    """제재 한 건. 사업자번호·업체명은 후보 1순위 이름, 날짜·기관·사유는 후보 뒷순위 이름으로 둔다 (후보 목록이 실제로 쓰이는지)."""
    return {"bizno": biz_no, "corpNm": company, "lmtBgnDt": start, "lmtEndDt": end, "prcrmntInsttNm": agency,
            "sanctnRsn": reason, "rgstDt": "202601011200"}


def g2b_handler(sanctions: dict[str, list[dict]] | None = None, *, http_status: int | None = None,
                gateway_code: str | None = None, per_page: int = 100):
    """ServiceKey가 GOOD_KEY면 inqryDiv=1은 bizno의 제재 목록을, 그 밖(기간 조회)은 전체를 페이징해 답한다.
    키가 틀리면 게이트웨이 30(XML, HTTP 200). http_status로 401/429/5xx, gateway_code로 22 등을 흉내 낸다.
    handler.calls에 (inqryDiv, bizno, pageNo)를, handler.urls에 URL 문자열을 모은다."""
    by_no = {k: list(v) for k, v in (sanctions or {}).items()}

    def handler(request: httpx.Request) -> httpx.Response:
        handler.urls.append(str(request.url))
        if request.url.host != "apis.data.go.kr" or request.url.path != PATH:
            return httpx.Response(404, text="not found")
        params = dict(request.url.params)
        handler.calls.append((params.get("inqryDiv"), params.get("bizno"), params.get("pageNo")))
        if http_status is not None:
            return httpx.Response(http_status, text=f"<html><body>HTTP {http_status}</body></html>")
        if gateway_code is not None:
            return httpx.Response(200, text=GATEWAY_XML.format(code=gateway_code, msg="LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR"),
                                  headers={"content-type": "application/xml;charset=UTF-8"})
        if params.get("ServiceKey") != GOOD_KEY:
            # 실제 게이트웨이는 키를 되돌려 주지 않지만 커넥터가 본문도 가리는지 보려고 일부러 넣는다
            return httpx.Response(200, text=GATEWAY_XML.format(code="30", msg=f"SERVICE_KEY_IS_NOT_REGISTERED_ERROR key={params.get('ServiceKey')}"),
                                  headers={"content-type": "application/xml;charset=UTF-8"})
        if params.get("inqryDiv") == "1":
            rows = by_no.get(params.get("bizno") or "", [])
        else:
            rows = [r for v in by_no.values() for r in v]
        page_no, size = int(params.get("pageNo", "1")), min(int(params.get("numOfRows", "10")), per_page)
        page = rows[(page_no - 1) * size: page_no * size]
        body = {"response": {"header": {"resultCode": "00", "resultMsg": "정상"},
                             "body": {"items": page, "numOfRows": size, "pageNo": page_no, "totalCount": len(rows)}}}
        return httpx.Response(200, text=json.dumps(body, ensure_ascii=False),
                              headers={"content-type": "application/json;charset=UTF-8"})

    handler.calls = []
    handler.urls = []
    return handler
