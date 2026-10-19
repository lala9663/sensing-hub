package io.sensinghub.llm;

import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentLinkedDeque;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.function.Function;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.sun.net.httpserver.HttpServer;

/**
 * 가짜 LLM 서버 (테스트 전용, 실제 LLM을 부르지 않는다). 받은 요청을 기록하고, 미리 넣어 둔 답을 차례로 돌려준다.
 * 답이 바닥나면 500 — 테스트가 예상보다 LLM을 더 불렀다는 뜻이다.
 */
public final class MockLlmServer implements AutoCloseable {

    public record Reply(int status, String body, Map<String, String> headers, long delayMillis) {

        public static Reply ok(String body) {
            return new Reply(200, body, Map.of(), 0);
        }

        public static Reply status(int status, String body) {
            return new Reply(status, body, Map.of(), 0);
        }
    }

    public record Recorded(String path, Map<String, List<String>> headers, JsonNode body) {

        public String header(String name) {
            for (Map.Entry<String, List<String>> e : headers.entrySet()) {
                if (e.getKey().equalsIgnoreCase(name) && !e.getValue().isEmpty()) {
                    return e.getValue().get(0);
                }
            }
            return null;
        }
    }

    private static final ObjectMapper JSON = new ObjectMapper();

    private final HttpServer server;
    private final ConcurrentLinkedDeque<Function<JsonNode, Reply>> script = new ConcurrentLinkedDeque<>();
    private final List<Recorded> requests = new CopyOnWriteArrayList<>();
    private final List<String> failures = new CopyOnWriteArrayList<>();

    private MockLlmServer(HttpServer server) {
        this.server = server;
    }

    public static MockLlmServer start() {
        try {
            HttpServer http = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
            MockLlmServer mock = new MockLlmServer(http);
            http.createContext("/", exchange -> {
                byte[] raw;
                try (InputStream in = exchange.getRequestBody()) {
                    raw = in.readAllBytes();
                }
                JsonNode body = raw.length == 0 ? JSON.nullNode() : JSON.readTree(raw);
                mock.requests.add(new Recorded(exchange.getRequestURI().getPath(),
                        Map.copyOf(exchange.getRequestHeaders()), body));
                Function<JsonNode, Reply> next = mock.script.pollFirst();
                Reply reply;
                try {
                    if (next == null) {
                        mock.failures.add("LLM이 예상보다 많이 불렸다: " + body);
                        reply = Reply.status(400, "{\"error\":\"no scripted reply\"}");
                    } else {
                        reply = next.apply(body);
                    }
                } catch (RuntimeException | AssertionError e) {
                    // 400은 재시도하지 않으므로 다음 답을 빼앗지 않는다. 테스트는 failures()로 원인을 본다
                    mock.failures.add(String.valueOf(e.getMessage()));
                    reply = Reply.status(400, "{\"error\":\"scripted reply failed\"}");
                }
                if (reply.delayMillis() > 0) {
                    try {
                        Thread.sleep(reply.delayMillis());
                    } catch (InterruptedException e) {
                        Thread.currentThread().interrupt();
                    }
                }
                byte[] out = reply.body().getBytes(StandardCharsets.UTF_8);
                exchange.getResponseHeaders().add("Content-Type", "application/json");
                reply.headers().forEach((k, v) -> exchange.getResponseHeaders().add(k, v));
                try {
                    exchange.sendResponseHeaders(reply.status(), out.length);
                    try (OutputStream os = exchange.getResponseBody()) {
                        os.write(out);
                    }
                } catch (IOException e) {
                    // 클라이언트가 시간 초과로 먼저 끊었다
                }
            });
            http.setExecutor(java.util.concurrent.Executors.newCachedThreadPool());
            http.start();
            return mock;
        } catch (IOException e) {
            throw new IllegalStateException(e);
        }
    }

    public String baseUrl() {
        return "http://127.0.0.1:" + server.getAddress().getPort();
    }

    /** 다음 요청에 돌려줄 답. 함수는 받은 요청 본문을 보고 답을 만든다 (검사도 여기서 할 수 있다). */
    public MockLlmServer then(Function<JsonNode, Reply> reply) {
        script.addLast(reply);
        return this;
    }

    public MockLlmServer then(Reply reply) {
        return then(body -> reply);
    }

    public List<Recorded> requests() {
        return new ArrayList<>(requests);
    }

    public int pending() {
        return script.size();
    }

    /** 답 함수 안의 검사 실패, 남은 답 없이 온 요청. */
    public List<String> failures() {
        return new ArrayList<>(failures);
    }

    public void reset() {
        script.clear();
        requests.clear();
        failures.clear();
    }

    @Override
    public void close() {
        server.stop(0);
    }

    // ------------------------------------------------------------------ OpenAI 호환 응답

    public static String openAiText(String text) {
        ObjectNode root = JSON.createObjectNode();
        ObjectNode choice = root.putArray("choices").addObject();
        choice.putObject("message").put("role", "assistant").put("content", text);
        choice.put("finish_reason", "stop");
        root.putObject("usage").put("prompt_tokens", 100).put("completion_tokens", 20);
        return root.toString();
    }

    /** arguments는 JSON 문자열. thoughtSignature가 있으면 Gemini처럼 extra_content.google.thought_signature를 붙인다. */
    public static String openAiToolCall(String id, String name, String arguments, String thoughtSignature) {
        ObjectNode root = JSON.createObjectNode();
        ObjectNode choice = root.putArray("choices").addObject();
        ObjectNode message = choice.putObject("message");
        message.put("role", "assistant");
        message.putNull("content");
        ArrayNode calls = message.putArray("tool_calls");
        ObjectNode call = calls.addObject();
        call.put("id", id);
        call.put("type", "function");
        call.putObject("function").put("name", name).put("arguments", arguments);
        if (thoughtSignature != null) {
            call.putObject("extra_content").putObject("google").put("thought_signature", thoughtSignature);
        }
        choice.put("finish_reason", "tool_calls");
        root.putObject("usage").put("prompt_tokens", 100).put("completion_tokens", 10);
        return root.toString();
    }

    /** Gemini 무료 등급이 하루 한도를 넘었을 때의 429 본문 (OpenAI 호환 주소는 배열로 감싸 준다). */
    public static final String GEMINI_DAILY_429 = """
            [{"error":{"code":429,"message":"You exceeded your current quota, please check your plan and billing details. \
            Quota exceeded for metric: generativelanguage.googleapis.com/generate_content_free_tier_requests, limit: 500",\
            "status":"RESOURCE_EXHAUSTED","details":[{"@type":"type.googleapis.com/google.rpc.QuotaFailure",\
            "violations":[{"quotaId":"GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}]}}]""";

    public static final String GEMINI_MINUTE_429 = """
            [{"error":{"code":429,"message":"Resource has been exhausted. Please retry in 41.3s.",\
            "status":"RESOURCE_EXHAUSTED","details":[{"@type":"type.googleapis.com/google.rpc.QuotaFailure",\
            "violations":[{"quotaId":"GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}]}]}}]""";
}
