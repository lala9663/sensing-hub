package io.sensinghub.llm;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;

import io.sensinghub.llm.LlmException.Kind;

/**
 * OpenAI 호환 {@code {base_url}/embeddings}. 사내 vLLM·Ollama, OpenAI, Gemini의 OpenAI 호환 주소에 쓴다. 워커
 * embed.OpenAiCompatEmbedder와 같은 요청을 보낸다.
 * <ul>
 *   <li>{@code dimensions}를 보내 {@code chunk.embedding}(1024차원)에 맞춘다 (Gemini·OpenAI text-embedding-3는 줄여 준다).
 *       이를 모르는 서버가 400을 내면 한 번 빼고 다시 보내고 기억한다 (그때는 모델 자체가 1024차원이어야 한다).</li>
 *   <li>Gemini 응답은 첫 항목의 {@code index}(0)를 빼고 준다. 질의는 하나만 보내므로 첫 항목을 쓴다.</li>
 *   <li>HTTP 오류 분류(429 재시도 없음, 5xx 한 번 재시도, 시간 초과)는 챗봇 LLM과 같은 {@link LlmHttp}가 한다.</li>
 * </ul>
 */
public class OpenAiCompatEmbeddingClient implements EmbeddingClient {

    private static final Logger log = LoggerFactory.getLogger(OpenAiCompatEmbeddingClient.class);

    private final String baseUrl;
    private final String model;
    private final String apiKey;
    private final int dimensions;
    private final LlmHttp http;
    private volatile boolean sendDimensions = true;

    public OpenAiCompatEmbeddingClient(String baseUrl, String model, String apiKey, int dimensions, int timeoutSeconds,
                                       long retryDelayMillis, ObjectMapper json) {
        this.baseUrl = baseUrl.replaceAll("/+$", "");
        this.model = model;
        this.apiKey = apiKey == null ? "" : apiKey.strip();
        this.dimensions = dimensions;
        this.http = new LlmHttp(json, timeoutSeconds, retryDelayMillis);
    }

    @Override
    public boolean configured() {
        return true;
    }

    @Override
    public String identity() {
        return model + "/" + dimensions;
    }

    @Override
    public String describe() {
        return "openai " + identity();
    }

    @Override
    public float[] embed(String text) {
        Map<String, String> headers = apiKey.isEmpty() ? Map.of() : Map.of("Authorization", "Bearer " + apiKey);
        boolean withDimensions = sendDimensions;
        JsonNode response;
        try {
            response = http.post(baseUrl + "/embeddings", headers, body(text, withDimensions));
        } catch (LlmException e) {
            if (!withDimensions || e.httpStatus() == null || e.httpStatus() != 400) {
                throw e;
            }
            log.info("embedding server rejected dimensions={}, retrying without: {}", dimensions, e.detail());
            sendDimensions = false;
            response = http.post(baseUrl + "/embeddings", headers, body(text, false));
        }
        return parse(response);
    }

    private Map<String, Object> body(String text, boolean withDimensions) {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("model", model);
        body.put("input", List.of(text));
        body.put("encoding_format", "float");
        if (withDimensions) {
            body.put("dimensions", dimensions);
        }
        return body;
    }

    private float[] parse(JsonNode response) {
        JsonNode values = response.path("data").path(0).path("embedding");
        if (!values.isArray() || values.isEmpty()) {
            throw new LlmException(Kind.BAD_RESPONSE, "임베딩 응답을 해석하지 못했습니다.", null,
                    LlmHttp.head(response.toString()));
        }
        if (values.size() != dimensions) {
            throw new LlmException(Kind.BAD_RESPONSE, "임베딩 차원이 " + values.size() + "입니다 (" + dimensions
                    + "이어야 합니다).", null, "model " + model);
        }
        float[] out = new float[values.size()];
        for (int i = 0; i < out.length; i++) {
            out[i] = (float) values.get(i).asDouble();
        }
        return out;
    }
}
