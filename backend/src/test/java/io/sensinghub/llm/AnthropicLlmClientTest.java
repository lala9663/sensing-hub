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

/** Claude Messages API 클라이언트: tool_use·tool_result 형식, 헤더, 529 재시도, 거절. 가짜 서버만 부른다. */
class AnthropicLlmClientTest {

    static MockLlmServer server;
    static final ObjectMapper JSON = new ObjectMapper();
    static final List<Tool> TOOLS = List.of(new Tool("list_issues", "이슈 목록",
            Map.of("type", "object", "properties", Map.of())));

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

    private AnthropicLlmClient client() {
        return new AnthropicLlmClient(server.baseUrl(), "claude-test", "sk-ant-test", 5, 0, 0, JSON);
    }

    @Test
    void sendsMessagesApiRequestAndParsesToolUse() {
        server.then(Reply.ok("""
                {"content":[{"type":"text","text":"이슈를 볼게요."},
                            {"type":"tool_use","id":"toolu_1","name":"list_issues","input":{"days":3}}],
                 "stop_reason":"tool_use","usage":{"input_tokens":50,"output_tokens":12}}
                """));
        Response r = client().chat("시스템 지시", List.of(Message.user("주요 이슈?")), TOOLS);
        assertThat(r.text()).isEqualTo("이슈를 볼게요.");
        assertThat(r.toolCalls()).extracting(ToolCall::id, ToolCall::name).containsExactly(
                org.assertj.core.groups.Tuple.tuple("toolu_1", "list_issues"));
        assertThat(r.toolCalls().get(0).arguments()).containsEntry("days", 3);
        assertThat(r.usage()).containsEntry("outputTokens", 12);

        MockLlmServer.Recorded req = server.requests().get(0);
        assertThat(req.path()).isEqualTo("/v1/messages");
        assertThat(req.header("x-api-key")).isEqualTo("sk-ant-test");
        assertThat(req.header("anthropic-version")).isEqualTo(AnthropicLlmClient.VERSION);
        JsonNode body = req.body();
        assertThat(body.path("system").asText()).isEqualTo("시스템 지시");
        assertThat(body.path("max_tokens").asInt()).isEqualTo(4096);
        assertThat(body.has("temperature")).isFalse();
        assertThat(body.path("tools").path(0).path("input_schema").path("type").asText()).isEqualTo("object");
        assertThat(body.path("tool_choice").path("type").asText()).isEqualTo("auto");
    }

    @Test
    void toolResultsAndFollowingUserTextShareOneUserMessage() {
        server.then(Reply.ok("{\"content\":[{\"type\":\"text\",\"text\":\"정리했습니다 [1]\"}],\"stop_reason\":\"end_turn\"}"));
        ToolCall a = new ToolCall("toolu_a", "list_issues", Map.of());
        ToolCall b = new ToolCall("toolu_b", "list_issues", Map.of("days", 1));
        Response r = client().chat("", List.of(
                Message.user("이슈?"),
                Message.assistant("", List.of(a, b)),
                Message.tool("toolu_a", "list_issues", "{\"count\":0}"),
                Message.tool("toolu_b", "list_issues", "{\"count\":2}"),
                Message.user("고마워, 하나 더")), TOOLS, false);
        assertThat(r.text()).isEqualTo("정리했습니다 [1]");

        JsonNode messages = server.requests().get(0).body().path("messages");
        assertThat(messages).hasSize(3);
        assertThat(messages.path(1).path("role").asText()).isEqualTo("assistant");
        assertThat(messages.path(1).path("content")).hasSize(2);   // 빈 글은 빼고 tool_use 두 개
        JsonNode user = messages.path(2);
        assertThat(user.path("role").asText()).isEqualTo("user");
        assertThat(user.path("content").path(0).path("type").asText()).isEqualTo("tool_result");
        assertThat(user.path("content").path(0).path("tool_use_id").asText()).isEqualTo("toolu_a");
        assertThat(user.path("content").path(1).path("tool_use_id").asText()).isEqualTo("toolu_b");
        assertThat(user.path("content").path(2).path("type").asText()).isEqualTo("text");
        assertThat(server.requests().get(0).body().path("tool_choice").path("type").asText()).isEqualTo("none");
    }

    @Test
    void overloadedIsRetriedOnceAndRefusalIsExplained() {
        server.then(Reply.status(529, "{\"type\":\"error\",\"error\":{\"type\":\"overloaded_error\"}}"))
                .then(Reply.ok("{\"content\":[{\"type\":\"text\",\"text\":\"됐어요\"}],\"stop_reason\":\"end_turn\"}"));
        assertThat(client().chat("", List.of(Message.user("q")), TOOLS).text()).isEqualTo("됐어요");
        assertThat(server.requests()).hasSize(2);

        server.reset();
        server.then(Reply.ok("{\"content\":[],\"stop_reason\":\"refusal\"}"));
        assertThatThrownBy(() -> client().chat("", List.of(Message.user("q")), TOOLS))
                .isInstanceOfSatisfying(LlmException.class, e -> assertThat(e.kind()).isEqualTo(Kind.BAD_RESPONSE));

        server.reset();
        server.then(Reply.status(429, "{\"type\":\"error\",\"error\":{\"type\":\"rate_limit_error\"}}"));
        assertThatThrownBy(() -> client().chat("", List.of(Message.user("q")), TOOLS))
                .isInstanceOfSatisfying(LlmException.class, e -> {
                    assertThat(e.kind()).isEqualTo(Kind.RATE_LIMITED);
                    assertThat(e.getMessage()).contains("1분쯤 뒤");
                });
        assertThat(server.requests()).hasSize(1);
    }
}
