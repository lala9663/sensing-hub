"""LLM 무료 한도 나눠 쓰기 (llm_budget, V19): 429 해석, 보강 하루 상한(워커 2대 합산), 429 뒤 쉬기, 장부 기록.

가짜 LLM 서버는 OpenAiCompatLlm에 단 httpx.MockTransport다 (실제 API는 부르지 않는다). DB 테스트는 SENSING_TEST_DSN이 있을 때만
돈다 (test_heartbeat_db.py와 같은 방식: 스키마를 지우고 마이그레이션을 다시 적용한다).
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import httpx
import psycopg
import pytest
from psycopg.rows import dict_row

from sensing_worker import llm as llm_module
from sensing_worker.config import Settings
from sensing_worker.llm import (DAILY, MINUTE, RATE_LIMITED, AnthropicLlm, LlmPaused, LlmRateLimited, LlmUnavailable,
                                OpenAiCompatLlm, parse_rate_limit)
from sensing_worker.llm_budget import CAP, ENRICH, RECIPE, LlmLedger, MeteredLlm, metered

DSN = os.getenv("SENSING_TEST_DSN")
MIGRATIONS = pathlib.Path(__file__).resolve().parents[2] / "backend/src/main/resources/db/migration"
db = pytest.mark.skipif(not DSN, reason="SENSING_TEST_DSN not set")
TZ = "America/Los_Angeles"
MODEL = "gemini-test-lite"

# Gemini OpenAI 호환 주소의 하루 한도 429 (2026-10-19 실측 모양: 배열로 감싸고 QuotaFailure·RetryInfo를 준다)
GEMINI_DAILY_429 = json.dumps([{"error": {
    "code": 429, "status": "RESOURCE_EXHAUSTED",
    "message": "You exceeded your current quota. Quota exceeded for metric: "
               "generativelanguage.googleapis.com/generate_content_free_tier_requests, limit: 500, model: gemini-test-lite",
    "details": [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{
            "quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
            "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
            "quotaDimensions": {"location": "global", "model": "gemini-test-lite"}, "quotaValue": "500"}]},
        {"@type": "type.googleapis.com/google.rpc.Help", "links": [{"description": "Learn more"}]},
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "60554s"}]}}])
GEMINI_MINUTE_429 = json.dumps([{"error": {
    "code": 429, "status": "RESOURCE_EXHAUSTED", "message": "Resource has been exhausted. Please retry in 41.3s.",
    "details": [{"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [
        {"quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier", "quotaValue": "15"}]}]}}])


def ok_body(text: str = '{"items": []}', prompt_tokens: int = 120, completion_tokens: int = 30) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}}


class FakeServer:
    """가짜 OpenAI 호환 LLM 서버. script가 바닥나면 마지막 답을 계속 준다. 받은 요청 수를 센다 (스레드 안전)."""

    def __init__(self, *replies: httpx.Response | dict):
        self.replies = list(replies) or [ok_body()]
        self.calls = 0
        self._lock = threading.Lock()

    def handler(self, request: httpx.Request) -> httpx.Response:
        with self._lock:
            self.calls += 1
            reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return reply if isinstance(reply, httpx.Response) else httpx.Response(200, json=reply)

    def client(self, model: str = MODEL) -> OpenAiCompatLlm:
        return OpenAiCompatLlm("http://llm.test/v1", model, "test-key", transport=httpx.MockTransport(self.handler))


def call(llm) -> str:
    return llm.complete("system", "user", {"type": "object"})


# ---------------------------------------------------------------------- 429 해석 (DB 없음)

def test_parse_rate_limit_reads_quota_id_and_retry_delay():
    daily = parse_rate_limit(GEMINI_DAILY_429)
    assert daily.scope == DAILY and daily.quota_id == "GenerateRequestsPerDayPerProjectPerModel-FreeTier"
    assert daily.retry_seconds == 60554

    minute = parse_rate_limit(GEMINI_MINUTE_429)
    assert minute.scope == MINUTE and minute.quota_id.startswith("GenerateRequestsPerMinute")
    assert minute.retry_seconds == 42                                   # "retry in 41.3s" → 올림

    both = parse_rate_limit('{"violations":[{"quotaId":"X-PerMinute"},{"quotaId":"Y-PerDay"}]}')
    assert both.scope == DAILY and both.quota_id == "Y-PerDay"           # 하루 한도가 하나라도 있으면 하루 한도

    other = parse_rate_limit('{"error":{"message":"rate limit"}}', retry_after="7")
    assert other == llm_module.RateLimit(RATE_LIMITED, None, 7)          # Claude·사내 게이트웨이: retry-after 헤더
    assert parse_rate_limit("Too many requests").retry_seconds is None
    assert parse_rate_limit("daily request limit reached").scope == DAILY  # quotaId가 없으면 본문 낱말로
    assert parse_rate_limit("x", retry_after="Wed, 21 Oct 2026 07:28:00 GMT").retry_seconds is None


def test_openai_429_raises_rate_limited_without_retry():
    server = FakeServer(httpx.Response(429, text=GEMINI_DAILY_429))
    with pytest.raises(LlmRateLimited) as e:
        call(server.client())
    assert e.value.limit.scope == DAILY and e.value.limit.retry_seconds == 60554
    assert isinstance(e.value, LlmUnavailable)                         # 등록·보강의 기존 처리(휴리스틱·건너뛰기)를 그대로 탄다
    assert server.calls == 1

    server = FakeServer(httpx.Response(429, text='{"error":"slow down"}', headers={"retry-after": "9"}))
    with pytest.raises(LlmRateLimited) as e:
        call(server.client())
    assert e.value.limit == llm_module.RateLimit(RATE_LIMITED, None, 9)


def test_anthropic_429_is_rate_limited(monkeypatch):
    class FakeStatusError(Exception):
        def __init__(self):
            super().__init__("rate_limit_error")
            self.status_code = 429
            self.response = httpx.Response(429, headers={"retry-after": "12"}, text='{"type":"rate_limit_error"}')

    class Client:
        class beta:
            class messages:
                @staticmethod
                def create(**kwargs):
                    raise FakeStatusError()

    monkeypatch.setattr(llm_module, "_anthropic_errors", lambda: (FakeStatusError,))
    with pytest.raises(LlmRateLimited) as e:
        AnthropicLlm("claude-test", client=Client()).complete("s", "u", {})
    assert e.value.limit == llm_module.RateLimit(RATE_LIMITED, None, 12)


def test_metered_leaves_no_llm_unwrapped():
    from sensing_worker.llm import NoLlm
    assert isinstance(metered(NoLlm(), object(), ENRICH, 300), NoLlm)
    inner = FakeServer().client()
    assert metered(inner, None, ENRICH, 300) is inner


def test_settings_defaults():
    s = Settings()
    assert s.enrich_daily_calls == 300 and s.llm_quota_tz == "America/Los_Angeles"


# ---------------------------------------------------------------------- DB

@pytest.fixture()
def conn():
    with psycopg.connect(DSN, autocommit=True, row_factory=dict_row) as c:
        c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        for path in sorted(MIGRATIONS.glob("V*__*.sql"), key=lambda p: int(p.name[1:].split("__")[0])):
            c.execute(path.read_text(encoding="utf-8"))
        yield c


@pytest.fixture()
def ledgers():
    """워커 두 대: 각자 연결을 가진 장부."""
    a, b = LlmLedger(DSN, TZ), LlmLedger(DSN, TZ)
    yield a, b
    a.close()
    b.close()


def usage(c, purpose: str = ENRICH, model: str = MODEL):
    return c.execute("SELECT * FROM llm_usage WHERE quota_day = (now() AT TIME ZONE %s)::date AND model = %s AND purpose = %s",
                     (TZ, model, purpose)).fetchone()


def cooldown(c, model: str = MODEL):
    return c.execute("SELECT *, EXTRACT(EPOCH FROM until_at - now())::float8 AS left_seconds FROM llm_cooldown "
                     "WHERE model = %s", (model,)).fetchone()


def next_reset(c):
    return c.execute("SELECT (((now() AT TIME ZONE %(tz)s)::date + 1)::timestamp AT TIME ZONE %(tz)s) AS r",
                     {"tz": TZ}).fetchone()["r"]


@db
def test_two_workers_share_the_enrich_daily_cap(conn, ledgers):
    server = FakeServer(ok_body(prompt_tokens=100, completion_tokens=10))
    w1 = MeteredLlm(server.client(), ledgers[0], ENRICH, daily_cap=5)
    w2 = MeteredLlm(server.client(), ledgers[1], ENRICH, daily_cap=5)

    # 두 워커가 동시에 10번씩 불러도 합쳐서 5번만 나간다 (예약이 DB에서 원자적)
    def run(w):
        sent = paused = 0
        for _ in range(10):
            try:
                call(w)
                sent += 1
            except LlmPaused as e:
                assert e.pause.reason == CAP
                paused += 1
        return sent, paused

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(run, [w1, w2]))
    assert sum(r[0] for r in results) == 5 and sum(r[1] for r in results) == 15
    assert server.calls == 5
    row = usage(conn)
    assert row["calls"] == 5 and row["daily_cap"] == 5 and row["rate_limited"] == 0
    assert row["input_tokens"] == 500 and row["output_tokens"] == 50

    # 상한에 닿으면 다음 리셋(태평양 시간 자정)까지 쉰다
    pause = w1.paused()
    assert pause.reason == CAP and pause.until == next_reset(conn) and pause.calls == 5

    # 레시피는 상한 없이 남은 한도를 쓴다 (따로 센다)
    recipe = MeteredLlm(server.client(), ledgers[0], RECIPE)
    assert recipe.paused() is None
    call(recipe)
    assert server.calls == 6 and usage(conn, RECIPE)["calls"] == 1 and usage(conn, RECIPE)["daily_cap"] is None


@db
def test_cap_counts_per_quota_day_so_remaining_documents_continue_tomorrow(conn, ledgers):
    conn.execute("INSERT INTO llm_usage (quota_day, model, purpose, calls, daily_cap) "
                 "VALUES ((now() AT TIME ZONE %s)::date - 1, %s, 'enrich', 300, 300)", (TZ, MODEL))
    server = FakeServer()
    w = MeteredLlm(server.client(), ledgers[0], ENRICH, daily_cap=300)
    assert w.paused() is None   # 어제 300번은 오늘과 무관하다
    call(w)
    assert usage(conn)["calls"] == 1 and server.calls == 1


@db
def test_daily_429_pauses_the_model_until_reset_and_no_one_calls_meanwhile(conn, ledgers, caplog):
    server = FakeServer(httpx.Response(429, text=GEMINI_DAILY_429), ok_body())
    w1 = MeteredLlm(server.client(), ledgers[0], ENRICH, daily_cap=300)
    w2 = MeteredLlm(server.client(), ledgers[1], ENRICH, daily_cap=300)
    recipe = MeteredLlm(server.client(), ledgers[1], RECIPE)
    caplog.set_level(logging.INFO, logger="sensing_worker.llm_budget")

    def paused_logs() -> int:
        return sum("paused" in r.message for r in caplog.records)

    with pytest.raises(LlmRateLimited):
        call(w1)
    row = cooldown(conn)
    assert row["reason"] == DAILY and row["quota_id"] == "GenerateRequestsPerDayPerProjectPerModel-FreeTier"
    assert row["retry_seconds"] == 60554 and row["purpose"] == ENRICH
    # 리셋 시각과 retryDelay 중 늦은 쪽까지 쉰다 (짧은 retryDelay를 믿고 루프마다 다시 부르지 않게)
    by_retry = conn.execute("SELECT now() + interval '60554 seconds' AS t").fetchone()["t"]
    assert abs((row["until_at"] - max(next_reset(conn), by_retry)).total_seconds()) < 5
    # 거절된 요청은 한도에 들어가지 않는다: calls가 아니라 rate_limited로 센다
    assert usage(conn)["calls"] == 0 and usage(conn)["rate_limited"] == 1

    # 쉬는 동안: 다른 워커의 보강도, 등록 레시피도 부르지 않는다 (가짜 서버 호출 0)
    for llm in (w1, w2, recipe):
        with pytest.raises(LlmPaused) as e:
            call(llm)
        assert e.value.pause.reason == DAILY
    assert server.calls == 1
    assert paused_logs() == 3   # 클라이언트(워커·용도)마다 쉬기 시작할 때 한 번

    # 워커 루프: 쉬는 중이면 보강을 시작하지도 않고, 루프를 여러 번 돌아도 로그를 더 남기지 않는다
    from sensing_worker import main as m
    started = []
    original = m.enrich_pending
    m.enrich_pending = lambda *a, **k: started.append(1)
    try:
        for _ in range(5):
            m.enrich_idle(None, w2, Settings(enrich_enabled=True))
    finally:
        m.enrich_pending = original
    assert started == [] and server.calls == 1
    assert paused_logs() == 3

    # 풀리면 (리셋 지남) 다시 부르고, 풀렸다고 한 번 알린다
    conn.execute("UPDATE llm_cooldown SET until_at = now() - interval '1 second'")
    caplog.clear()
    assert w2.paused() is None
    assert any("resumed" in r.message for r in caplog.records)
    call(w2)
    assert server.calls == 2 and usage(conn)["calls"] == 1


@db
def test_daily_429_without_retry_delay_waits_for_the_quota_day_reset(conn, ledgers):
    body = '{"error":{"message":"Quota exceeded","details":[{"violations":[{"quotaId":"GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}]}}'
    server = FakeServer(httpx.Response(429, text=body))
    with pytest.raises(LlmRateLimited):
        call(MeteredLlm(server.client(), ledgers[0], ENRICH))
    assert cooldown(conn)["until_at"] == next_reset(conn)


@db
def test_minute_429_pauses_only_for_retry_delay(conn, ledgers):
    server = FakeServer(httpx.Response(429, text=GEMINI_MINUTE_429), ok_body())
    w = MeteredLlm(server.client(), ledgers[0], ENRICH, daily_cap=300)
    with pytest.raises(LlmRateLimited):
        call(w)
    row = cooldown(conn)
    assert row["reason"] == MINUTE and row["retry_seconds"] == 42
    assert 30 < row["left_seconds"] <= 42
    with pytest.raises(LlmPaused):
        call(w)
    assert server.calls == 1
    conn.execute("UPDATE llm_cooldown SET until_at = now() - interval '1 second'")   # 42초가 지났다
    call(w)
    assert server.calls == 2


@db
def test_other_provider_429_uses_retry_after_and_shorter_pause_never_cuts_a_daily_one(conn, ledgers):
    server = FakeServer(httpx.Response(429, text='{"error":"rate"}', headers={"retry-after": "7"}))
    with pytest.raises(LlmRateLimited):
        call(MeteredLlm(server.client(), ledgers[0], RECIPE))
    row = cooldown(conn)
    assert row["reason"] == RATE_LIMITED and 0 < row["left_seconds"] <= 7 and row["purpose"] == RECIPE

    ledger = ledgers[0]
    daily = ledger.cool_down(MODEL, ENRICH, parse_rate_limit(GEMINI_DAILY_429))
    assert daily.reason == DAILY
    later = ledger.cool_down(MODEL, ENRICH, parse_rate_limit(GEMINI_MINUTE_429))   # 동시에 나간 요청이 분당 429
    assert later.reason == DAILY and later.until == daily.until


@db
def test_failures_and_unknown_models_are_recorded_per_model(conn, ledgers):
    server = FakeServer(httpx.Response(503, text="overloaded"), ok_body())
    w = MeteredLlm(server.client(), ledgers[0], ENRICH, daily_cap=300)
    with pytest.raises(LlmUnavailable) as e:
        call(w)
    assert not isinstance(e.value, (LlmPaused, LlmRateLimited))
    assert usage(conn)["calls"] == 1 and usage(conn)["errors"] == 1 and cooldown(conn) is None

    # 쉬는 중 기록은 모델별: 다른 모델은 영향이 없다 (챗봇만 SENSING_CHAT_LLM_MODEL로 다른 모델을 쓸 때)
    ledgers[0].cool_down("other-model", "chat", parse_rate_limit(GEMINI_DAILY_429))
    assert w.paused() is None
    call(w)
    assert usage(conn)["calls"] == 2


@db
def test_unknown_quota_timezone_stops_at_start(conn):
    with pytest.raises(RuntimeError, match="SENSING_LLM_QUOTA_TZ"):
        LlmLedger(DSN, "Mars/Olympus_Mons")


@db
def test_enrich_pending_keeps_documents_for_later_when_paused(conn, ledgers):
    """보강 도중 상한에 닿으면 아무것도 쓰지 않고(문서는 다음 날 다시 대상) 넘어간다."""
    from sensing_worker.db import Store
    from sensing_worker.enrich import enrich_pending
    site = conn.execute("INSERT INTO site (domain, base_url) VALUES ('a.test', 'https://a.test') RETURNING id").fetchone()["id"]
    source = conn.execute("INSERT INTO source (site_id, name, requested_by) VALUES (%s, 'a', 2) RETURNING id", (site,)).fetchone()["id"]
    conn.execute("INSERT INTO document (source_id, url, url_hash, title, body_text) "
                 "VALUES (%s, 'https://a.test/1', 'h1', '제목', '본문')", (source,))
    conn.execute("INSERT INTO llm_usage (quota_day, model, purpose, calls, daily_cap) "
                 "VALUES ((now() AT TIME ZONE %s)::date, %s, 'enrich', 1, 1)", (TZ, MODEL))
    server = FakeServer()
    store = Store(DSN)
    try:
        out = enrich_pending(store, MeteredLlm(server.client(), ledgers[0], ENRICH, daily_cap=1), Settings(enrich_batch=5))
    finally:
        store.close()
    assert out.llm_unavailable and out.done == 0 and server.calls == 0
    assert conn.execute("SELECT count(*) AS n FROM document_enrichment").fetchone()["n"] == 0
    assert usage(conn)["calls"] == 1
