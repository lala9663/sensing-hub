package io.sensinghub.llm;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import java.util.ArrayList;
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
import io.sensinghub.llm.LlmClient.StreamListener;
import io.sensinghub.llm.LlmClient.Tool;
import io.sensinghub.llm.LlmClient.ToolCall;
import io.sensinghub.llm.LlmException.Kind;
import io.sensinghub.llm.MockLlmServer.Reply;

/**
 * 스트리밍(SSE): OpenAI 호환(chat.completion.chunk — 글 조각, 도구 호출 조각 잇기, Gemini thought_signature 보관, 사용량)과
 * Anthropic(content_block_* 이벤트), 그리고 {@link LlmClient#chatStreaming}의 한 번에 받기 전환(끊긴 스트림·5xx·400은 바꾸고,
 * 429·시간 초과는 바꾸지 않는다, 400 뒤에는 스트리밍을 잠시 끈다). 가짜 서버만 부른다.
 */
class LlmStreamingTest {

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

    private OpenAiCompatLlmClient openAi(int timeoutSeconds) {
        return new OpenAiCompatLlmClient(server.baseUrl() + "/v1", "gemini-test", "test-key", timeoutSeconds, 0, 0, JSON);
    }

    /** 받은 조각과 reset 수. */
    private static final class Collected implements StreamListener {
        final List<String> pieces = new ArrayList<>();
        int resets;

        @Override
        public void text(String delta) {
            pieces.add(delta);
        }

        @Override
        public void reset() {
            resets++;
        }
    }

    // ------------------------------------------------------------------ OpenAI 호환

    @Test
    void openAiTextArrivesInPiecesWithUsage() {
        server.then(Reply.sse(MockLlmServer.openAiStreamText("반도체 ", "소식", "입니다 [1]")));
        Collected got = new Collected();
        Response r = openAi(5).stream("시스템", List.of(Message.user("반도체")), TOOLS, true, got);

        assertThat(got.pieces).containsExactly("반도체 ", "소식", "입니다 [1]");
        assertThat(r.text()).isEqualTo("반도체 소식입니다 [1]");
        assertThat(r.toolCalls()).isEmpty();
        assertThat(r.finishReason()).isEqualTo("stop");
        assertThat(r.usage()).containsEntry("inputTokens", 120).containsEntry("outputTokens", 30);
        MockLlmServer.Recorded req = server.requests().get(0);
        assertThat(req.path()).isEqualTo("/v1/chat/completions");
        assertThat(req.header("Accept")).isEqualTo("text/event-stream");
        assertThat(req.header("Authorization")).isEqualTo("Bearer test-key");
        assertThat(req.body().path("stream").asBoolean()).isTrue();
        assertThat(req.body().path("stream_options").path("include_usage").asBoolean()).isTrue();
        assertThat(req.body().path("tool_choice").asText()).isEqualTo("auto");
    }

    /** 도구 호출 조각을 잇고 Gemini 3의 thought_signature를 보관해 다음 요청에 돌려준다 (index 없이 오는 경우도). */
    @Test
    void openAiToolCallFragmentsAreJoinedAndTheThoughtSignatureIsKept() {
        for (boolean withIndex : List.of(true, false)) {
            server.reset();
            server.then(Reply.sse(MockLlmServer.openAiStreamToolCall("call-s", "search_documents", "sig-9", withIndex,
                    "{\"que", "ry\":\"반도", "체\"}")));
            Collected got = new Collected();
            Response r = openAi(5).stream("시스템", List.of(Message.user("반도체 기사")), TOOLS, true, got);

            assertThat(got.pieces).as("index " + withIndex).isEmpty();
            assertThat(r.finishReason()).isEqualTo("tool_calls");
            assertThat(r.toolCalls()).hasSize(1);
            ToolCall call = r.toolCalls().get(0);
            assertThat(call.id()).isEqualTo("call-s");
            assertThat(call.name()).isEqualTo("search_documents");
            assertThat(call.arguments()).containsEntry("query", "반도체");
            assertThat(call.providerData()).containsOnlyKeys("extra_content");

            server.then(Reply.sse(MockLlmServer.openAiStreamText("찾았습니다 [1]")));
            openAi(5).stream("시스템", List.of(Message.user("반도체 기사"), Message.assistant(null, r.toolCalls()),
                    Message.tool("call-s", "search_documents", "{\"count\":1}")), TOOLS, false, new Collected());
            JsonNode echoed = server.requests().get(1).body().path("messages").path(2).path("tool_calls").path(0);
            assertThat(echoed.path("extra_content").path("google").path("thought_signature").asText()).isEqualTo("sig-9");
            assertThat(echoed.path("function").path("arguments").asText()).contains("반도체");
        }
    }

    /** stream을 무시하고 JSON으로 답하는 서버: 한 번에 받은 답으로 읽는다 (다시 부르지 않는다). */
    @Test
    void jsonReplyToAStreamRequestIsReadAsOneShot() {
        server.then(Reply.ok(MockLlmServer.openAiText("한 번에 온 답")));
        OpenAiCompatLlmClient client = openAi(5);
        Collected got = new Collected();
        assertThat(client.chatStreaming("", List.of(Message.user("q")), TOOLS, true, got).text()).isEqualTo("한 번에 온 답");
        assertThat(got.pieces).isEmpty();
        assertThat(server.requests()).hasSize(1);
        assertThat(client.streams()).isTrue();
    }

    /** 끝([DONE]·finish_reason) 없이 끊긴 스트림: 받은 조각을 버리고(reset) 같은 요청을 한 번에 받기로 다시 보낸다. */
    @Test
    void brokenStreamFallsBackToOneShot() {
        List<String> broken = new ArrayList<>(MockLlmServer.openAiStreamText("앞부분", "까지만"));
        broken.subList(2, broken.size()).clear();   // 글 두 조각 뒤 끝 없이 닫힌다
        server.then(Reply.sse(broken)).then(Reply.ok(MockLlmServer.openAiText("다시 받은 답")));
        OpenAiCompatLlmClient client = openAi(5);
        Collected got = new Collected();
        Response r = client.chatStreaming("", List.of(Message.user("q")), TOOLS, true, got);

        assertThat(r.text()).isEqualTo("다시 받은 답");
        assertThat(got.pieces).containsExactly("앞부분", "까지만");
        assertThat(got.resets).isEqualTo(1);
        assertThat(server.requests()).hasSize(2);
        assertThat(server.requests().get(1).body().has("stream")).isFalse();
        assertThat(client.streams()).isTrue();   // 연결 문제는 일시적이라 스트리밍을 끄지 않는다
    }

    /** 5xx는 스트림을 다시 보내지 않고 한 번에 받기로 바꾼다. 스트리밍을 거절(400)하면 바꾼 뒤 한동안 스트리밍을 쓰지 않는다. */
    @Test
    void serverErrorAndRejectedStreamFallBackToOneShot() {
        server.then(Reply.status(503, "{\"error\":\"overloaded\"}")).then(Reply.ok(MockLlmServer.openAiText("됐어요")));
        OpenAiCompatLlmClient client = openAi(5);
        assertThat(client.chatStreaming("", List.of(Message.user("q")), TOOLS, true, new Collected()).text()).isEqualTo("됐어요");
        assertThat(server.requests()).hasSize(2);
        assertThat(client.streams()).isTrue();

        server.reset();
        server.then(Reply.status(400, "{\"error\":{\"message\":\"stream is not supported\"}}"))
                .then(Reply.ok(MockLlmServer.openAiText("한 번에")))
                .then(Reply.ok(MockLlmServer.openAiText("다음 질문도 한 번에")));
        assertThat(client.chatStreaming("", List.of(Message.user("q")), TOOLS, true, new Collected()).text()).isEqualTo("한 번에");
        assertThat(client.streams()).isFalse();
        assertThat(client.chatStreaming("", List.of(Message.user("q2")), TOOLS, true, new Collected()).text())
                .isEqualTo("다음 질문도 한 번에");
        assertThat(server.requests()).hasSize(3);
        assertThat(server.requests().get(0).body().path("stream").asBoolean()).isTrue();
        assertThat(server.requests().get(2).body().has("stream")).isFalse();
    }

    /** 429(시작 전이든 스트림 중간이든)·시간 초과는 한 번에 받기로 바꾸지 않는다 (한도만 더 쓴다). */
    @Test
    void rateLimitAndTimeoutAreNotRetried() {
        server.then(Reply.status(429, MockLlmServer.GEMINI_DAILY_429));
        Collected got = new Collected();
        assertThatThrownBy(() -> openAi(5).chatStreaming("", List.of(Message.user("q")), TOOLS, true, got))
                .isInstanceOfSatisfying(LlmException.class, e -> {
                    assertThat(e.kind()).isEqualTo(Kind.RATE_LIMITED);
                    assertThat(e.rateLimit().daily()).isTrue();
                });
        assertThat(server.requests()).hasSize(1);
        assertThat(got.resets).isZero();

        server.reset();
        List<String> events = new ArrayList<>(MockLlmServer.openAiStreamText("시작"));
        events.add(1, "data: {\"error\":{\"code\":429,\"message\":\"Resource has been exhausted. Please retry in 30s.\","
                + "\"status\":\"RESOURCE_EXHAUSTED\"}}");
        server.then(Reply.sse(events));
        assertThatThrownBy(() -> openAi(5).chatStreaming("", List.of(Message.user("q")), TOOLS, true, new Collected()))
                .isInstanceOfSatisfying(LlmException.class, e -> {
                    assertThat(e.kind()).isEqualTo(Kind.RATE_LIMITED);
                    assertThat(e.getMessage()).contains("약 30초 뒤");
                });
        assertThat(server.requests()).hasSize(1);

        server.reset();
        server.then(Reply.sse(1500, MockLlmServer.openAiStreamText("느린", "답")));
        long started = System.nanoTime();
        assertThatThrownBy(() -> openAi(1).chatStreaming("", List.of(Message.user("q")), TOOLS, true, new Collected()))
                .isInstanceOfSatisfying(LlmException.class, e -> {
                    assertThat(e.kind()).isEqualTo(Kind.TIMEOUT);
                    assertThat(e.getMessage()).contains("1초 안에");
                });
        assertThat((System.nanoTime() - started) / 1_000_000).isLessThan(1450);   // 남은 조각을 기다리지 않고 끊었다
        assertThat(server.requests()).hasSize(1);
    }

    // ------------------------------------------------------------------ Anthropic

    private AnthropicLlmClient anthropic() {
        return new AnthropicLlmClient(server.baseUrl(), "claude-test", "sk-ant-test", 5, 0, 0, JSON);
    }

    private static String event(String name, String data) {
        return "event: " + name + "\ndata: " + data;
    }

    @Test
    void anthropicTextAndToolUseArriveAsContentBlocks() {
        server.then(Reply.sse(List.of(
                event("message_start", "{\"type\":\"message_start\",\"message\":{\"usage\":{\"input_tokens\":50,\"output_tokens\":1}}}"),
                event("content_block_start", "{\"type\":\"content_block_start\",\"index\":0,\"content_block\":{\"type\":\"text\",\"text\":\"\"}}"),
                event("ping", "{\"type\":\"ping\"}"),
                event("content_block_delta", "{\"type\":\"content_block_delta\",\"index\":0,\"delta\":{\"type\":\"text_delta\",\"text\":\"이슈를 \"}}"),
                event("content_block_delta", "{\"type\":\"content_block_delta\",\"index\":0,\"delta\":{\"type\":\"text_delta\",\"text\":\"볼게요.\"}}"),
                event("content_block_stop", "{\"type\":\"content_block_stop\",\"index\":0}"),
                event("content_block_start", "{\"type\":\"content_block_start\",\"index\":1,\"content_block\":"
                        + "{\"type\":\"tool_use\",\"id\":\"toolu_1\",\"name\":\"list_issues\",\"input\":{}}}"),
                event("content_block_delta", "{\"type\":\"content_block_delta\",\"index\":1,\"delta\":{\"type\":\"input_json_delta\",\"partial_json\":\"{\\\"da\"}}"),
                event("content_block_delta", "{\"type\":\"content_block_delta\",\"index\":1,\"delta\":{\"type\":\"input_json_delta\",\"partial_json\":\"ys\\\": 3}\"}}"),
                event("content_block_stop", "{\"type\":\"content_block_stop\",\"index\":1}"),
                event("message_delta", "{\"type\":\"message_delta\",\"delta\":{\"stop_reason\":\"tool_use\"},\"usage\":{\"output_tokens\":12}}"),
                event("message_stop", "{\"type\":\"message_stop\"}"))));
        Collected got = new Collected();
        Response r = anthropic().stream("시스템", List.of(Message.user("주요 이슈?")), TOOLS, true, got);

        assertThat(got.pieces).containsExactly("이슈를 ", "볼게요.");
        assertThat(r.text()).isEqualTo("이슈를 볼게요.");
        assertThat(r.toolCalls()).extracting(ToolCall::id, ToolCall::name).containsExactly(
                org.assertj.core.groups.Tuple.tuple("toolu_1", "list_issues"));
        assertThat(r.toolCalls().get(0).arguments()).containsEntry("days", 3);
        assertThat(r.finishReason()).isEqualTo("tool_use");
        assertThat(r.usage()).containsEntry("inputTokens", 50).containsEntry("outputTokens", 12);
        MockLlmServer.Recorded req = server.requests().get(0);
        assertThat(req.path()).isEqualTo("/v1/messages");
        assertThat(req.header("x-api-key")).isEqualTo("sk-ant-test");
        assertThat(req.body().path("stream").asBoolean()).isTrue();
    }

    /** 스트림 중간의 overloaded_error는 서버 오류: 한 번에 받기로 다시 보낸다. 거절(refusal)은 한 번에 받기와 같은 안내. */
    @Test
    void anthropicOverloadedMidStreamFallsBackAndRefusalIsExplained() {
        server.then(Reply.sse(List.of(
                event("message_start", "{\"type\":\"message_start\",\"message\":{\"usage\":{\"input_tokens\":5}}}"),
                event("error", "{\"type\":\"error\",\"error\":{\"type\":\"overloaded_error\",\"message\":\"Overloaded\"}}"))))
                .then(Reply.ok("{\"content\":[{\"type\":\"text\",\"text\":\"다시 받은 답\"}],\"stop_reason\":\"end_turn\"}"));
        assertThat(anthropic().chatStreaming("", List.of(Message.user("q")), TOOLS, true, new Collected()).text())
                .isEqualTo("다시 받은 답");
        assertThat(server.requests()).hasSize(2);
        assertThat(server.requests().get(1).body().has("stream")).isFalse();

        server.reset();
        server.then(Reply.sse(List.of(
                event("message_start", "{\"type\":\"message_start\",\"message\":{}}"),
                event("message_delta", "{\"type\":\"message_delta\",\"delta\":{\"stop_reason\":\"refusal\"}}"),
                event("message_stop", "{\"type\":\"message_stop\"}"))));
        assertThatThrownBy(() -> anthropic().stream("", List.of(Message.user("q")), TOOLS, true, new Collected()))
                .isInstanceOfSatisfying(LlmException.class, e -> {
                    assertThat(e.kind()).isEqualTo(Kind.BAD_RESPONSE);
                    assertThat(e.getMessage()).contains("답하지 않았습니다");
                });
    }

    /** 스트리밍하지 않는 구현(기본 메서드)은 chatStreaming이 그냥 chat이다. */
    @Test
    void clientsWithoutStreamingUseChat() {
        NotConfiguredLlmClient none = new NotConfiguredLlmClient();
        assertThat(none.streams()).isFalse();
        assertThatThrownBy(() -> none.chatStreaming("", List.of(), List.of(), true, new Collected()))
                .isInstanceOfSatisfying(LlmException.class, e -> assertThat(e.kind()).isEqualTo(Kind.NOT_CONFIGURED));
    }
}
