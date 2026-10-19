package io.sensinghub.llm;

import java.util.ArrayList;
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
        Map<String, String> headers = apiKey.isEmpty() ? Map.of() : Map.of("Authorization", "Bearer " + apiKey);
        return parse(http.post(baseUrl + "/chat/completions", headers, body));
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
            Map<String, Object> extra = new LinkedHashMap<>();
            call.fields().forEachRemaining(e -> {
                if (!List.of("id", "type", "function", "index").contains(e.getKey())) {
                    extra.put(e.getKey(), json.convertValue(e.getValue(), Object.class));
                }
            });
            calls.add(new ToolCall(id, name, arguments(fn.path("arguments")), extra));
        }
        Map<String, Object> usage = new LinkedHashMap<>();
        JsonNode u = data.path("usage");
        if (u.isObject()) {
            usage.put("inputTokens", u.path("prompt_tokens").asInt(0));
            usage.put("outputTokens", u.path("completion_tokens").asInt(0));
        }
        return new Response(text, calls, usage, choice.path("finish_reason").asText(null));
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
