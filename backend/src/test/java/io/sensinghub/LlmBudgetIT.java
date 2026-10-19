package io.sensinghub;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.within;

import java.time.OffsetDateTime;
import java.time.ZoneId;
import java.time.format.DateTimeFormatter;
import java.time.temporal.ChronoUnit;
import java.util.Map;

import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.web.client.TestRestTemplate;
import org.springframework.http.HttpEntity;
import org.springframework.http.HttpHeaders;
import org.springframework.http.HttpMethod;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;

import com.fasterxml.jackson.databind.JsonNode;

import io.sensinghub.llm.MockLlmServer;
import io.sensinghub.llm.MockLlmServer.Reply;

/**
 * 챗봇과 워커 요약이 LLM 무료 한도를 나눠 쓴다 (V19 llm_usage·llm_cooldown): 챗봇 호출의 장부 기록, 429 하루 한도 → 리셋까지 쉬기
 * (그동안 LLM 호출 없이 "언제 풀리는지" 안내), 분당 한도 → 짧게 쉬기, 워커가 같은 모델로 받은 429도 챗봇을 쉬게 함, 챗봇 전용 모델
 * (SENSING_CHAT_LLM_MODEL)의 한도 분리, 운영 지표의 오늘 사용량. LLM은 가짜 OpenAI 호환 서버라 실제 호출이 없다.
 * 실제 PostgreSQL이 필요하며 SENSING_IT_DB_URL이 있을 때만 실행된다.
 */
@EnabledIfEnvironmentVariable(named = "SENSING_IT_DB_URL", matches = "jdbc:postgresql:.+")
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT,
        properties = {"sensing.scheduler.enabled=false", "sensing.digest.enabled=false"})
class LlmBudgetIT {

    static final MockLlmServer LLM = MockLlmServer.start();
    static final String WORKER_MODEL = "summary-model";
    static final String CHAT_MODEL = "chat-model";
    static final String TZ = "America/Los_Angeles";
    static final ZoneId KST = ZoneId.of("Asia/Seoul");

    /** Gemini 하루 한도 429 (2026-10-19 실측 모양: QuotaFailure.quotaId·quotaValue와 RetryInfo.retryDelay). */
    static final String GEMINI_DAILY_429_WITH_RETRY = """
            [{"error":{"code":429,"message":"You exceeded your current quota. Quota exceeded for metric: \
            generativelanguage.googleapis.com/generate_content_free_tier_requests, limit: 500, model: chat-model",\
            "status":"RESOURCE_EXHAUSTED","details":[{"@type":"type.googleapis.com/google.rpc.QuotaFailure",\
            "violations":[{"quotaMetric":"generativelanguage.googleapis.com/generate_content_free_tier_requests",\
            "quotaId":"GenerateRequestsPerDayPerProjectPerModel-FreeTier","quotaValue":"500"}]},\
            {"@type":"type.googleapis.com/google.rpc.RetryInfo","retryDelay":"60554s"}]}}]""";

    @DynamicPropertySource
    static void props(DynamicPropertyRegistry r) {
        r.add("spring.datasource.url", () -> System.getenv("SENSING_IT_DB_URL"));
        r.add("spring.flyway.clean-disabled", () -> "false");
        r.add("sensing.secret-key", () -> "it-secret-key");
        r.add("sensing.llm.provider", () -> "openai");
        r.add("sensing.llm.base-url", () -> LLM.baseUrl() + "/v1");
        r.add("sensing.llm.model", () -> WORKER_MODEL);       // 워커 요약·레시피가 쓰는 모델
        r.add("sensing.llm.chat-model", () -> CHAT_MODEL);    // 챗봇만 다른 무료 모델 (SENSING_CHAT_LLM_MODEL)
        r.add("sensing.llm.api-key", () -> "test-key");
        r.add("sensing.llm.timeout-seconds", () -> "10");
        r.add("sensing.llm.retry-delay-ms", () -> "0");
        r.add("sensing.llm.enrich-daily-calls", () -> "250");
    }

    @AfterAll
    static void stopLlm() {
        LLM.close();
    }

    @Autowired TestRestTemplate http;
    @Autowired JdbcClient jdbc;

    @BeforeEach
    void clean() {
        LLM.reset();
        jdbc.sql("TRUNCATE chat_feedback, chat_citation, chat_message, chat_session RESTART IDENTITY CASCADE").update();
        jdbc.sql("TRUNCATE llm_usage, llm_cooldown").update();
    }

    @AfterEach
    void noLeftovers() {
        assertThat(LLM.failures()).isEmpty();
        jdbc.sql("TRUNCATE llm_usage, llm_cooldown").update();   // 다른 IT의 챗봇을 쉬게 하지 않는다
    }

    // ------------------------------------------------------------------ 도우미

    private ResponseEntity<JsonNode> call(String user, HttpMethod method, String path, Object body) {
        HttpHeaders h = new HttpHeaders();
        h.set("X-User", user);
        h.setContentType(MediaType.APPLICATION_JSON);
        return http.exchange(path, method, new HttpEntity<>(body, h), JsonNode.class);
    }

    private long session() {
        ResponseEntity<JsonNode> res = call("viewer", HttpMethod.POST, "/api/v1/chat/sessions", Map.of());
        assertThat(res.getStatusCode()).isEqualTo(HttpStatus.CREATED);
        return res.getBody().get("id").asLong();
    }

    private JsonNode ask(long session, String content) {
        ResponseEntity<JsonNode> res = call("viewer", HttpMethod.POST, "/api/v1/chat/sessions/" + session + "/messages",
                Map.of("content", content));
        assertThat(res.getStatusCode()).as(String.valueOf(res.getBody())).isEqualTo(HttpStatus.OK);
        return res.getBody().get("assistant");
    }

    private Map<String, Object> usage(String model, String purpose) {
        return jdbc.sql("""
                SELECT calls, rate_limited, errors, input_tokens, output_tokens FROM llm_usage
                WHERE quota_day = CAST(now() AT TIME ZONE ? AS date) AND model = ? AND purpose = ?
                """).params(TZ, model, purpose).query().singleRow();
    }

    private Map<String, Object> cooldown(String model) {
        return jdbc.sql("SELECT reason, until_at, quota_id, retry_seconds, purpose FROM llm_cooldown WHERE model = ?")
                .param(model).query().singleRow();
    }

    private OffsetDateTime sql(String expression) {
        return jdbc.sql("SELECT " + expression).query(OffsetDateTime.class).single();
    }

    private static OffsetDateTime at(Object value) {
        return value instanceof OffsetDateTime o ? o : ((java.sql.Timestamp) value).toInstant().atOffset(java.time.ZoneOffset.UTC);
    }

    private static String kst(OffsetDateTime t) {
        return t.atZoneSameInstant(KST).format(DateTimeFormatter.ofPattern("HH:mm"));
    }

    // ------------------------------------------------------------------

    @Test
    void chatCallsAreRecordedUnderTheChatModel() {
        LLM.then(body -> {
            assertThat(body.path("model").asText()).isEqualTo(CHAT_MODEL);
            return Reply.ok(MockLlmServer.openAiText("안녕하세요."));
        });
        JsonNode answer = ask(session(), "안녕");
        assertThat(answer.get("error").asBoolean()).isFalse();
        Map<String, Object> row = usage(CHAT_MODEL, "chat");
        assertThat(((Number) row.get("calls")).intValue()).isEqualTo(1);
        assertThat(((Number) row.get("input_tokens")).longValue()).isEqualTo(100);
        assertThat(((Number) row.get("output_tokens")).longValue()).isEqualTo(20);
        assertThat(((Number) row.get("rate_limited")).intValue()).isZero();
    }

    @Test
    void dailyQuota429PausesChatUntilResetWithoutCallingTheLlmMeanwhile() {
        long s = session();
        LLM.then(Reply.status(429, GEMINI_DAILY_429_WITH_RETRY));
        JsonNode first = ask(s, "주요 이슈 알려 줘");
        assertThat(first.get("error").asBoolean()).isTrue();
        assertThat(first.get("content").asText()).contains("하루 사용 한도").contains("한국 시간");

        Map<String, Object> c = cooldown(CHAT_MODEL);
        assertThat(c.get("reason")).isEqualTo("DAILY");
        assertThat(c.get("quota_id")).isEqualTo("GenerateRequestsPerDayPerProjectPerModel-FreeTier");
        assertThat(((Number) c.get("retry_seconds")).intValue()).isEqualTo(60554);
        assertThat(c.get("purpose")).isEqualTo("chat");
        // 쿼터 기준(태평양 시간) 다음 자정과 retryDelay 중 늦은 쪽
        OffsetDateTime expected = sql("GREATEST(now() + interval '60554 seconds', "
                + "(CAST(CAST(now() AT TIME ZONE 'America/Los_Angeles' AS date) + 1 AS timestamp) AT TIME ZONE 'America/Los_Angeles'))");
        OffsetDateTime until = at(c.get("until_at"));
        assertThat(until).isCloseTo(expected, within(10, ChronoUnit.SECONDS));
        assertThat(first.get("content").asText()).contains(kst(until));
        // 거절된 요청은 한도에 들어가지 않는다
        assertThat(((Number) usage(CHAT_MODEL, "chat").get("calls")).intValue()).isZero();
        assertThat(((Number) usage(CHAT_MODEL, "chat").get("rate_limited")).intValue()).isEqualTo(1);

        // 쉬는 동안: LLM을 부르지 않고 바로 안내한다 (가짜 서버에 남은 답이 없으므로 불렀다면 failures에 남는다)
        JsonNode again = ask(s, "그럼 어제 이슈는?");
        assertThat(again.get("error").asBoolean()).isTrue();
        assertThat(again.get("content").asText()).contains("하루 사용 한도").contains("한국 시간 ").contains(kst(until));
        assertThat(LLM.requests()).hasSize(1);
        assertThat(jdbc.sql("SELECT meta->>'error' FROM chat_message WHERE session_id = ? AND role = 'ASSISTANT' ORDER BY seq DESC LIMIT 1")
                .param(s).query(String.class).single()).isEqualTo("RATE_LIMITED");

        // 풀리면 다시 부른다. 쉬는 동안의 턴은 대화 기록에서 빠진다 (오류 답 처리 그대로)
        jdbc.sql("UPDATE llm_cooldown SET until_at = now() - interval '1 second'").update();
        LLM.then(body -> {
            assertThat(body.path("messages").toString()).doesNotContain("주요 이슈 알려 줘").doesNotContain("어제 이슈");
            return Reply.ok(MockLlmServer.openAiText("다시 됩니다."));
        });
        assertThat(ask(s, "다시 해 볼게").get("error").asBoolean()).isFalse();
        assertThat(LLM.requests()).hasSize(2);
    }

    @Test
    void minuteQuota429PausesOnlyBriefly() {
        long s = session();
        LLM.then(Reply.status(429, MockLlmServer.GEMINI_MINUTE_429));
        assertThat(ask(s, "질문").get("content").asText()).contains("약 42초 뒤");
        Map<String, Object> c = cooldown(CHAT_MODEL);
        assertThat(c.get("reason")).isEqualTo("MINUTE");
        long left = java.time.Duration.between(OffsetDateTime.now(), at(c.get("until_at"))).getSeconds();
        assertThat(left).isBetween(25L, 42L);

        JsonNode waiting = ask(s, "다시 질문");
        assertThat(waiting.get("content").asText()).containsPattern("약 \\d{1,2}초 뒤에 다시 시도해 주세요");
        assertThat(LLM.requests()).hasSize(1);
    }

    @Test
    void workerQuotaHitOnTheSameModelPausesChatButOtherModelsDoNot() {
        long s = session();
        // 워커 요약이 자기 모델(SENSING_LLM_MODEL)로 하루 한도에 걸렸다: 챗봇은 다른 모델이라 계속 답한다
        jdbc.sql("INSERT INTO llm_cooldown (model, until_at, reason, quota_id, purpose) VALUES (?, now() + interval '5 hours', 'DAILY', 'q', 'enrich')")
                .param(WORKER_MODEL).update();
        LLM.then(Reply.ok(MockLlmServer.openAiText("요약 모델과 한도를 따로 씁니다.")));
        assertThat(ask(s, "첫 질문").get("error").asBoolean()).isFalse();
        assertThat(LLM.requests()).hasSize(1);

        // 같은 모델이면 (챗봇 모델을 워커도 쓰다 429를 받았다) 챗봇도 부르지 않는다
        jdbc.sql("INSERT INTO llm_cooldown (model, until_at, reason, purpose) VALUES (?, now() + interval '2 hours', 'DAILY', 'enrich')")
                .param(CHAT_MODEL).update();
        JsonNode paused = ask(s, "두 번째 질문");
        assertThat(paused.get("error").asBoolean()).isTrue();
        assertThat(paused.get("content").asText()).contains("한국 시간");
        assertThat(LLM.requests()).hasSize(1);
    }

    @Test
    void opsOverviewShowsTodaysLlmUsageCapAndPauses() {
        JsonNode empty = call("admin", HttpMethod.GET, "/api/v1/ops/overview", null).getBody().get("llm");
        assertThat(empty.get("quotaTimeZone").asText()).isEqualTo(TZ);
        assertThat(empty.get("resetAt").isNull()).isFalse();
        assertThat(empty.get("enrichDailyCap").asInt()).isEqualTo(250);   // 오늘 워커 기록이 없으면 백엔드 설정
        assertThat(empty.get("usage")).isEmpty();
        assertThat(empty.get("cooldowns")).isEmpty();

        jdbc.sql("""
                INSERT INTO llm_usage (quota_day, model, purpose, calls, rate_limited, input_tokens, output_tokens, daily_cap)
                VALUES (CAST(now() AT TIME ZONE 'America/Los_Angeles' AS date), 'summary-model', 'enrich', 300, 1, 900000, 40000, 300),
                       (CAST(now() AT TIME ZONE 'America/Los_Angeles' AS date), 'summary-model', 'recipe', 4, 0, 8000, 900, NULL),
                       (CAST(now() AT TIME ZONE 'America/Los_Angeles' AS date), 'chat-model', 'chat', 12, 0, 30000, 2000, NULL),
                       (CAST(now() AT TIME ZONE 'America/Los_Angeles' AS date) - 1, 'summary-model', 'enrich', 300, 0, 0, 0, 300)
                """).update();
        jdbc.sql("""
                INSERT INTO llm_cooldown (model, until_at, reason, quota_id, retry_seconds, purpose)
                VALUES ('summary-model', now() + interval '3 hours', 'DAILY', 'GenerateRequestsPerDayPerProjectPerModel-FreeTier', 10800, 'enrich'),
                       ('old-model', now() - interval '1 minute', 'MINUTE', NULL, 40, 'chat')
                """).update();
        JsonNode llm = call("admin", HttpMethod.GET, "/api/v1/ops/overview", null).getBody().get("llm");
        assertThat(llm.get("enrichCalls").asLong()).isEqualTo(300);       // 어제 기록은 세지 않는다
        assertThat(llm.get("enrichDailyCap").asInt()).isEqualTo(300);     // 워커가 장부에 남긴 상한이 우선
        assertThat(llm.get("usage")).hasSize(3);   // model, purpose 순: chat-model/chat, summary-model/enrich, summary-model/recipe
        JsonNode enrich = llm.get("usage").get(1);
        assertThat(enrich.get("purpose").asText()).isEqualTo("enrich");
        assertThat(enrich.get("calls").asLong()).isEqualTo(300);
        assertThat(enrich.get("rateLimited").asLong()).isEqualTo(1);
        assertThat(enrich.get("dailyCap").asInt()).isEqualTo(300);
        assertThat(llm.get("usage").get(2).get("dailyCap").isNull()).isTrue();
        assertThat(llm.get("cooldowns")).hasSize(1);                     // 이미 풀린 것은 빼고
        JsonNode c = llm.get("cooldowns").get(0);
        assertThat(c.get("model").asText()).isEqualTo("summary-model");
        assertThat(c.get("reason").asText()).isEqualTo("DAILY");
        assertThat(c.get("secondsLeft").asLong()).isBetween(10_700L, 10_800L);
    }
}
