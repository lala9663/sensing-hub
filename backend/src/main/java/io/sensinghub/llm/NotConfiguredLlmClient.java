package io.sensinghub.llm;

import java.util.List;

import io.sensinghub.llm.LlmException.Kind;

/**
 * LLM 연동 전 기본 구현 ({@code sensing.llm.provider=none}, 또는 제공자는 골랐지만 키·주소가 빠졌을 때).
 * 호출하면 "LLM이 설정되지 않았습니다"를 사용자 문장으로 던진다. 챗봇은 이를 답 대신 안내로 보여 준다.
 */
public class NotConfiguredLlmClient implements LlmClient {

    private final String reason;

    public NotConfiguredLlmClient() {
        this(null);
    }

    /** @param reason 관리자에게 보일 빠진 설정 (예: "SENSING_LLM_BASE_URL이 없습니다") */
    public NotConfiguredLlmClient(String reason) {
        this.reason = reason;
    }

    @Override
    public Response chat(String system, List<Message> messages, List<Tool> tools, boolean allowTools) {
        throw new LlmException(Kind.NOT_CONFIGURED, "LLM이 설정되지 않았습니다. 관리자에게 SENSING_LLM_PROVIDER 설정을 요청해 주세요."
                + (reason == null ? "" : " (" + reason + ")"));
    }

    @Override
    public boolean configured() {
        return false;
    }

    @Override
    public String describe() {
        return "none";
    }

    public String reason() {
        return reason;
    }
}
