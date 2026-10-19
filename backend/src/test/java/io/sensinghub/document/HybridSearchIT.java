package io.sensinghub.document;

import static io.sensinghub.llm.MockLlmServer.vector;
import static org.assertj.core.api.Assertions.assertThat;

import java.util.List;

import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;

import com.fasterxml.jackson.databind.ObjectMapper;

import io.sensinghub.auth.CurrentUser;
import io.sensinghub.auth.Role;
import io.sensinghub.document.HybridSearch.Hit;
import io.sensinghub.document.HybridSearch.Mode;
import io.sensinghub.document.HybridSearch.Result;
import io.sensinghub.llm.EmbeddingClient;
import io.sensinghub.llm.MockLlmServer;
import io.sensinghub.llm.MockLlmServer.Reply;
import io.sensinghub.llm.NotConfiguredEmbeddingClient;
import io.sensinghub.llm.OpenAiCompatEmbeddingClient;

/**
 * 하이브리드 검색 (챗봇 2단계): 전문 검색 + 의미 검색(조각 벡터) RRF, 조건(수집 시각), 모델이 다른 벡터 무시, 유사도 하한, 그리고
 * 임베딩이 없거나 실패할 때 전문 검색으로 그대로 동작하는지. 워커가 채우는 chunk.embedding은 행을 직접 넣어 흉내 내고, 질의 임베딩은
 * 가짜 서버({@link MockLlmServer})가 답한다 (실제 호출 없음). 실제 PostgreSQL(pgvector)이 필요하며 SENSING_IT_DB_URL이 있을 때만 실행된다.
 */
@EnabledIfEnvironmentVariable(named = "SENSING_IT_DB_URL", matches = "jdbc:postgresql:.+")
@SpringBootTest(properties = {"sensing.scheduler.enabled=false", "sensing.digest.enabled=false"})
class HybridSearchIT {

    static final MockLlmServer EMBED = MockLlmServer.start();
    static final ObjectMapper JSON = new ObjectMapper();
    static final String MODEL = "mock-embed";
    static final CurrentUser VIEWER = new CurrentUser(3L, "viewer", "일반 사용자", Role.VIEWER);

    @DynamicPropertySource
    static void db(DynamicPropertyRegistry r) {
        r.add("spring.datasource.url", () -> System.getenv("SENSING_IT_DB_URL"));
        r.add("spring.flyway.clean-disabled", () -> "false");
    }

    @AfterAll
    static void stop() {
        EMBED.close();
    }

    @Autowired DocumentService documents;
    @Autowired JdbcClient jdbc;

    long press;
    long gov;

    @BeforeEach
    void clean() {
        EMBED.reset();
        jdbc.sql("""
                TRUNCATE chat_feedback, chat_citation, chat_message, chat_session, chunk, attachment, issue_document, issue,
                         document_version, document_watch, document_enrichment, document_company, company_signal, company,
                         document, crawl_error, crawl_job, registration_request, audit_log, api_credential
                RESTART IDENTITY CASCADE
                """).update();
        jdbc.sql("UPDATE source SET active_config_version_id = NULL").update();
        jdbc.sql("TRUNCATE source_config_version, source, site RESTART IDENTITY CASCADE").update();
        press = siteSource("news.example.com", "NEWS");
        gov = siteSource("gov.example.go.kr", "PUBLIC");
    }

    @AfterEach
    void scriptedRepliesPassed() {
        assertThat(EMBED.failures()).isEmpty();
    }

    private long siteSource(String domain, String siteType) {
        long siteId = jdbc.sql("INSERT INTO site (domain, base_url, site_type, name) VALUES (?, ?, ?, ?) RETURNING id")
                .params(domain, "https://" + domain, siteType, domain).query(Long.class).single();
        return jdbc.sql("INSERT INTO source (site_id, name, status) VALUES (?, ?, 'ACTIVE') RETURNING id")
                .params(siteId, domain).query(Long.class).single();
    }

    /** 색인 전 문서(전문 검색은 제목 부분 일치)를 daysAgo일 전에 수집·발행한 것으로 넣는다. */
    private long document(long sourceId, String url, String title, int daysAgo) {
        return jdbc.sql("""
                INSERT INTO document (source_id, url, url_hash, title, published_at, collected_at)
                VALUES (?, ?, md5(?) || md5(?), ?, now() - make_interval(days => ?), now() - make_interval(days => ?))
                RETURNING id
                """).params(sourceId, url, url, url, title, daysAgo, daysAgo).query(Long.class).single();
    }

    /** 워커가 임베딩한 조각을 흉내 낸다. */
    private void chunk(long documentId, int seq, String text, float[] embedding, String model) {
        jdbc.sql("""
                INSERT INTO chunk (document_id, seq, text, embedding, index_version, embed_model, embedded_at)
                VALUES (?, ?, ?, CAST(? AS vector), 'fts-kiwi-v1', ?, now())
                """).params(documentId, seq, text, DocumentService.vectorLiteral(embedding), model + "/1024").update();
    }

    private HybridSearch search(EmbeddingClient client) {
        HybridSearch s = new HybridSearch(documents, client, 50, 60, 0.5, 60);
        s.clearPause();
        return s;
    }

    private EmbeddingClient embedder(String model) {
        return new OpenAiCompatEmbeddingClient(EMBED.baseUrl() + "/v1", model, "k", 1024, 5, 0, JSON);
    }

    private static List<String> titles(Result r) {
        return r.hits().stream().map(h -> h.row().title()).toList();
    }

    private static List<String> matches(Result r) {
        return r.hits().stream().map(h -> h.words() && h.meaning() ? "both" : h.words() ? "words" : "meaning").toList();
    }

    /** 질의 벡터 = 칸 0. 문서 벡터의 칸 0 값이 곧 코사인 유사도다 (단위 벡터). */
    private void corpus() {
        long a = document(press, "https://news.example.com/a", "반도체 수출 사상 최대", 1);
        chunk(a, 0, "반도체 수출 사상 최대", vector(5, 1), MODEL);                                  // 낱말만 (유사도 0)
        long b = document(gov, "https://gov.example.go.kr/b", "반도체 장비 국산화 지원사업 공고", 2);
        chunk(b, 0, "반도체 장비 국산화 지원사업 공고", vector(6, 1), MODEL);
        chunk(b, 1, "본문 조각", vector(0, 0.8f, 1, 0.6f), MODEL);                              // 낱말 + 본문 조각 0.8
        long f = document(press, "https://news.example.com/f", "반도체 업황 전망", 10);
        chunk(f, 0, "반도체 업황 전망", vector(7, 1), MODEL);                                       // 낱말만, 10일 전
        long c = document(press, "https://news.example.com/c", "안세영, 덴마크오픈 우승", 1);
        chunk(c, 0, "안세영, 덴마크오픈 우승", vector(0, 0.9f, 2, 0.43589f), MODEL);                // 의미만 0.9
        long g = document(press, "https://news.example.com/g", "AI 칩 공급망 재편", 10);
        chunk(g, 0, "AI 칩 공급망 재편", vector(0, 0.95f, 2, 0.31225f), MODEL);                     // 의미만 0.95, 10일 전
        long d = document(press, "https://news.example.com/d", "가을 축제 개막", 1);
        chunk(d, 0, "가을 축제 개막", vector(0, 0.3f, 3, 0.95394f), MODEL);                        // 하한(0.5) 아래
        long e = document(press, "https://news.example.com/e", "다른 모델로 만든 벡터", 1);
        chunk(e, 0, "다른 모델로 만든 벡터", vector(0, 1), "old-model");                             // 다른 모델: 비교 안 함
    }

    @Test
    void wordsAndMeaningAreFusedWithRrfAndFiltersApplyToBoth() {
        corpus();
        EMBED.then(Reply.ok(MockLlmServer.openAiEmbedding(vector(0, 1))))
                .then(Reply.ok(MockLlmServer.openAiEmbedding(vector(0, 1))))
                .then(Reply.ok(MockLlmServer.openAiEmbedding(vector(0, 1))));
        HybridSearch s = search(embedder(MODEL));

        // 낱말(최신순): a, b, f   의미(유사도순): g 0.95, c 0.9, b 0.8   → b(양쪽), a·g(1위 동점, 낱말 먼저), c, f
        Result all = s.search(VIEWER, null, null, null, "반도체", 10);
        assertThat(all.mode()).isEqualTo(Mode.HYBRID);
        assertThat(titles(all)).containsExactly("반도체 장비 국산화 지원사업 공고", "반도체 수출 사상 최대", "AI 칩 공급망 재편",
                "안세영, 덴마크오픈 우승", "반도체 업황 전망");
        assertThat(matches(all)).containsExactly("both", "words", "meaning", "meaning", "words");
        assertThat(EMBED.requests()).hasSize(1);
        assertThat(EMBED.requests().get(0).path()).isEqualTo("/v1/embeddings");
        assertThat(EMBED.requests().get(0).body().path("input").get(0).asText()).isEqualTo("반도체");
        assertThat(EMBED.requests().get(0).body().path("model").asText()).isEqualTo(MODEL);

        // 수집 시각 조건은 의미 후보에도 걸린다 (10일 전 g·f 제외)
        Result recent = s.search(VIEWER, java.time.OffsetDateTime.now().minusDays(5).toString(), null, null, "반도체", 10);
        assertThat(titles(recent)).containsExactly("반도체 장비 국산화 지원사업 공고", "반도체 수출 사상 최대", "안세영, 덴마크오픈 우승");

        Result top2 = s.search(VIEWER, null, null, null, "반도체", 2);
        assertThat(titles(top2)).containsExactly("반도체 장비 국산화 지원사업 공고", "반도체 수출 사상 최대");

        // 검색어가 없으면 임베딩 없이 최신 문서
        Result latest = s.search(VIEWER, null, null, null, "  ", 3);
        assertThat(latest.mode()).isEqualTo(Mode.LATEST);
        assertThat(latest.hits()).hasSize(3).noneMatch(Hit::words);
        assertThat(EMBED.requests()).hasSize(3);
    }

    @Test
    void withoutEmbeddingsItIsTheSameFullTextSearchAsBefore() {
        corpus();
        List<String> fullText = documents.list(VIEWER, null, null, null, null, null, "반도체", 10).stream()
                .map(DocumentService.DocumentRow::title).toList();
        assertThat(fullText).containsExactly("반도체 수출 사상 최대", "반도체 장비 국산화 지원사업 공고", "반도체 업황 전망");

        // 임베딩 미설정
        Result off = search(new NotConfiguredEmbeddingClient()).search(VIEWER, null, null, null, "반도체", 10);
        assertThat(off.mode()).isEqualTo(Mode.FULLTEXT);
        assertThat(titles(off)).isEqualTo(fullText);
        assertThat(off.hits()).allMatch(h -> h.words() && !h.meaning());

        // 설정은 있지만 그 모델로 임베딩된 조각이 아직 없다 → 질의를 임베딩하지 않는다
        Result notYet = search(embedder("new-model")).search(VIEWER, null, null, null, "반도체", 10);
        assertThat(notYet.mode()).isEqualTo(Mode.FULLTEXT);
        assertThat(notYet.note()).contains("new-model/1024");
        assertThat(titles(notYet)).isEqualTo(fullText);
        assertThat(EMBED.requests()).isEmpty();
    }

    @Test
    void embeddingFailureFallsBackToFullTextAndPausesSemanticSearch() {
        corpus();
        HybridSearch s = search(embedder(MODEL));
        List<String> fullText = List.of("반도체 수출 사상 최대", "반도체 장비 국산화 지원사업 공고", "반도체 업황 전망");

        // 429(하루 한도): 다시 보내지 않고 전문 검색으로 답한다
        EMBED.then(Reply.status(429, MockLlmServer.GEMINI_DAILY_429));
        Result limited = s.search(VIEWER, null, null, null, "반도체", 10);
        assertThat(limited.mode()).isEqualTo(Mode.FULLTEXT);
        assertThat(limited.note()).contains("RATE_LIMITED");
        assertThat(titles(limited)).isEqualTo(fullText);
        assertThat(EMBED.requests()).hasSize(1);

        // 멈춘 동안은 부르지도 않는다
        Result paused = s.search(VIEWER, null, null, null, "반도체", 10);
        assertThat(paused.mode()).isEqualTo(Mode.FULLTEXT);
        assertThat(EMBED.requests()).hasSize(1);

        // 5xx는 한 번 다시 보내고, 그래도 실패하면 전문 검색
        s.clearPause();
        EMBED.then(Reply.status(500, "boom")).then(Reply.status(503, "boom"));
        Result down = s.search(VIEWER, null, null, null, "반도체", 10);
        assertThat(down.mode()).isEqualTo(Mode.FULLTEXT);
        assertThat(titles(down)).isEqualTo(fullText);
        assertThat(EMBED.requests()).hasSize(3);

        // 차원이 다른 응답도 쓰지 않는다
        s.clearPause();
        EMBED.then(Reply.ok(MockLlmServer.openAiEmbedding(new float[768])));
        assertThat(s.search(VIEWER, null, null, null, "반도체", 10).mode()).isEqualTo(Mode.FULLTEXT);

        // 멈춤이 풀리면 다시 하이브리드
        s.clearPause();
        EMBED.then(Reply.ok(MockLlmServer.openAiEmbedding(vector(0, 1))));
        assertThat(s.search(VIEWER, null, null, null, "반도체", 10).mode()).isEqualTo(Mode.HYBRID);
        assertThat(EMBED.pending()).isZero();
    }
}
