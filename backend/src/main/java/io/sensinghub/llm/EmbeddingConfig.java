package io.sensinghub.llm;

import java.util.Locale;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

import com.fasterxml.jackson.databind.ObjectMapper;

/**
 * 의미 검색 임베딩 제공자 선택. 워커(worker/src/sensing_worker/embed.py make_embedder)와 같은 환경 변수와 같은 규칙을 쓴다:
 * <ul>
 *   <li>{@code SENSING_EMBED_PROVIDER}: none(기본) | openai(OpenAI 호환 /embeddings, Gemini OpenAI 호환 주소 포함)</li>
 *   <li>{@code SENSING_EMBED_MODEL}(필수), {@code SENSING_EMBED_DIMENSIONS}(1024 — chunk.embedding 열 크기)</li>
 *   <li>{@code SENSING_EMBED_BASE_URL}·{@code SENSING_EMBED_API_KEY}: 주소를 비우면 챗봇 LLM의 주소와 키
 *       ({@code SENSING_LLM_BASE_URL}·{@code SENSING_LLM_API_KEY})를 함께 쓴다. 주소를 따로 주면 키도 따로 준다 —
 *       LLM 키를 다른 서버에 보내지 않기 위해서다.</li>
 * </ul>
 * 설정이 모자라면 기동을 막지 않고 {@link NotConfiguredEmbeddingClient}로 둔다 (검색은 전문 검색만 한다).
 */
@Configuration
public class EmbeddingConfig {

    /** chunk.embedding VECTOR(1024) (V1) */
    public static final int COLUMN_DIMENSIONS = 1024;
    private static final Logger log = LoggerFactory.getLogger(EmbeddingConfig.class);

    @Bean
    public EmbeddingClient embeddingClient(ObjectMapper json,
                                           @Value("${sensing.embed.provider:none}") String provider,
                                           @Value("${sensing.embed.model:}") String model,
                                           @Value("${sensing.embed.base-url:}") String baseUrl,
                                           @Value("${sensing.embed.api-key:}") String apiKey,
                                           @Value("${sensing.embed.dimensions:1024}") int dimensions,
                                           @Value("${sensing.llm.base-url:}") String llmBaseUrl,
                                           @Value("${sensing.llm.api-key:}") String llmApiKey,
                                           @Value("${sensing.embed.timeout-seconds:10}") String timeout,
                                           @Value("${sensing.llm.retry-delay-ms:1500}") long retryDelayMillis) {
        EmbeddingClient client = create(json, provider, model, baseUrl, apiKey, dimensions, llmBaseUrl, llmApiKey,
                LlmConfig.seconds(timeout), retryDelayMillis);
        if (client instanceof NotConfiguredEmbeddingClient nc) {
            log.info("search embedding not configured{} — full-text search only",
                    nc.reason() == null ? "" : ": " + nc.reason());
        } else {
            log.info("search embedding: {}", client.describe());
        }
        return client;
    }

    static EmbeddingClient create(ObjectMapper json, String provider, String model, String baseUrl, String apiKey,
                                  int dimensions, String llmBaseUrl, String llmApiKey, int timeoutSeconds,
                                  long retryDelayMillis) {
        String p = provider == null ? "" : provider.strip().toLowerCase(Locale.ROOT);
        return switch (p) {
            case "", "none", "off", "false" -> new NotConfiguredEmbeddingClient();
            case "openai", "openai-compat", "openai_compat" -> {
                boolean ownUrl = !isBlank(baseUrl);
                String url = ownUrl ? baseUrl.strip() : llmBaseUrl;
                String key = !isBlank(apiKey) ? apiKey : ownUrl ? "" : llmApiKey;
                if (isBlank(url)) {
                    yield new NotConfiguredEmbeddingClient("SENSING_EMBED_BASE_URL(또는 SENSING_LLM_BASE_URL)이 없습니다");
                }
                if (isBlank(model)) {
                    yield new NotConfiguredEmbeddingClient("SENSING_EMBED_MODEL이 없습니다");
                }
                if (dimensions != COLUMN_DIMENSIONS) {
                    yield new NotConfiguredEmbeddingClient("SENSING_EMBED_DIMENSIONS는 " + COLUMN_DIMENSIONS
                            + "이어야 합니다 (chunk.embedding 열 크기)");
                }
                yield new OpenAiCompatEmbeddingClient(url.strip(), model.strip(), key, dimensions, timeoutSeconds,
                        retryDelayMillis, json);
            }
            default -> new NotConfiguredEmbeddingClient("알 수 없는 SENSING_EMBED_PROVIDER: " + provider.strip());
        };
    }

    private static boolean isBlank(String s) {
        return s == null || s.isBlank();
    }
}
