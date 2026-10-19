package io.sensinghub.llm;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;

import io.sensinghub.llm.LlmException.Kind;

/**
 * Claude Messages API ({@code {ANTHROPIC_BASE_URL}/v1/messages}) + tools. 키는 워커와 같은 ANTHROPIC_API_KEY.
 * <ul>
 *   <li>도구 결과는 user 메시지의 tool_result 블록이다. 연달아 오는 결과와 그 뒤 사용자 글은 한 user 메시지로 합친다
 *       (tool_result 블록이 앞, 글이 뒤).</li>
 *   <li>allowTools=false면 tool_choice none: 대화에 tool_use가 있으면 도구 정의를 빼면 안 되므로 정의는 그대로 보낸다.</li>
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
        JsonNode data = http.post(baseUrl + "/v1/messages",
                Map.of("x-api-key", apiKey, "anthropic-version", VERSION), body);
        return parse(data);
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
}
