package io.sensinghub.llm;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;

import io.sensinghub.llm.LlmException.Kind;

/**
 * OpenAI 호환 {@code {base_url}/chat/completions} + tools(function calling). 사내 게이트웨이(vLLM·Ollama)와
 * Gemini의 OpenAI 호환 주소({@code https://generativelanguage.googleapis.com/v1beta/openai})에 쓴다.
 * 워커 llm.OpenAiCompatLlm과 같은 환경 변수(SENSING_LLM_BASE_URL·MODEL·API_KEY)를 읽는다.
 * <ul>
 *   <li>temperature는 보내지 않는다: Gemini 3 계열은 기본값(1.0)에서 벗어나면 반복·품질 저하가 난다고 안내한다.</li>
 *   <li>도구 호출의 {@code extra_content}(Gemini 3의 thought_signature)는 {@link LlmClient.ToolCall#providerData()}에 담아 다음 요청에
 *       그대로 돌려준다. 빠뜨리면 Gemini 3가 도구 결과를 담은 요청을 400으로 거절한다.</li>
 *   <li>스트리밍({@link #stream})은 같은 요청에 {@code stream: true}를 더한 SSE다. 스트리밍을 거절(400)하거나 형식이 깨지면
 *       {@link LlmHttp#STREAM_PAUSE} 동안 스트리밍을 쉰다 ({@link #streams()} false).</li>
 * </ul>
 */
public class OpenAiCompatLlmClient implements LlmClient {

    private static final TypeReference<Map<String, Object>> MAP = new TypeReference<>() { };

    private final String baseUrl;
    private final String model;
    private final String apiKey;
    private final int maxTokens;
    private final LlmHttp http;
    private final ObjectMapper json;

    public OpenAiCompatLlmClient(String baseUrl, String model, String apiKey, int timeoutSeconds, int maxTokens,
                                 long retryDelayMillis, ObjectMapper json) {
        this.baseUrl = baseUrl.replaceAll("/+$", "");
        this.model = model;
        this.apiKey = apiKey == null ? "" : apiKey.strip();
        this.maxTokens = maxTokens;
        this.json = json;
        this.http = new LlmHttp(json, timeoutSeconds, retryDelayMillis);
    }

    @Override
    public String describe() {
        return "openai " + model;
    }

    @Override
    public Response chat(String system, List<Message> messages, List<Tool> tools, boolean allowTools) {
        return parse(http.post(baseUrl + "/chat/completions", headers(), body(system, messages, tools, allowTools)));
    }

    @Override
    public boolean streams() {
        return http.streamingAvailable();
    }

    /**
     * {@code stream: true} + {@code stream_options.include_usage}(마지막 조각에 사용량). 조각의 delta.content는 글, delta.tool_calls는
     * index별로 이어 붙인다 (id·이름은 첫 조각, arguments는 여러 조각). 도구 호출의 그 밖의 필드(Gemini extra_content.google.thought_signature)는
     * 한 번에 받기와 같이 providerData에 담는다. [DONE]도 finish_reason도 없이 끝나면 끊긴 스트림(NETWORK)이다.
     */
    @Override
    public Response stream(String system, List<Message> messages, List<Tool> tools, boolean allowTools,
                           StreamListener listener) {
        Map<String, Object> body = body(system, messages, tools, allowTools);
        body.put("stream", true);
        body.put("stream_options", Map.of("include_usage", true));
        StreamedResponse acc = new StreamedResponse(listener);
        try {
            JsonNode plain = http.stream(baseUrl + "/chat/completions", headers(), body, (event, data) -> acc.chunk(data));
            return plain != null ? parse(plain) : acc.response();
        } catch (LlmException e) {
            http.streamFailed(e);
            throw e;
        }
    }

    private Map<String, String> headers() {
        return apiKey.isEmpty() ? Map.of() : Map.of("Authorization", "Bearer " + apiKey);
    }

    private Map<String, Object> body(String system, List<Message> messages, List<Tool> tools, boolean allowTools) {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("model", model);
        body.put("messages", messages(system, messages));
        if (maxTokens > 0) {
            body.put("max_tokens", maxTokens);
        }
        if (tools != null && !tools.isEmpty()) {
            List<Map<String, Object>> defs = new ArrayList<>();
            for (Tool t : tools) {
                defs.add(Map.of("type", "function", "function",
                        Map.of("name", t.name(), "description", t.description(), "parameters", t.inputSchema())));
            }
            body.put("tools", defs);
            body.put("tool_choice", allowTools ? "auto" : "none");
        }
        return body;
    }

    private List<Map<String, Object>> messages(String system, List<Message> messages) {
        List<Map<String, Object>> out = new ArrayList<>();
        if (system != null && !system.isBlank()) {
            out.add(Map.of("role", "system", "content", system));
        }
        for (Message m : messages) {
            Map<String, Object> msg = new LinkedHashMap<>();
            msg.put("role", m.role());
            switch (m.role()) {
                case ASSISTANT -> {
                    boolean hasCalls = m.toolCalls() != null && !m.toolCalls().isEmpty();
                    msg.put("content", m.content() == null || m.content().isEmpty() ? (hasCalls ? null : "") : m.content());
                    if (hasCalls) {
                        List<Map<String, Object>> calls = new ArrayList<>();
                        for (ToolCall c : m.toolCalls()) {
                            Map<String, Object> call = new LinkedHashMap<>();
                            call.put("id", c.id());
                            call.put("type", "function");
                            call.put("function", Map.of("name", c.name(), "arguments", toJson(c.arguments())));
                            if (c.providerData() != null) {
                                call.putAll(c.providerData());
                            }
                            calls.add(call);
                        }
                        msg.put("tool_calls", calls);
                    }
                }
                case TOOL -> {
                    msg.put("tool_call_id", m.toolCallId());
                    if (m.toolName() != null) {
                        msg.put("name", m.toolName());
                    }
                    msg.put("content", m.content() == null ? "" : m.content());
                }
                default -> msg.put("content", m.content() == null ? "" : m.content());
            }
            out.add(msg);
        }
        return out;
    }

    private Response parse(JsonNode data) {
        JsonNode choice = data.path("choices").path(0);
        JsonNode message = choice.path("message");
        if (message.isMissingNode()) {
            throw new LlmException(Kind.BAD_RESPONSE, "LLM 응답을 해석하지 못했습니다. 잠시 후 다시 시도해 주세요.", null,
                    LlmHttp.head(data.toString()));
        }
        String text = text(message.path("content"));
        List<ToolCall> calls = new ArrayList<>();
        int n = 0;
        for (JsonNode call : message.path("tool_calls")) {
            n++;
            JsonNode fn = call.path("function");
            String name = fn.path("name").asText("");
            if (name.isEmpty()) {
                continue;
            }
            String id = call.path("id").asText("");
            if (id.isEmpty()) {
                id = "call_" + n;   // 일부 서버는 id를 주지 않는다. 결과 메시지와 짝만 맞으면 된다
            }
            calls.add(new ToolCall(id, name, arguments(fn.path("arguments")), extras(call)));
        }
        return new Response(text, calls, usage(data.path("usage")), choice.path("finish_reason").asText(null));
    }

    private static Map<String, Object> usage(JsonNode u) {
        Map<String, Object> usage = new LinkedHashMap<>();
        if (u.isObject()) {
            usage.put("inputTokens", u.path("prompt_tokens").asInt(0));
            usage.put("outputTokens", u.path("completion_tokens").asInt(0));
        }
        return usage;
    }

    /** 도구 호출의 id·type·function·index 밖 필드 (Gemini 3의 extra_content). 다음 요청에 그대로 돌려준다. */
    private Map<String, Object> extras(JsonNode call) {
        Map<String, Object> extra = new LinkedHashMap<>();
        call.fields().forEachRemaining(e -> {
            if (!List.of("id", "type", "function", "index").contains(e.getKey())) {
                extra.put(e.getKey(), json.convertValue(e.getValue(), Object.class));
            }
        });
        return extra;
    }

    /** 맵을 깊게 합친다 (같은 도구 호출의 extra_content가 여러 조각에 나뉘어 와도 잃지 않게). */
    @SuppressWarnings("unchecked")
    static void merge(Map<String, Object> into, Map<String, Object> add) {
        add.forEach((k, v) -> {
            if (into.get(k) instanceof Map<?, ?> a && v instanceof Map<?, ?> b) {
                Map<String, Object> m = new LinkedHashMap<>((Map<String, Object>) a);
                merge(m, (Map<String, Object>) b);
                into.put(k, m);
            } else {
                into.put(k, v);
            }
        });
    }

    /** 스트림 조각(chat.completion.chunk)을 모아 한 번에 받은 답과 같은 {@link Response}를 만든다. */
    private final class StreamedResponse {

        private final StreamListener listener;
        private final StringBuilder text = new StringBuilder();
        private final List<CallParts> calls = new ArrayList<>();
        private final Map<Integer, CallParts> byIndex = new HashMap<>();
        private Map<String, Object> usage = new LinkedHashMap<>();
        private String finishReason;
        private boolean done;
        private boolean choices;

        StreamedResponse(StreamListener listener) {
            this.listener = listener;
        }

        void chunk(String data) {
            if ("[DONE]".equals(data.strip())) {
                done = true;
                return;
            }
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
            if (node.isArray() && !node.isEmpty()) {
                node = node.get(0);   // Gemini는 오류를 배열로 감싸 준다
            }
            JsonNode error = node.path("error");
            if (error.isObject()) {
                int code = error.path("code").asInt(0);
                throw code >= 400 ? LlmHttp.error(code, data, null)
                        : new LlmException(Kind.BAD_RESPONSE, "LLM이 답을 만들다 오류가 났습니다. 잠시 후 다시 시도해 주세요.", null,
                                LlmHttp.head(data));
            }
            if (node.path("usage").isObject()) {
                usage = usage(node.path("usage"));
            }
            JsonNode choice = node.path("choices").path(0);
            if (choice.isMissingNode()) {
                return;   // 사용량만 든 마지막 조각
            }
            choices = true;
            JsonNode delta = choice.has("delta") ? choice.path("delta") : choice.path("message");
            String piece = text(delta.path("content"));
            if (!piece.isEmpty()) {
                text.append(piece);
                listener.text(piece);
            }
            for (JsonNode call : delta.path("tool_calls")) {
                add(call);
            }
            String finish = choice.path("finish_reason").asText(null);
            if (finish != null && !finish.isBlank()) {
                finishReason = finish;
            }
        }

        /** 같은 index(없으면 마지막 호출)에 이어 붙인다. 같은 index에 다른 id가 오면 새 호출이다 (병렬 호출을 모두 index 0으로 주는 서버). */
        private void add(JsonNode call) {
            int index = call.path("index").isNumber() ? call.path("index").asInt() : -1;
            String id = call.path("id").asText("");
            CallParts parts = index >= 0 ? byIndex.get(index) : calls.isEmpty() ? null : calls.get(calls.size() - 1);
            if (parts == null || !id.isEmpty() && !parts.id.isEmpty() && !id.equals(parts.id)) {
                parts = new CallParts();
                calls.add(parts);
                if (index >= 0) {
                    byIndex.put(index, parts);
                }
            }
            if (!id.isEmpty()) {
                parts.id = id;
            }
            JsonNode fn = call.path("function");
            String name = fn.path("name").asText("");
            if (!name.isEmpty() && !name.contentEquals(parts.name)) {
                parts.name.append(name);
            }
            JsonNode args = fn.path("arguments");
            if (args.isObject()) {
                parts.objectArgs = args;
            } else if (args.isTextual()) {
                parts.args.append(args.asText());
            }
            merge(parts.extra, extras(call));
        }

        Response response() {
            if (!choices) {
                throw new LlmException(Kind.BAD_RESPONSE, "LLM 응답을 해석하지 못했습니다. 잠시 후 다시 시도해 주세요.", null,
                        "stream without choices");
            }
            if (!done && finishReason == null) {
                throw new LlmException(Kind.NETWORK, "LLM 응답을 받다가 연결이 끊겼습니다. 잠시 후 다시 시도해 주세요.", null,
                        "stream ended without [DONE] or finish_reason");
            }
            List<ToolCall> out = new ArrayList<>();
            int n = 0;
            for (CallParts p : calls) {
                n++;
                if (p.name.isEmpty()) {
                    continue;
                }
                JsonNode args = p.objectArgs != null ? p.objectArgs : json.getNodeFactory().textNode(p.args.toString());
                out.add(new ToolCall(p.id.isEmpty() ? "call_" + n : p.id, p.name.toString(), arguments(args), p.extra));
            }
            return new Response(text.toString(), out, usage, finishReason);
        }
    }

    private static final class CallParts {
        String id = "";
        final StringBuilder name = new StringBuilder();
        final StringBuilder args = new StringBuilder();
        JsonNode objectArgs;
        final Map<String, Object> extra = new LinkedHashMap<>();
    }

    /** content는 문자열, null, 또는 일부 서버에서 [{type:text, text}] 배열이다. */
    private static String text(JsonNode content) {
        if (content.isTextual()) {
            return content.asText();
        }
        if (content.isArray()) {
            StringBuilder sb = new StringBuilder();
            content.forEach(part -> sb.append(part.path("text").asText("")));
            return sb.toString();
        }
        return "";
    }

    /** arguments는 JSON 문자열이 표준이지만 객체로 주는 서버도 있다. 해석하지 못하면 빈 맵 (도구가 인자 오류로 답한다). */
    private Map<String, Object> arguments(JsonNode args) {
        try {
            if (args.isObject()) {
                return json.convertValue(args, MAP);
            }
            String s = args.asText("");
            if (s.isBlank()) {
                return Map.of();
            }
            Map<String, Object> parsed = json.readValue(s, MAP);
            return parsed == null ? Map.of() : parsed;
        } catch (JsonProcessingException | IllegalArgumentException e) {
            return Map.of();
        }
    }

    private String toJson(Map<String, Object> value) {
        try {
            return json.writeValueAsString(value == null ? Map.of() : value);
        } catch (JsonProcessingException e) {
            throw new IllegalStateException(e);
        }
    }
}
