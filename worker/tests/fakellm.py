"""테스트용 가짜 LLM: 미리 정한 문자열을 차례로 돌려주거나 LlmUnavailable을 낸다. 실제 API는 호출하지 않는다."""

from __future__ import annotations

import json

from sensing_worker.llm import LlmUnavailable


class FakeLlm:
    provider = "fake"
    model = "fake-1"

    def __init__(self, responses: list[str] | None = None, error: Exception | None = None):
        self.responses = list(responses or [])
        self.error = error
        self.calls: list[tuple[str, str, dict]] = []   # (system, user, schema)
        self.last_usage = {"input_tokens": 1000, "output_tokens": 50}

    def complete(self, system: str, user: str, schema: dict) -> str:
        self.calls.append((system, user, schema))
        if self.error is not None:
            raise self.error
        if not self.responses:
            raise LlmUnavailable("가짜 LLM에 남은 응답이 없습니다")
        # 응답이 여럿이면 차례로 소비하고, 마지막 것은 계속 돌려준다
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]


class BatchFakeLlm(FakeLlm):
    """보강(enrich) 묶음 프롬프트에 맞춰 답하는 가짜 LLM: 프롬프트의 [문서 N]마다 항목 하나를 만든다.

    make(idx, prompt) -> dict 로 항목을 바꿀 수 있다. 기본은 요약 "요약 N", 토픽 ["정책·규제", "엉뚱한분류"], 태그 7개.
    """

    def __init__(self, make=None, error: Exception | None = None):
        super().__init__(error=error)
        self.make = make or (lambda idx, prompt: {
            "idx": idx, "summary": f"요약 {idx}", "topics": ["정책·규제", "엉뚱한분류"],
            "tags": [f"태그{idx}-{k}" for k in range(1, 8)]})

    def complete(self, system: str, user: str, schema: dict) -> str:
        self.calls.append((system, user, schema))
        if self.error is not None:
            raise self.error
        import re
        idxs = [int(m) for m in re.findall(r"^\[문서 (\d+)\]", user, re.M)]
        return json.dumps({"items": [self.make(i, user) for i in idxs]}, ensure_ascii=False)


def portal_recipe(base: str, **overrides) -> str:
    """fakesite.portal_cms_routes 사이트에 맞는 레시피 JSON."""
    recipe = {
        "hubs": [f"{base}/portal/bbs/list.do?bbsId=B01"],
        "article_signature": "/portal/bbs/view.do?bbsId=*&nttId={n}",
        "selectors": {"title": ".headline-area strong", "date": ".reg", "body": ".cont-area", "attachment": None},
        "date_format": "%Y.%m.%d",
        "notes": "보도자료 목록이 허브이고 상세 제목은 .headline-area, 등록일은 라벨 없이 .reg에 있다.",
    }
    recipe.update(overrides)
    return json.dumps(recipe, ensure_ascii=False)
