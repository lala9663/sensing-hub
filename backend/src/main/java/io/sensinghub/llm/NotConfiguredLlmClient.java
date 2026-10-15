package io.sensinghub.llm;

import java.util.List;
import java.util.Map;

import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.stereotype.Component;

/**
 * LLM 연동 전 기본 구현 ({@code sensing.llm.provider=none}).
 * 사내 게이트웨이나 외부 API 구현체를 추가하면 provider 값으로 고른다.
 */
@Component
@ConditionalOnProperty(name = "sensing.llm.provider", havingValue = "none", matchIfMissing = true)
public class NotConfiguredLlmClient implements LlmClient {

    @Override
    public Response chat(String system, List<Message> messages, List<Tool> tools) {
        return new Response("LLM이 아직 연결되지 않았습니다. 관리자에게 문의해 주세요.", List.of(), Map.of());
    }
}
