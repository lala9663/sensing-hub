package io.sensinghub.llm;

import static io.sensinghub.llm.MockLlmServer.vector;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;

import io.sensinghub.llm.LlmException.Kind;
import io.sensinghub.llm.MockLlmServer.Reply;

/** 질의 임베딩 클라이언트: 요청 형식, dimensions를 모르는 서버, 챗봇 LLM과 같은 오류 분류, 설정 규칙. 가짜 서버만 부른다. */
class OpenAiCompatEmbeddingClientTest {

    static MockLlmServer server;
    static final ObjectMapper JSON = new ObjectMapper();

    @BeforeAll
    static void start() {
        server = MockLlmServer.start();
    }

    @AfterAll
    static void stop() {
        server.close();
    }

    @BeforeEach
    void reset() {
        server.reset();
    }

    private OpenAiCompatEmbeddingClient client(String key) {
        return new OpenAiCompatEmbeddingClient(server.baseUrl() + "/v1/", "gemini-embedding-001", key, 1024, 5, 0, JSON);
    }

    @Test
    void sendsModelDimensionsAndKeyAndReadsTheVector() {
        server.then(Reply.ok(MockLlmServer.openAiEmbedding(vector(0, 0.6f, 3, 0.8f))));
        OpenAiCompatEmbeddingClient c = client("test-key");
        float[] v = c.embed("반도체 수출");

        assertThat(v).hasSize(1024);
        assertThat(v[0]).isEqualTo(0.6f);
        assertThat(v[3]).isEqualTo(0.8f);
        MockLlmServer.Recorded req = server.requests().get(0);
        assertThat(req.path()).isEqualTo("/v1/embeddings");
        assertThat(req.header("Authorization")).isEqualTo("Bearer test-key");
        JsonNode body = req.body();
        assertThat(body.path("model").asText()).isEqualTo("gemini-embedding-001");
        assertThat(body.path("input").get(0).asText()).isEqualTo("반도체 수출");
        assertThat(body.path("dimensions").asInt()).isEqualTo(1024);
        assertThat(body.path("encoding_format").asText()).isEqualTo("float");
        assertThat(c.identity()).isEqualTo("gemini-embedding-001/1024");
        assertThat(c.configured()).isTrue();
    }

    @Test
    void serverWithoutDimensionsIsRetriedOnceWithoutAndRemembered() {
        server.then(Reply.status(400, "{\"error\":{\"message\":\"Model does not support matryoshka representation\"}}"))
                .then(Reply.ok(MockLlmServer.openAiEmbedding(vector(1, 1))))
                .then(Reply.ok(MockLlmServer.openAiEmbedding(vector(2, 1))));
        OpenAiCompatEmbeddingClient c = client("");
        assertThat(c.embed("하나")[1]).isEqualTo(1f);
        assertThat(c.embed("둘")[2]).isEqualTo(1f);
        assertThat(server.requests()).extracting(r -> r.body().has("dimensions")).containsExactly(true, false, false);
        assertThat(server.requests().get(0).header("Authorization")).isNull();   // 키가 없으면 인증 헤더도 없다
    }

    @Test
    void errorsAreClassifiedLikeTheChatLlm() {
        // 429는 다시 보내지 않는다
        server.then(Reply.status(429, MockLlmServer.GEMINI_MINUTE_429));
        assertThatThrownBy(() -> client("k").embed("질의"))
                .isInstanceOfSatisfying(LlmException.class, e -> assertThat(e.kind()).isEqualTo(Kind.RATE_LIMITED));
        assertThat(server.requests()).hasSize(1);

        // 5xx는 한 번 다시 보낸다
        server.reset();
        server.then(Reply.status(503, "overloaded")).then(Reply.ok(MockLlmServer.openAiEmbedding(vector(0, 1))));
        assertThat(client("k").embed("질의")[0]).isEqualTo(1f);
        assertThat(server.requests()).hasSize(2);

        // 차원이 다르면 쓰지 않는다 (dimensions를 무시하는 모델)
        server.reset();
        server.then(Reply.ok(MockLlmServer.openAiEmbedding(new float[3072])));
        assertThatThrownBy(() -> client("k").embed("질의"))
                .isInstanceOfSatisfying(LlmException.class, e -> {
                    assertThat(e.kind()).isEqualTo(Kind.BAD_RESPONSE);
                    assertThat(e.getMessage()).contains("3072");
                });

        server.reset();
        server.then(Reply.ok("{\"data\":[]}"));
        assertThatThrownBy(() -> client("k").embed("질의"))
                .isInstanceOfSatisfying(LlmException.class, e -> assertThat(e.kind()).isEqualTo(Kind.BAD_RESPONSE));

        server.reset();
        server.then(Reply.status(401, "{\"error\":\"bad key\"}"));
        assertThatThrownBy(() -> client("k").embed("질의"))
                .isInstanceOfSatisfying(LlmException.class, e -> assertThat(e.kind()).isEqualTo(Kind.AUTH));
        assertThat(server.failures()).isEmpty();
    }

    @Test
    void configReusesLlmAddressAndKeyOnlyWithoutItsOwnAddress() {
        String llmUrl = "https://generativelanguage.googleapis.com/v1beta/openai";
        assertThat(EmbeddingConfig.create(JSON, "none", "m", "", "", 1024, llmUrl, "llm-key", 10, 0).configured()).isFalse();

        EmbeddingClient shared = EmbeddingConfig.create(JSON, "openai", "gemini-embedding-001", "", "", 1024, llmUrl,
                "llm-key", 10, 0);
        assertThat(shared.configured()).isTrue();
        assertThat(shared.identity()).isEqualTo("gemini-embedding-001/1024");
        assertThat(shared.describe()).isEqualTo("openai gemini-embedding-001/1024").doesNotContain("llm-key");

        // 주소를 따로 주면 LLM 키를 보내지 않는다
        server.then(Reply.ok(MockLlmServer.openAiEmbedding(vector(0, 1))))
                .then(Reply.ok(MockLlmServer.openAiEmbedding(vector(0, 1))));
        EmbeddingConfig.create(JSON, "openai", "bge-m3", server.baseUrl() + "/v1", "", 1024, llmUrl, "llm-key", 10, 0)
                .embed("a");
        EmbeddingConfig.create(JSON, "openai", "bge-m3", server.baseUrl() + "/v1", "embed-key", 1024, llmUrl, "llm-key", 10, 0)
                .embed("b");
        assertThat(server.requests()).extracting(r -> r.header("Authorization")).containsExactly(null, "Bearer embed-key");

        assertThat(((NotConfiguredEmbeddingClient) EmbeddingConfig.create(JSON, "openai", "", "", "", 1024, llmUrl, "k", 10, 0))
                .reason()).contains("SENSING_EMBED_MODEL");
        assertThat(((NotConfiguredEmbeddingClient) EmbeddingConfig.create(JSON, "openai", "m", "", "", 1024, "", "k", 10, 0))
                .reason()).contains("BASE_URL");
        assertThat(((NotConfiguredEmbeddingClient) EmbeddingConfig.create(JSON, "openai", "m", "", "", 768, llmUrl, "k", 10, 0))
                .reason()).contains("1024");
        assertThat(((NotConfiguredEmbeddingClient) EmbeddingConfig.create(JSON, "cohere", "m", "", "", 1024, llmUrl, "k", 10, 0))
                .reason()).contains("cohere");
        NotConfiguredEmbeddingClient none = new NotConfiguredEmbeddingClient();
        assertThat(none.identity()).isNull();
        assertThatThrownBy(() -> none.embed("x"))
                .isInstanceOfSatisfying(LlmException.class, e -> assertThat(e.kind()).isEqualTo(Kind.NOT_CONFIGURED));
    }
}
