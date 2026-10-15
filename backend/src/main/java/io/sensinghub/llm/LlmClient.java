package io.sensinghub.llm;

import java.util.List;
import java.util.Map;

/**
 * LLM 호출 추상화. 사내 LLM 게이트웨이와 외부 API(PoC용) 구현체를 설정으로 바꾼다.
 * 챗봇(M3)은 이 인터페이스만 사용한다.
 */
public interface LlmClient {

    record Message(String role, String content) {
    }

    /** 챗봇이 쓸 수 있는 도구. 예: search_documents, register_source, get_source_status */
    record Tool(String name, String description, Map<String, Object> inputSchema) {
    }

    record ToolCall(String name, Map<String, Object> arguments) {
    }

    record Response(String text, List<ToolCall> toolCalls, Map<String, Object> usage) {
    }

    Response chat(String system, List<Message> messages, List<Tool> tools);
}
