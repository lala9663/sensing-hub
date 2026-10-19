package io.sensinghub.llm;

import io.sensinghub.llm.LlmException.Kind;

/**
 * 임베딩 미설정 ({@code sensing.embed.provider=none}, 또는 모델·주소가 빠졌을 때). 검색은 이 경우 임베딩을 부르지 않고
 * 전문 검색만 한다.
 */
public class NotConfiguredEmbeddingClient implements EmbeddingClient {

    private final String reason;

    public NotConfiguredEmbeddingClient() {
        this(null);
    }

    /** @param reason 관리자에게 보일 빠진 설정 (예: "SENSING_EMBED_MODEL이 없습니다") */
    public NotConfiguredEmbeddingClient(String reason) {
        this.reason = reason;
    }

    @Override
    public float[] embed(String text) {
        throw new LlmException(Kind.NOT_CONFIGURED, "임베딩이 설정되지 않았습니다."
                + (reason == null ? "" : " (" + reason + ")"));
    }

    @Override
    public boolean configured() {
        return false;
    }

    @Override
    public String identity() {
        return null;
    }

    @Override
    public String describe() {
        return "none";
    }

    public String reason() {
        return reason;
    }
}
