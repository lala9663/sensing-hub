"""임베딩 단계 단위 테스트 (DB 없이). OpenAI 호환 /embeddings는 httpx.MockTransport로 흉내 내 실제 API를 부르지 않는다."""

from __future__ import annotations

import json

import httpx
import pytest

from sensing_worker.config import Settings
from sensing_worker.embed import (COLUMN_DIMENSIONS, EmbedUnavailable, NoEmbedder, OpenAiCompatEmbedder, Pacer,
                                  describe, embed_text, make_embedder)


def vec(first: float, dims: int = COLUMN_DIMENSIONS) -> list[float]:
    return [first] + [0.0] * (dims - 1)


def server(handler) -> tuple[list[dict], httpx.MockTransport]:
    seen: list[dict] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append({"url": str(request.url), "auth": request.headers.get("authorization"), "body": body})
        return handler(body)

    return seen, httpx.MockTransport(wrapped)


def test_embedder_sends_model_dimensions_and_reads_gemini_style_indexes():
    # Gemini OpenAI 호환 주소는 첫 항목의 index(0)를 빼고 준다. 순서가 섞여 와도 index대로 맞춘다
    seen, transport = server(lambda body: httpx.Response(200, json={"object": "list", "data": [
        {"object": "embedding", "index": 2, "embedding": vec(0.3)},
        {"object": "embedding", "embedding": vec(0.1)},
        {"object": "embedding", "index": 1, "embedding": vec(0.2)},
    ]}))
    e = OpenAiCompatEmbedder("https://gen.example/v1beta/openai/", "gemini-embedding-001", "k-1", transport=transport)
    out = e.embed(["가", "나", "다"])
    assert [v[0] for v in out] == [0.1, 0.2, 0.3]
    assert seen[0]["url"] == "https://gen.example/v1beta/openai/embeddings"
    assert seen[0]["auth"] == "Bearer k-1"
    assert seen[0]["body"] == {"model": "gemini-embedding-001", "input": ["가", "나", "다"], "encoding_format": "float",
                               "dimensions": 1024}
    assert e.identity == "gemini-embedding-001/1024"
    assert describe(e) == "openai gemini-embedding-001/1024"
    assert e.embed([]) == [] and len(seen) == 1   # 빈 묶음은 부르지 않는다


def test_server_without_dimensions_support_is_retried_once_without_it_and_remembered():
    def handler(body):
        if "dimensions" in body:
            return httpx.Response(400, json={"error": {"message": "This model does not support matryoshka"}})
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": vec(1.0)}]})

    seen, transport = server(handler)
    e = OpenAiCompatEmbedder("http://vllm:8000/v1", "bge-m3", transport=transport)
    assert e.embed(["하나"])[0][0] == 1.0
    assert e.embed(["둘"])[0][0] == 1.0
    assert ["dimensions" in s["body"] for s in seen] == [True, False, False]
    assert seen[0]["auth"] is None   # 키가 없으면 인증 헤더를 보내지 않는다


@pytest.mark.parametrize("response, message", [
    (httpx.Response(200, json={"data": [{"index": 0, "embedding": vec(1.0, 3072)}]}), "임베딩 차원이 3072"),
    (httpx.Response(200, json={"data": []}), "1개를 보냈는데 0개"),
    (httpx.Response(200, text="<html>gateway</html>"), "형식을 해석할 수 없습니다"),
    (httpx.Response(429, json={"error": {"message": "Quota exceeded"}}), "HTTP 429"),
    (httpx.Response(503, text="overloaded"), "HTTP 503"),
])
def test_embedder_errors_become_unavailable(response, message):
    _, transport = server(lambda body: response)
    e = OpenAiCompatEmbedder("http://x/v1", "m", transport=transport)
    e._send_dimensions = False   # 400 재시도 경로와 섞지 않는다
    with pytest.raises(EmbedUnavailable, match=message):
        e.embed(["글"])


def test_connection_failure_is_unavailable():
    def refuse(request):
        raise httpx.ConnectError("refused", request=request)

    e = OpenAiCompatEmbedder("http://127.0.0.1:9/v1", "m", transport=httpx.MockTransport(refuse))
    with pytest.raises(EmbedUnavailable, match="연결 실패"):
        e.embed(["글"])


def test_make_embedder_reuses_llm_address_and_key_only_when_no_own_address():
    base = dict(llm_base_url="https://gen.example/v1beta/openai", llm_api_key="llm-key")
    assert isinstance(make_embedder(Settings(embed_provider="none", **base)), NoEmbedder)
    assert make_embedder(Settings(embed_provider="none")).identity is None

    shared = make_embedder(Settings(embed_provider="openai", embed_model="gemini-embedding-001", **base))
    assert shared.base_url == "https://gen.example/v1beta/openai"
    assert shared._client.headers["authorization"] == "Bearer llm-key"

    own = make_embedder(Settings(embed_provider="openai", embed_model="bge-m3", embed_base_url="http://vllm/v1", **base))
    assert own.base_url == "http://vllm/v1" and "authorization" not in own._client.headers   # LLM 키를 다른 서버에 보내지 않는다
    own_key = make_embedder(Settings(embed_provider="openai", embed_model="bge-m3", embed_base_url="http://vllm/v1",
                                     embed_api_key="embed-key", **base))
    assert own_key._client.headers["authorization"] == "Bearer embed-key"

    with pytest.raises(RuntimeError, match="SENSING_EMBED_MODEL"):
        make_embedder(Settings(embed_provider="openai", **base))
    with pytest.raises(RuntimeError, match="BASE_URL"):
        make_embedder(Settings(embed_provider="openai", embed_model="m", llm_base_url=""))
    with pytest.raises(RuntimeError, match="1024"):
        make_embedder(Settings(embed_provider="openai", embed_model="m", embed_dimensions=768, **base))
    with pytest.raises(RuntimeError, match="none \\| openai"):
        make_embedder(Settings(embed_provider="cohere"))


def row(seq: int, text: str, title: str | None, site_type: str = "PUBLIC", ai_restricted: bool = False) -> dict:
    return {"id": 1, "document_id": 1, "seq": seq, "text": text, "title": title, "site_type": site_type,
            "ai_restricted": ai_restricted}


def test_embed_text_sends_titles_and_titled_body_chunks_but_never_restricted_bodies():
    title = "2026년  반도체\n지원사업 공고"
    assert embed_text(row(0, "2026년 반도체 지원사업 공고", title)) == "2026년 반도체 지원사업 공고"
    assert embed_text(row(1, "신청 요건과 일정을 안내합니다.", title)) == "2026년 반도체 지원사업 공고\n신청 요건과 일정을 안내합니다."
    # 언론 기사·AI 봇을 막은 사이트: 제목만, 본문 조각은 보내지 않는다
    assert embed_text(row(0, "반도체 수출 사상 최대", "반도체 수출 사상 최대", "NEWS")) == "반도체 수출 사상 최대"
    assert embed_text(row(1, "언론 기사 본문", "반도체 수출 사상 최대", "NEWS")) is None
    assert embed_text(row(0, "청년 정책 발표", "청년 정책 발표", ai_restricted=True)) == "청년 정책 발표"
    assert embed_text(row(1, "막힌 사이트 본문", "청년 정책 발표", ai_restricted=True)) is None
    # 제목 없는 문서의 첫 조각은 본문이다
    assert embed_text(row(0, "제목 없는 본문", None, ai_restricted=True)) is None
    assert embed_text(row(0, "제목 없는 본문", None)) == "제목 없는 본문"


def test_pacer_spaces_calls_by_texts_per_minute_and_backs_off_on_failure():
    p = Pacer(per_minute=80, backoff_seconds=60, cap_seconds=200)
    assert p.ready(0)
    p.sent(20, now=0)                       # 20개 = 80개/분의 1/4 → 15초 뒤
    assert not p.ready(14.9) and p.ready(15)
    assert p.failed(15) == 60 and not p.ready(74) and p.ready(75)
    assert p.failed(75) == 120 and p.failed(195) == 200   # 실패가 이어지면 두 배씩, 상한
    p.sent(0, now=400)                      # 성공하면 처음으로
    assert p.ready(400) and p.failed(400) == 60
    unlimited = Pacer(per_minute=0, backoff_seconds=60)
    unlimited.sent(1000, now=0)
    assert unlimited.ready(0)


def test_backfill_waits_between_batches_and_stops_on_failure(monkeypatch):
    from fakeembed import FakeEmbedder, unavailable
    from sensing_worker import embed as embed_mod

    batches = [3, 3, 1, 0]
    limits: list[int] = []

    def fake_pending(store, embedder, settings, limit):
        limits.append(limit)
        n = batches.pop(0)
        return embed_mod.EmbedOutcome(fetched=n, embedded=n)

    slept: list[float] = []
    monkeypatch.setattr(embed_mod, "embed_pending", fake_pending)
    out = embed_mod.backfill(None, FakeEmbedder(), Settings(embed_batch=3, embed_per_minute=6000), sleep=slept.append)
    assert (out.fetched, out.embedded) == (7, 7) and limits == [3, 3, 3, 3]
    assert len(slept) == 3 and all(0 < s <= 0.031 for s in slept)   # 3개 = 6000개/분에서 0.03초

    monkeypatch.setattr(embed_mod, "embed_pending",
                        lambda *a: embed_mod.EmbedOutcome(fetched=2, unavailable=True, error=str(unavailable())))
    out = embed_mod.backfill(None, FakeEmbedder(), Settings(embed_batch=3, embed_per_minute=0), sleep=slept.append)
    assert out.unavailable and "429" in out.error and out.fetched == 2
