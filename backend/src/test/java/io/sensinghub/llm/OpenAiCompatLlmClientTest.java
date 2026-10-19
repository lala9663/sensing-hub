package io.sensinghub.llm;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import java.util.List;
import java.util.Map;

import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;

import io.sensinghub.llm.LlmClient.Message;
import io.sensinghub.llm.LlmClient.Response;
import io.sensinghub.llm.LlmClient.Tool;
import io.sensinghub.llm.LlmClient.ToolCall;
import io.sensinghub.llm.LlmException.Kind;
import io.sensinghub.llm.MockLlmServer.Reply;

/** OpenAI 호환 클라이언트: 요청 형식(tools, 도구 결과, Gemini thought_signature 되돌려 주기)과 HTTP 오류 분류. 가짜 서버만 부른다. */
class OpenAiCompatLlmClientTest {

    static MockLlmServer server;
    static final ObjectMapper JSON = new ObjectMapper();
    static final List<Tool> TOOLS = List.of(new Tool("search_documents", "문서 검색",
            Map.of("type", "object", "properties", Map.of("query", Map.of("type", "string")))));

    @BeforeAll
    static void start() {
        server = MockLlmServer.start();
    }

    @AfterAll
    static void stop() {
        server.close();
    }

    @BeforeEach
    void reset() {
        server.reset();
    }

    private OpenAiCompatLlmClient client(String key, int timeoutSeconds) {
        return new OpenAiCompatLlmClient(server.baseUrl() + "/v1/", "gemini-test", key, timeoutSeconds, 0, 0, JSON);
    }

    @Test
    void sendsToolsAndParsesToolCallKeepingThoughtSignature() {
        server.then(Reply.ok(MockLlmServer.openAiToolCall("call-a", "search_documents", "{\"query\":\"반도체\"}", "sig-123")));
        Response r = client("test-key", 5).chat("시스템", List.of(Message.user("반도체 기사")), TOOLS);

        assertThat(r.toolCalls()).hasSize(1);
        ToolCall call = r.toolCalls().get(0);
        assertThat(call.id()).isEqualTo("call-a");
        assertThat(call.name()).isEqualTo("search_documents");
        assertThat(call.arguments()).containsEntry("query", "반도체");
        assertThat(call.providerData()).containsKey("extra_content");
        assertThat(r.usage()).containsEntry("inputTokens", 100);

        MockLlmServer.Recorded req = server.requests().get(0);
        assertThat(req.path()).isEqualTo("/v1/chat/completions");
        assertThat(req.header("Authorization")).isEqualTo("Bearer test-key");
        JsonNode body = req.body();
        assertThat(body.path("model").asText()).isEqualTo("gemini-test");
        assertThat(body.has("temperature")).isFalse();
        assertThat(body.has("max_tokens")).isFalse();
        assertThat(body.path("tool_choice").asText()).isEqualTo("auto");
        assertThat(body.path("tools").path(0).path("function").path("name").asText()).isEqualTo("search_documents");
        assertThat(body.path("messages").path(0).path("role").asText()).isEqualTo("system");
        assertThat(body.path("messages").path(1).path("content").asText()).isEqualTo("반도체 기사");

        // 다음 요청: 도구 호출(서명 포함)과 결과를 돌려준다
        server.then(Reply.ok(MockLlmServer.openAiText("찾았습니다 [1]")));
        Response answer = client("test-key", 5).chat("시스템", List.of(Message.user("반도체 기사"),
                Message.assistant(null, r.toolCalls()), Message.tool("call-a", "search_documents", "{\"count\":1}")),
                TOOLS, false);
        assertThat(answer.text()).isEqualTo("찾았습니다 [1]");
        assertThat(answer.toolCalls()).isEmpty();
        JsonNode second = server.requests().get(1).body();
        assertThat(second.path("tool_choice").asText()).isEqualTo("none");
        JsonNode assistant = second.path("messages").path(2);
        assertThat(assistant.path("role").asText()).isEqualTo("assistant");
        assertThat(assistant.path("content").isNull()).isTrue();
        JsonNode echoed = assistant.path("tool_calls").path(0);
        assertThat(echoed.path("id").asText()).isEqualTo("call-a");
        assertThat(echoed.path("function").path("arguments").asText()).contains("반도체");
        assertThat(echoed.path("extra_content").path("google").path("thought_signature").asText()).isEqualTo("sig-123");
        JsonNode tool = second.path("messages").path(3);
        assertThat(tool.path("role").asText()).isEqualTo("tool");
        assertThat(tool.path("tool_call_id").asText()).isEqualTo("call-a");
        assertThat(tool.path("content").asText()).isEqualTo("{\"count\":1}");
    }

    @Test
    void omitsAuthorizationWithoutKeyAndToleratesMissingIdsAndBadArguments() {
        server.then(Reply.ok("""
                {"choices":[{"message":{"role":"assistant","content":null,"tool_calls":[
                  {"type":"function","function":{"name":"search_documents","arguments":"not json"}}]},"finish_reason":"tool_calls"}]}
                """));
        Response r = client("", 5).chat("", List.of(Message.user("안녕")), TOOLS);
        assertThat(server.requests().get(0).header("Authorization")).isNull();
        assertThat(r.toolCalls().get(0).id()).isEqualTo("call_1");
        assertThat(r.toolCalls().get(0).arguments()).isEmpty();
        assertThat(r.text()).isEmpty();
    }

    @Test
    void dailyQuota429IsExplainedWithoutRetry() {
        server.then(Reply.status(429, MockLlmServer.GEMINI_DAILY_429));
        assertThatThrownBy(() -> client("k", 5).chat("", List.of(Message.user("q")), TOOLS))
                .isInstanceOfSatisfying(LlmException.class, e -> {
                    assertThat(e.kind()).isEqualTo(Kind.RATE_LIMITED);
                    assertThat(e.httpStatus()).isEqualTo(429);
                    assertThat(e.getMessage()).contains("하루 사용 한도");
                });
        assertThat(server.requests()).hasSize(1);
    }

    @Test
    void minuteQuota429SaysWhenToRetry() {
        server.then(Reply.status(429, MockLlmServer.GEMINI_MINUTE_429));
        assertThatThrownBy(() -> client("k", 5).chat("", List.of(Message.user("q")), TOOLS))
                .isInstanceOfSatisfying(LlmException.class, e -> {
                    assertThat(e.kind()).isEqualTo(Kind.RATE_LIMITED);
                    assertThat(e.getMessage()).contains("약 42초 뒤");
                });
        server.reset();
        server.then(new Reply(429, "{\"error\":{\"message\":\"rate limit\"}}", Map.of("Retry-After", "7"), 0));
        assertThatThrownBy(() -> client("k", 5).chat("", List.of(Message.user("q")), TOOLS))
                .hasMessageContaining("약 7초 뒤");
    }

    @Test
    void serverErrorIsRetriedOnceThenExplained() {
        server.then(Reply.status(503, "{\"error\":\"overloaded\"}")).then(Reply.ok(MockLlmServer.openAiText("다시 됐어요")));
        assertThat(client("k", 5).chat("", List.of(Message.user("q")), TOOLS).text()).isEqualTo("다시 됐어요");
        assertThat(server.requests()).hasSize(2);

        server.reset();
        server.then(Reply.status(500, "{}")).then(Reply.status(502, "{}"));
        assertThatThrownBy(() -> client("k", 5).chat("", List.of(Message.user("q")), TOOLS))
                .isInstanceOfSatisfying(LlmException.class, e -> {
                    assertThat(e.kind()).isEqualTo(Kind.SERVER_ERROR);
                    assertThat(e.getMessage()).contains("HTTP 502").contains("잠시 후");
                });
    }

    @Test
    void timeoutAuthAndUnknownModelAreClassified() {
        server.then(new Reply(200, MockLlmServer.openAiText("늦은 답"), Map.of(), 2500));
        assertThatThrownBy(() -> client("k", 1).chat("", List.of(Message.user("q")), TOOLS))
                .isInstanceOfSatisfying(LlmException.class, e -> {
                    assertThat(e.kind()).isEqualTo(Kind.TIMEOUT);
                    assertThat(e.getMessage()).contains("1초 안에");
                });

        server.reset();
        server.then(Reply.status(401, "{\"error\":\"bad key\"}"));
        assertThatThrownBy(() -> client("wrong", 5).chat("", List.of(Message.user("q")), TOOLS))
                .isInstanceOfSatisfying(LlmException.class, e -> {
                    assertThat(e.kind()).isEqualTo(Kind.AUTH);
                    assertThat(e.getMessage()).doesNotContain("wrong");
                });

        server.reset();
        server.then(Reply.status(404, "{\"error\":\"model not found\"}"));
        assertThatThrownBy(() -> client("k", 5).chat("", List.of(Message.user("q")), TOOLS))
                .hasMessageContaining("SENSING_LLM_MODEL");

        server.reset();
        server.then(Reply.ok("<html>not json</html>"));
        assertThatThrownBy(() -> client("k", 5).chat("", List.of(Message.user("q")), TOOLS))
                .isInstanceOfSatisfying(LlmException.class, e -> assertThat(e.kind()).isEqualTo(Kind.BAD_RESPONSE));
    }

    @Test
    void connectionFailureIsNetworkError() {
        OpenAiCompatLlmClient closed = new OpenAiCompatLlmClient("http://127.0.0.1:1/v1", "m", "", 2, 0, 0, JSON);
        assertThatThrownBy(() -> closed.chat("", List.of(Message.user("q")), TOOLS))
                .isInstanceOfSatisfying(LlmException.class, e -> assertThat(e.kind()).isEqualTo(Kind.NETWORK));
    }

    @Test
    void configPicksProviderLikeTheWorker() {
        LlmConfig config = new LlmConfig();
        assertThat(config.llmClient(JSON, "none", "", "", "", "", "", "60", 0, 0).configured()).isFalse();
        assertThat(config.llmClient(JSON, "", "", "", "", "", "", "60", 0, 0).configured()).isFalse();
        LlmClient missingUrl = config.llmClient(JSON, "openai", "m", "", "", "", "", "60", 0, 0);
        assertThat(missingUrl.configured()).isFalse();
        assertThatThrownBy(() -> missingUrl.chat("", List.of(), List.of()))
                .isInstanceOfSatisfying(LlmException.class, e -> {
                    assertThat(e.kind()).isEqualTo(Kind.NOT_CONFIGURED);
                    assertThat(e.getMessage()).contains("LLM이 설정되지 않았습니다").contains("SENSING_LLM_BASE_URL");
                });
        assertThat(config.llmClient(JSON, "OpenAI-Compat", "gemini", "https://x/v1", "k", "", "", "120.5", 0, 0).describe())
                .isEqualTo("openai gemini");
        assertThat(config.llmClient(JSON, "anthropic", "", "", "", "", "", "60", 0, 0).configured()).isFalse();
        assertThat(config.llmClient(JSON, "anthropic", "", "", "", "sk-ant-x", "", "60", 0, 0).describe())
                .isEqualTo("anthropic claude-opus-5-5");
        assertThat(config.llmClient(JSON, "mystery", "", "", "", "", "", "60", 0, 0).configured()).isFalse();
        assertThat(LlmConfig.seconds("90.5")).isEqualTo(91);
        assertThat(LlmConfig.seconds("x")).isEqualTo(60);
    }
}
