package io.sensinghub.llm;

import java.util.List;
import java.util.Map;

/**
 * LLM 호출 추상화. 사내 LLM 게이트웨이와 외부 API(PoC용) 구현체를 설정({@code sensing.llm.provider})으로 바꾼다
 * ({@link LlmConfig}). 챗봇은 이 인터페이스만 사용한다.
 * <ul>
 *   <li>대화는 USER → (ASSISTANT 도구 호출 → TOOL 결과)* → ASSISTANT 답 순서다. 도구 결과는 호출 id로 짝을 맞춘다.</li>
 *   <li>실패(미설정·한도 초과·서버 오류·시간 초과)는 {@link LlmException}으로 던지고, 메시지는 사용자에게 그대로 보일 문장이다.</li>
 * </ul>
 */
public interface LlmClient {

    String USER = "user";
    String ASSISTANT = "assistant";
    String TOOL = "tool";

    /**
     * role: user | assistant | tool.
     * assistant는 toolCalls를 가질 수 있고, tool은 toolCallId·toolName으로 어느 호출의 결과인지 밝힌다.
     */
    record Message(String role, String content, List<ToolCall> toolCalls, String toolCallId, String toolName) {

        public Message(String role, String content) {
            this(role, content, List.of(), null, null);
        }

        public static Message user(String content) {
            return new Message(USER, content);
        }

        public static Message assistant(String content, List<ToolCall> toolCalls) {
            return new Message(ASSISTANT, content, toolCalls == null ? List.of() : toolCalls, null, null);
        }

        public static Message tool(String toolCallId, String toolName, String content) {
            return new Message(TOOL, content, List.of(), toolCallId, toolName);
        }
    }

    /** 챗봇이 쓸 수 있는 도구. 예: search_documents, register_source, get_source_status */
    record Tool(String name, String description, Map<String, Object> inputSchema) {
    }

    /**
     * @param providerData 제공자가 도구 호출에 붙인 불투명한 값. 다음 요청에 그대로 돌려줘야 한다
     *                     (예: Gemini OpenAI 호환 주소의 {@code extra_content.google.thought_signature}). 없으면 빈 맵
     */
    record ToolCall(String id, String name, Map<String, Object> arguments, Map<String, Object> providerData) {

        public ToolCall(String id, String name, Map<String, Object> arguments) {
            this(id, name, arguments, Map.of());
        }
    }

    /** usage: inputTokens·outputTokens (제공자가 주면). finishReason: 제공자 값 그대로 (stop, tool_calls, end_turn ...) */
    record Response(String text, List<ToolCall> toolCalls, Map<String, Object> usage, String finishReason) {

        public Response(String text, List<ToolCall> toolCalls, Map<String, Object> usage) {
            this(text, toolCalls, usage, null);
        }
    }

    /**
     * @param allowTools false면 도구 정의는 보내되(대화에 이미 도구 호출이 있으면 정의가 필요하다) 이번에는 부르지 말고
     *                   글로 답하게 한다 (도구 호출 한도에 닿았을 때)
     */
    Response chat(String system, List<Message> messages, List<Tool> tools, boolean allowTools);

    default Response chat(String system, List<Message> messages, List<Tool> tools) {
        return chat(system, messages, tools, true);
    }

    /** 설정돼 있어 실제로 부를 수 있는가 (화면 안내용). */
    default boolean configured() {
        return true;
    }

    /** 화면·대화 기록용 "제공자 모델" (비밀 없음). */
    default String describe() {
        return getClass().getSimpleName();
    }
}
