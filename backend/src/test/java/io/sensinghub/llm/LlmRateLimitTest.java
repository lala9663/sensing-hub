package io.sensinghub.llm;

import static org.assertj.core.api.Assertions.assertThat;

import java.time.OffsetDateTime;
import java.time.ZoneId;
import java.time.ZonedDateTime;

import org.junit.jupiter.api.Test;

import io.sensinghub.llm.LlmException.RateLimit;
import io.sensinghub.llm.LlmLedger.Pause;

/** 429 해석(quotaId·retryDelay·retry-after)과 챗봇의 "쉬는 중" 안내 문장. DB·서버 없이 돈다. */
class LlmRateLimitTest {

    static final String DAILY_WITH_RETRY = """
            [{"error":{"code":429,"message":"You exceeded your current quota.","status":"RESOURCE_EXHAUSTED","details":[\
            {"@type":"type.googleapis.com/google.rpc.QuotaFailure","violations":[\
            {"quotaId":"GenerateRequestsPerDayPerProjectPerModel-FreeTier","quotaValue":"500"}]},\
            {"@type":"type.googleapis.com/google.rpc.RetryInfo","retryDelay":"60554s"}]}}]""";

    @Test
    void geminiQuotaIdTellsDailyFromMinute() {
        assertThat(LlmHttp.rateLimit(DAILY_WITH_RETRY, null))
                .isEqualTo(new RateLimit("DAILY", "GenerateRequestsPerDayPerProjectPerModel-FreeTier", 60554));
        assertThat(LlmHttp.rateLimit(MockLlmServer.GEMINI_DAILY_429, null).daily()).isTrue();
        assertThat(LlmHttp.rateLimit(MockLlmServer.GEMINI_MINUTE_429, null))
                .isEqualTo(new RateLimit("MINUTE", "GenerateRequestsPerMinutePerProjectPerModel-FreeTier", 42));
        assertThat(LlmHttp.rateLimit("{\"violations\":[{\"quotaId\":\"A-PerMinute\"},{\"quotaId\":\"B-PerDay\"}]}", null))
                .isEqualTo(new RateLimit("DAILY", "B-PerDay", null));
        // Claude·사내 게이트웨이: quotaId 없이 retry-after 헤더
        assertThat(LlmHttp.rateLimit("{\"error\":{\"type\":\"rate_limit_error\"}}", "7"))
                .isEqualTo(new RateLimit("RATE_LIMITED", null, 7));
        assertThat(LlmHttp.rateLimit("daily limit reached", null).daily()).isTrue();
    }

    @Test
    void the429ExceptionCarriesTheLimit() {
        LlmException e = LlmHttp.error(429, DAILY_WITH_RETRY, null);
        assertThat(e.rateLimit().daily()).isTrue();
        assertThat(e.getMessage()).contains("하루 사용 한도");
        assertThat(LlmHttp.error(503, "busy", null).rateLimit()).isNull();
    }

    @Test
    void pausedMessageSaysWhenItOpensInKoreanTime() {
        ZonedDateTime now = ZonedDateTime.of(2026, 10, 19, 23, 10, 0, 0, ZoneId.of("Asia/Seoul"));
        // 태평양 시간 자정 = 한국 시간 16:00 (일광 절약 시간)
        OffsetDateTime reset = OffsetDateTime.parse("2026-10-20T00:00:00-07:00");
        assertThat(MeteredLlmClient.message(new Pause("DAILY", reset, null), now))
                .isEqualTo("LLM 하루 사용 한도(무료 등급)를 오늘 다 썼습니다. 한국 시간 내일 16:00쯤 풀리니 그때 다시 시도해 주세요.");
        assertThat(MeteredLlmClient.message(new Pause("DAILY", reset, null), now.plusHours(10)))   // 다음 날 오전 9시 10분
                .contains("한국 시간 16:00쯤");
        assertThat(MeteredLlmClient.when(OffsetDateTime.parse("2026-10-22T00:00:00-07:00"), now)).isEqualTo("10월 22일 16:00");

        OffsetDateTime soon = now.plusSeconds(41).plusNanos(300_000_000).toOffsetDateTime();
        assertThat(MeteredLlmClient.message(new Pause("MINUTE", soon, null), now))
                .isEqualTo("LLM 호출 한도를 잠시 넘었습니다. 약 42초 뒤에 다시 시도해 주세요.");
        assertThat(MeteredLlmClient.message(new Pause("RATE_LIMITED", now.plusMinutes(30).toOffsetDateTime(), null), now))
                .isEqualTo("LLM 호출 한도를 넘었습니다. 한국 시간 23:40쯤 다시 시도해 주세요.");
    }

    @Test
    void unknownQuotaTimeZoneFallsBackToPacific() {
        assertThat(LlmLedger.zone("Mars/Olympus")).isEqualTo("America/Los_Angeles");
        assertThat(LlmLedger.zone(" ")).isEqualTo("America/Los_Angeles");
        assertThat(LlmLedger.zone("Asia/Seoul")).isEqualTo("Asia/Seoul");
    }
}
