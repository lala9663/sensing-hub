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

    /**
     * events가 있으면 SSE(text/event-stream)로 답한다: 이벤트마다 빈 줄을 붙여 보내고 flush, 사이에 eventDelayMillis만큼 쉰다
     * (스트리밍이 정말 조금씩 오는지 볼 수 있게). 이벤트는 "data: {...}" 또는 "event: x\ndata: {...}" 글 그대로다.
     */
    public record Reply(int status, String body, Map<String, String> headers, long delayMillis, List<String> events,
                        long eventDelayMillis) {

        public Reply(int status, String body, Map<String, String> headers, long delayMillis) {
            this(status, body, headers, delayMillis, null, 0);
        }

        public static Reply ok(String body) {
            return new Reply(200, body, Map.of(), 0);
        }

        public static Reply status(int status, String body) {
            return new Reply(status, body, Map.of(), 0);
        }

        public static Reply sse(List<String> events) {
            return sse(0, events);
        }

        public static Reply sse(long eventDelayMillis, List<String> events) {
            return new Reply(200, "", Map.of(), 0, List.copyOf(events), eventDelayMillis);
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
                reply.headers().forEach((k, v) -> exchange.getResponseHeaders().add(k, v));
                if (reply.events() != null) {
                    exchange.getResponseHeaders().add("Content-Type", "text/event-stream");
                    try {
                        exchange.sendResponseHeaders(reply.status(), 0);   // chunked
                        try (OutputStream os = exchange.getResponseBody()) {
                            for (int i = 0; i < reply.events().size(); i++) {
                                if (i > 0 && reply.eventDelayMillis() > 0) {
                                    Thread.sleep(reply.eventDelayMillis());
                                }
                                os.write((reply.events().get(i) + "\n\n").getBytes(StandardCharsets.UTF_8));
                                os.flush();
                            }
                        }
                    } catch (IOException e) {
                        // 클라이언트가 먼저 끊었다
                    } catch (InterruptedException e) {
                        Thread.currentThread().interrupt();
                    }
                    return;
                }
                byte[] out = reply.body().getBytes(StandardCharsets.UTF_8);
                exchange.getResponseHeaders().add("Content-Type", "application/json");
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

    // ------------------------------------------------------------------ OpenAI 호환 스트리밍 (SSE chat.completion.chunk)

    /** 글 조각마다 delta.content, 끝에 finish_reason stop, 사용량만 든 조각, [DONE]. */
    public static List<String> openAiStreamText(String... pieces) {
        List<String> events = new ArrayList<>();
        for (int i = 0; i < pieces.length; i++) {
            ObjectNode chunk = chunk();
            ObjectNode delta = ((ObjectNode) chunk.get("choices").get(0)).putObject("delta");
            if (i == 0) {
                delta.put("role", "assistant");
            }
            delta.put("content", pieces[i]);
            events.add("data: " + chunk);
        }
        ObjectNode last = chunk();
        ((ObjectNode) last.get("choices").get(0)).putObject("delta");
        ((ObjectNode) last.get("choices").get(0)).put("finish_reason", "stop");
        events.add("data: " + last);
        events.add("data: " + usageChunk(120, 30));
        events.add("data: [DONE]");
        return events;
    }

    /**
     * 도구 호출 하나를 여러 조각으로: 첫 조각에 id·이름(과 Gemini thought_signature가 있으면 extra_content), 이어서 arguments 조각들.
     * Gemini처럼 index 없이 줄 수도 있다 (withIndex false).
     */
    public static List<String> openAiStreamToolCall(String id, String name, String thoughtSignature, boolean withIndex,
                                                    String... argumentParts) {
        List<String> events = new ArrayList<>();
        for (int i = 0; i < Math.max(1, argumentParts.length); i++) {
            ObjectNode chunk = chunk();
            ObjectNode delta = ((ObjectNode) chunk.get("choices").get(0)).putObject("delta");
            ObjectNode call = delta.putArray("tool_calls").addObject();
            if (withIndex) {
                call.put("index", 0);
            }
            ObjectNode fn = call.putObject("function");
            if (i == 0) {
                delta.put("role", "assistant");
                call.put("id", id);
                call.put("type", "function");
                fn.put("name", name);
                if (thoughtSignature != null) {
                    call.putObject("extra_content").putObject("google").put("thought_signature", thoughtSignature);
                }
            }
            fn.put("arguments", argumentParts.length == 0 ? "{}" : argumentParts[i]);
            events.add("data: " + chunk);
        }
        ObjectNode last = chunk();
        ((ObjectNode) last.get("choices").get(0)).putObject("delta");
        ((ObjectNode) last.get("choices").get(0)).put("finish_reason", "tool_calls");
        events.add("data: " + last);
        events.add("data: " + usageChunk(100, 10));
        events.add("data: [DONE]");
        return events;
    }

    private static ObjectNode chunk() {
        ObjectNode root = JSON.createObjectNode();
        root.put("object", "chat.completion.chunk");
        root.putArray("choices").addObject().put("index", 0);
        return root;
    }

    private static ObjectNode usageChunk(int prompt, int completion) {
        ObjectNode root = JSON.createObjectNode();
        root.put("object", "chat.completion.chunk");
        root.putArray("choices");
        root.putObject("usage").put("prompt_tokens", prompt).put("completion_tokens", completion);
        return root;
    }

    /** OpenAI 호환 /embeddings 응답. Gemini처럼 첫 항목의 index(0)는 빼고 준다. */
    public static String openAiEmbedding(float[]... vectors) {
        ObjectNode root = JSON.createObjectNode();
        root.put("object", "list");
        ArrayNode data = root.putArray("data");
        for (int i = 0; i < vectors.length; i++) {
            ObjectNode item = data.addObject();
            item.put("object", "embedding");
            if (i > 0) {
                item.put("index", i);
            }
            ArrayNode values = item.putArray("embedding");
            for (float v : vectors[i]) {
                values.add(v);
            }
        }
        root.put("model", "mock-embed");
        return root.toString();
    }

    /** 1024차원 벡터: 칸 i에 값 v를 둔다 (나머지 0). {@code vector(0, 0.8f, 1, 0.6f)}처럼 칸·값을 번갈아 준다. */
    public static float[] vector(Object... indexValuePairs) {
        float[] v = new float[1024];
        for (int i = 0; i < indexValuePairs.length; i += 2) {
            v[(Integer) indexValuePairs[i]] = ((Number) indexValuePairs[i + 1]).floatValue();
        }
        return v;
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
