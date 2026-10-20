package io.sensinghub.llm;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;

import io.sensinghub.llm.LlmException.Kind;

/**
 * Claude Messages API ({@code {ANTHROPIC_BASE_URL}/v1/messages}) + tools. 키는 워커와 같은 ANTHROPIC_API_KEY.
 * <ul>
 *   <li>도구 결과는 user 메시지의 tool_result 블록이다. 연달아 오는 결과와 그 뒤 사용자 글은 한 user 메시지로 합친다
 *       (tool_result 블록이 앞, 글이 뒤).</li>
 *   <li>allowTools=false면 tool_choice none: 대화에 tool_use가 있으면 도구 정의를 빼면 안 되므로 정의는 그대로 보낸다.</li>
 *   <li>스트리밍({@link #stream})은 같은 요청에 {@code stream: true}를 더한 SSE다 (실패 처리는 OpenAI 호환과 같다).</li>
 * </ul>
 */
public class AnthropicLlmClient implements LlmClient {

    static final String VERSION = "2023-06-01";
    private static final TypeReference<Map<String, Object>> MAP = new TypeReference<>() { };

    private final String baseUrl;
    private final String model;
    private final String apiKey;
    private final int maxTokens;
    private final LlmHttp http;
    private final ObjectMapper json;

    public AnthropicLlmClient(String baseUrl, String model, String apiKey, int timeoutSeconds, int maxTokens,
                              long retryDelayMillis, ObjectMapper json) {
        this.baseUrl = baseUrl.replaceAll("/+$", "");
        this.model = model;
        this.apiKey = apiKey.strip();
        this.maxTokens = maxTokens > 0 ? maxTokens : 4096;
        this.json = json;
        this.http = new LlmHttp(json, timeoutSeconds, retryDelayMillis);
    }

    @Override
    public String describe() {
        return "anthropic " + model;
    }

    @Override
    public Response chat(String system, List<Message> messages, List<Tool> tools, boolean allowTools) {
        return parse(http.post(baseUrl + "/v1/messages", headers(), body(system, messages, tools, allowTools)));
    }

    @Override
    public boolean streams() {
        return http.streamingAvailable();
    }

    /**
     * {@code stream: true} (SSE). content_block_start/delta/stop으로 글(text_delta)과 도구 호출(tool_use + input_json_delta 조각)을 모은다.
     * 사용량은 message_start(input)와 message_delta(output), 중간 error 이벤트는 종류대로 (overloaded·api 오류는 서버 오류).
     * message_stop 없이 끝나면 끊긴 스트림(NETWORK)이다.
     */
    @Override
    public Response stream(String system, List<Message> messages, List<Tool> tools, boolean allowTools,
                           StreamListener listener) {
        Map<String, Object> body = body(system, messages, tools, allowTools);
        body.put("stream", true);
        StreamedResponse acc = new StreamedResponse(listener);
        try {
            JsonNode plain = http.stream(baseUrl + "/v1/messages", headers(), body, acc::event);
            return plain != null ? parse(plain) : acc.response();
        } catch (LlmException e) {
            http.streamFailed(e);
            throw e;
        }
    }

    private Map<String, String> headers() {
        return Map.of("x-api-key", apiKey, "anthropic-version", VERSION);
    }

    private Map<String, Object> body(String system, List<Message> messages, List<Tool> tools, boolean allowTools) {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("model", model);
        body.put("max_tokens", maxTokens);
        if (system != null && !system.isBlank()) {
            body.put("system", system);
        }
        body.put("messages", messages(messages));
        if (tools != null && !tools.isEmpty()) {
            List<Map<String, Object>> defs = new ArrayList<>();
            for (Tool t : tools) {
                defs.add(Map.of("name", t.name(), "description", t.description(), "input_schema", t.inputSchema()));
            }
            body.put("tools", defs);
            body.put("tool_choice", Map.of("type", allowTools ? "auto" : "none"));
        }
        return body;
    }

    private List<Map<String, Object>> messages(List<Message> messages) {
        List<Map<String, Object>> out = new ArrayList<>();
        for (Message m : messages) {
            List<Map<String, Object>> blocks = new ArrayList<>();
            String role;
            if (ASSISTANT.equals(m.role())) {
                role = ASSISTANT;
                if (m.content() != null && !m.content().isBlank()) {
                    blocks.add(Map.of("type", "text", "text", m.content()));
                }
                for (ToolCall c : m.toolCalls() == null ? List.<ToolCall>of() : m.toolCalls()) {
                    blocks.add(Map.of("type", "tool_use", "id", c.id(), "name", c.name(),
                            "input", c.arguments() == null ? Map.of() : c.arguments()));
                }
            } else if (TOOL.equals(m.role())) {
                role = USER;
                blocks.add(Map.of("type", "tool_result", "tool_use_id", m.toolCallId(),
                        "content", m.content() == null ? "" : m.content()));
            } else {
                role = USER;
                if (m.content() != null && !m.content().isBlank()) {
                    blocks.add(Map.of("type", "text", "text", m.content()));
                }
            }
            if (blocks.isEmpty()) {
                continue;   // 빈 메시지는 API가 거절한다
            }
            Map<String, Object> last = out.isEmpty() ? null : out.get(out.size() - 1);
            if (last != null && role.equals(last.get("role"))) {
                @SuppressWarnings("unchecked")
                List<Map<String, Object>> merged = (List<Map<String, Object>>) last.get("content");
                merged.addAll(blocks);
            } else {
                Map<String, Object> msg = new LinkedHashMap<>();
                msg.put("role", role);
                msg.put("content", blocks);
                out.add(msg);
            }
        }
        return out;
    }

    private Response parse(JsonNode data) {
        String stop = data.path("stop_reason").asText(null);
        if ("refusal".equals(stop)) {
            throw new LlmException(Kind.BAD_RESPONSE, "모델이 이 요청에 답하지 않았습니다. 질문을 바꿔 다시 시도해 주세요.",
                    null, "stop_reason=refusal");
        }
        JsonNode content = data.path("content");
        if (!content.isArray()) {
            throw new LlmException(Kind.BAD_RESPONSE, "LLM 응답을 해석하지 못했습니다. 잠시 후 다시 시도해 주세요.", null,
                    LlmHttp.head(data.toString()));
        }
        StringBuilder text = new StringBuilder();
        List<ToolCall> calls = new ArrayList<>();
        for (JsonNode block : content) {
            String type = block.path("type").asText("");
            if ("text".equals(type)) {
                text.append(block.path("text").asText(""));
            } else if ("tool_use".equals(type)) {
                Map<String, Object> input = block.path("input").isObject()
                        ? json.convertValue(block.path("input"), MAP) : Map.of();
                calls.add(new ToolCall(block.path("id").asText(), block.path("name").asText(), input));
            }
        }
        Map<String, Object> usage = new LinkedHashMap<>();
        JsonNode u = data.path("usage");
        if (u.isObject()) {
            usage.put("inputTokens", u.path("input_tokens").asInt(0));
            usage.put("outputTokens", u.path("output_tokens").asInt(0));
        }
        return new Response(text.toString(), calls, usage, stop);
    }

    /**
     * 스트림 이벤트를 모아 한 번에 받은 응답과 같은 모양의 메시지 JSON(content 블록·stop_reason·usage)을 만들고 {@link #parse}로 해석한다
     * (거절·사용량 처리가 한 곳에 있게).
     */
    private final class StreamedResponse {

        private final StreamListener listener;
        private final Map<Integer, ObjectNode> blocks = new TreeMap<>();
        private final Map<Integer, StringBuilder> inputs = new TreeMap<>();
        private final ObjectNode usage = json.createObjectNode();
        private String stop;
        private boolean started;
        private boolean stopped;

        StreamedResponse(StreamListener listener) {
            this.listener = listener;
        }

        void event(String event, String data) {
            JsonNode node;
            try {
                node = json.readTree(data);
            } catch (JsonProcessingException e) {
                throw new LlmException(Kind.BAD_RESPONSE, "LLM 응답을 해석하지 못했습니다. 잠시 후 다시 시도해 주세요.", null,
                        LlmHttp.head(data));
            }
            if (node == null) {
                return;
            }
            String type = event != null ? event : node.path("type").asText("");
            switch (type) {
                case "message_start" -> {
                    started = true;
                    addUsage(node.path("message").path("usage"));
                }
                case "content_block_start" -> {
                    int index = node.path("index").asInt(blocks.size());
                    ObjectNode block = node.path("content_block").isObject()
                            ? ((ObjectNode) node.path("content_block")).deepCopy() : json.createObjectNode();
                    blocks.put(index, block);
                    String text = block.path("text").asText("");
                    if ("text".equals(block.path("type").asText()) && !text.isEmpty()) {
                        listener.text(text);
                    }
                }
                case "content_block_delta" -> delta(node.path("index").asInt(-1), node.path("delta"));
                case "message_delta" -> {
                    String s = node.path("delta").path("stop_reason").asText(null);
                    if (s != null && !s.isBlank()) {
                        stop = s;
                    }
                    addUsage(node.path("usage"));
                }
                case "message_stop" -> stopped = true;
                case "error" -> throw streamError(node.path("error"));
                default -> {
                    // ping, content_block_stop
                }
            }
        }

        private void delta(int index, JsonNode delta) {
            ObjectNode block = blocks.get(index);
            if (block == null) {
                return;
            }
            switch (delta.path("type").asText("")) {
                case "text_delta" -> {
                    String piece = delta.path("text").asText("");
                    block.put("text", block.path("text").asText("") + piece);
                    if (!piece.isEmpty() && "text".equals(block.path("type").asText())) {
                        listener.text(piece);
                    }
                }
                case "input_json_delta" -> inputs.computeIfAbsent(index, k -> new StringBuilder())
                        .append(delta.path("partial_json").asText(""));
                default -> {
                    // thinking 등은 쓰지 않는다
                }
            }
        }

        private void addUsage(JsonNode u) {
            if (!u.isObject()) {
                return;
            }
            for (String key : List.of("input_tokens", "output_tokens")) {
                if (u.path(key).isNumber() && u.path(key).asInt() > 0) {
                    usage.put(key, u.path(key).asInt());
                }
            }
        }

        Response response() {
            if (!started) {
                throw new LlmException(Kind.BAD_RESPONSE, "LLM 응답을 해석하지 못했습니다. 잠시 후 다시 시도해 주세요.", null,
                        "stream without message_start");
            }
            if (!stopped) {
                throw new LlmException(Kind.NETWORK, "LLM 응답을 받다가 연결이 끊겼습니다. 잠시 후 다시 시도해 주세요.", null,
                        "stream ended without message_stop");
            }
            ObjectNode message = json.createObjectNode();
            if (stop != null) {
                message.put("stop_reason", stop);
            }
            ArrayNode content = message.putArray("content");
            blocks.forEach((index, block) -> {
                StringBuilder input = inputs.get(index);
                if ("tool_use".equals(block.path("type").asText()) && input != null && !input.toString().isBlank()) {
                    try {
                        block.set("input", json.readTree(input.toString()));
                    } catch (JsonProcessingException e) {
                        block.set("input", json.createObjectNode());   // 해석하지 못한 인자: 도구가 인자 오류로 답한다
                    }
                }
                content.add(block);
            });
            if (!usage.isEmpty()) {
                message.set("usage", usage);
            }
            return parse(message);
        }
    }

    /** 스트림 중간의 error 이벤트 (HTTP 상태 없이 온다). */
    private static LlmException streamError(JsonNode error) {
        String type = error.path("type").asText("");
        String detail = LlmHttp.head(error.toString());
        return switch (type) {
            case "overloaded_error" -> LlmHttp.error(529, detail, null);
            case "api_error" -> LlmHttp.error(500, detail, null);
            case "rate_limit_error" -> LlmHttp.error(429, detail, null);
            default -> new LlmException(Kind.BAD_RESPONSE, "LLM이 답을 만들다 오류가 났습니다. 잠시 후 다시 시도해 주세요.", null, detail);
        };
    }
}
