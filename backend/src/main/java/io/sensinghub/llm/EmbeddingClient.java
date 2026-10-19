package io.sensinghub.llm;

/**
 * 의미 검색의 질의 임베딩 (챗봇 2단계). 문서 조각의 벡터는 워커(worker/src/sensing_worker/embed.py)가 같은 설정으로 만들어
 * {@code chunk.embedding}에 넣고 어떤 모델인지 {@code chunk.embed_model}에 남긴다. 검색은 {@link #identity()}가 같은 조각끼리만
 * 비교한다 — 다른 모델의 벡터는 비교할 수 없다.
 * <p>오류는 챗봇 LLM과 같은 분류의 {@link LlmException}으로 던진다 (429 한도·5xx·시간 초과·키 오류). 검색은 이를 받아
 * 전문 검색만으로 계속한다.
 */
public interface EmbeddingClient {

    /** 글 하나의 벡터 ({@code chunk.embedding}과 같은 차원). */
    float[] embed(String text);

    boolean configured();

    /** {@code chunk.embed_model}과 같은 형식 '모델/차원' (예: gemini-embedding-001/1024). 미설정이면 null. */
    String identity();

    /** 상태 표시·로그용 (키는 넣지 않는다). */
    String describe();
}
