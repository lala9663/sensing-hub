package io.sensinghub;

import static org.assertj.core.api.Assertions.assertThat;

import java.util.HashSet;
import java.util.List;
import java.util.Set;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.web.client.TestRestTemplate;
import org.springframework.http.HttpEntity;
import org.springframework.http.HttpHeaders;
import org.springframework.http.HttpMethod;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;

import com.fasterxml.jackson.databind.JsonNode;

/**
 * 전문 검색(/documents?q=)과 이슈 API (M5 기반). 워커가 하는 색인(chunk.tokens)과 이슈 묶기는 행을 직접 넣어 흉내 낸다.
 * 실제 PostgreSQL이 필요하며 SENSING_IT_DB_URL이 있을 때만 실행된다.
 */
@EnabledIfEnvironmentVariable(named = "SENSING_IT_DB_URL", matches = "jdbc:postgresql:.+")
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT,
        properties = {"sensing.scheduler.enabled=false", "sensing.digest.enabled=false"})
class SearchIssueIT {

    @DynamicPropertySource
    static void db(DynamicPropertyRegistry r) {
        r.add("spring.datasource.url", () -> System.getenv("SENSING_IT_DB_URL"));
        r.add("spring.flyway.clean-disabled", () -> "false");
    }

    @Autowired TestRestTemplate http;
    @Autowired JdbcClient jdbc;

    @BeforeEach
    void clean() {
        jdbc.sql("""
                TRUNCATE chat_feedback, chat_citation, chat_message, chat_session, chunk, attachment, issue_document, issue,
                         document_version, document_watch, document_enrichment, document_company, company_signal, company,
                         document, crawl_error, crawl_job, registration_request, audit_log, api_credential
                RESTART IDENTITY CASCADE
                """).update();
        jdbc.sql("UPDATE source SET active_config_version_id = NULL").update();
        jdbc.sql("TRUNCATE source_config_version, source, site RESTART IDENTITY CASCADE").update();
    }

    private ResponseEntity<JsonNode> call(String user, String path) {
        HttpHeaders h = new HttpHeaders();
        h.set("X-User", user);
        return http.exchange(path, HttpMethod.GET, new HttpEntity<>(h), JsonNode.class);
    }

    private JsonNode get(String path) {
        ResponseEntity<JsonNode> res = call("viewer", path);
        assertThat(res.getStatusCode()).isEqualTo(HttpStatus.OK);
        return res.getBody();
    }

    private Set<Long> ids(JsonNode list) {
        Set<Long> out = new HashSet<>();
        list.forEach(n -> out.add(n.get("id").asLong()));
        return out;
    }

    private long siteSource(String domain, String siteType, String name) {
        long siteId = jdbc.sql("INSERT INTO site (domain, base_url, site_type, name) VALUES (?, ?, ?, ?) RETURNING id")
                .params(domain, "https://" + domain, siteType, name).query(Long.class).single();
        return jdbc.sql("INSERT INTO source (site_id, name, status) VALUES (?, ?, 'ACTIVE') RETURNING id")
                .params(siteId, domain).query(Long.class).single();
    }

    private long document(long sourceId, String url, String title, String publishedAt, String body, boolean indexed) {
        return jdbc.sql("""
                INSERT INTO document (source_id, url, url_hash, title, published_at, body_text, indexed_at)
                VALUES (?, ?, md5(?) || md5(?), ?, CAST(? AS timestamptz), ?, CASE WHEN ? THEN now() END) RETURNING id
                """).params(sourceId, url, url, url, title, publishedAt, body, indexed).query(Long.class).single();
    }

    /** 워커가 넣는 것과 같은 모양: 미리 뽑은 낱말을 공백으로 이어 to_tsvector('simple', ...). */
    private void chunk(long documentId, int seq, String text, String terms) {
        jdbc.sql("""
                INSERT INTO chunk (document_id, seq, text, tokens, index_version)
                VALUES (?, ?, ?, to_tsvector('simple', ?), 'fts-kiwi-v1')
                """).params(documentId, seq, text, terms).update();
    }

    private long issue(String title, int docCount, int sourceCount, String firstSeen, String lastSeen, String topTerms) {
        return jdbc.sql("""
                INSERT INTO issue (title, first_seen_at, last_seen_at, doc_count, source_count, top_terms)
                VALUES (?, CAST(? AS timestamptz), CAST(? AS timestamptz), ?, ?, CAST(? AS jsonb)) RETURNING id
                """).params(title, firstSeen, lastSeen, docCount, sourceCount, topTerms).query(Long.class).single();
    }

    private void member(long issueId, long documentId, double similarity) {
        jdbc.sql("INSERT INTO issue_document (issue_id, document_id, similarity, terms) VALUES (?, ?, ?, '[]'::jsonb)")
                .params(issueId, documentId, similarity).update();
    }

    @Test
    void documentSearchUsesFullTextIndexAndFallsBackForUnindexedDocuments() {
        long press = siteSource("news.example.com", "NEWS", "예시일보");
        long gov = siteSource("gov.example.go.kr", "PUBLIC", null);
        long title = document(press, "https://news.example.com/1", "반도체 수출 사상 최대", "2026-10-16T09:00:00+09:00",
                null, true);
        chunk(title, 0, "반도체 수출 사상 최대", "반도체 수출 사상 최대");
        long body = document(gov, "https://gov.example.go.kr/1", "소부장 지원사업 공고", "2026-10-15T09:00:00+09:00",
                "반도체 장비 국산화를 지원합니다.", true);
        chunk(body, 0, "소부장 지원사업 공고", "소부장 지원사업 공고");
        chunk(body, 1, "반도체 장비 국산화를 지원합니다.", "반도체 장비 국산화 지원");
        long unindexed = document(press, "https://news.example.com/2", "반도체가 이끈 3분기 실적", "2026-10-14T09:00:00+09:00",
                null, false);
        // 색인된 문서는 색인 낱말로만 찾는다: 제목에 '반도체' 글자가 있어도 낱말은 '비반도체'라 접두 일치하지 않는다
        long other = document(press, "https://news.example.com/3", "비반도체 부문 약진", "2026-10-13T09:00:00+09:00",
                null, true);
        chunk(other, 0, "비반도체 부문 약진", "비반도체 부문 약진");

        // 조사를 뗀 질의, 제목·본문 조각, 아직 색인 전 문서(부분 일치)
        assertThat(ids(get("/api/v1/documents?q=반도체가"))).containsExactlyInAnyOrder(title, body, unindexed);
        assertThat(ids(get("/api/v1/documents?q=반도체를"))).containsExactlyInAnyOrder(title, body, unindexed);
        // 모든 낱말이 있어야 한다 (조각이 달라도 된다: 제목의 '지원사업' + 본문의 '장비')
        assertThat(ids(get("/api/v1/documents?q=반도체 수출"))).containsExactly(title);
        assertThat(ids(get("/api/v1/documents?q=지원사업, 장비"))).containsExactly(body);
        assertThat(ids(get("/api/v1/documents?q=3분기 실적"))).containsExactly(unindexed);
        // 접두 일치
        assertThat(ids(get("/api/v1/documents?q=국산"))).containsExactly(body);
        assertThat(ids(get("/api/v1/documents?q=비반도"))).containsExactly(other);
        assertThat(get("/api/v1/documents?q=없는낱말")).isEmpty();
        // 글자·숫자가 없는 질의는 통째로 부분 일치 (특수문자가 tsquery를 깨지 않는다)
        assertThat(get("/api/v1/documents?q=:*!()")).isEmpty();
        // 다른 필터와 함께
        assertThat(ids(get("/api/v1/documents?q=반도체&source_id=" + gov))).containsExactly(body);
    }

    @Test
    void issuesListSortsByOutletsThenDocumentsAndFilters() {
        long a = siteSource("a.example.com", "NEWS", "가일보");
        long b = siteSource("b.example.com", "NEWS", null);
        long first = document(a, "https://a.example.com/1", "반도체 수출 사상 최대", "2026-10-16T09:00:00+09:00", null, true);
        long earlier = document(b, "https://b.example.com/1", "반도체 수출 역대 최대 기록", "2026-10-16T07:00:00+09:00", null, true);
        long single = document(b, "https://b.example.com/2", "단독 기사 하나", "2026-10-16T08:00:00+09:00", null, true);
        long semis = issue("반도체 수출 사상 최대", 4, 3, "2026-10-16T07:00:00+09:00", "2026-10-16T10:00:00+09:00",
                "[\"반도체\", \"수출\", \"최대\"]");
        long busy = issue("국무회의 생중계 논란", 5, 3, "2026-10-15T07:00:00+09:00", "2026-10-15T20:00:00+09:00",
                "[\"국무\", \"회의\", \"생중계\"]");
        long wide = issue("다카이치 야스쿠니 공물 봉납", 5, 5, "2026-10-14T07:00:00+09:00", "2026-10-14T20:00:00+09:00",
                "[\"다카이치\", \"야스쿠니\", \"봉납\"]");
        long lone = issue("단독 기사 하나", 1, 1, "2026-10-16T08:00:00+09:00", "2026-10-16T08:00:00+09:00",
                "[\"단독\", \"기사\"]");
        member(semis, first, 0.8);
        member(semis, earlier, 1.0);
        member(lone, single, 1.0);

        // 정렬: 언론사 수 → 기사 수 → 마지막 시각. 기사 하나짜리는 기본(min_docs=2)에서 빠진다
        JsonNode all = get("/api/v1/issues");
        assertThat(all.findValuesAsText("id")).containsExactly(String.valueOf(wide), String.valueOf(busy),
                String.valueOf(semis));
        JsonNode top = all.get(0);
        assertThat(top.get("title").asText()).isEqualTo("다카이치 야스쿠니 공물 봉납");
        assertThat(top.get("sourceCount").asInt()).isEqualTo(5);
        assertThat(top.get("docCount").asInt()).isEqualTo(5);
        assertThat(top.get("topTerms").toString()).isEqualTo("[\"다카이치\",\"야스쿠니\",\"봉납\"]");
        assertThat(top.get("status").asText()).isEqualTo("OPEN");
        assertThat(top.get("firstSeenAt").asText()).isNotBlank();

        assertThat(ids(get("/api/v1/issues?min_docs=1"))).containsExactlyInAnyOrder(wide, busy, semis, lone);
        assertThat(get("/api/v1/issues?min_docs=5").findValuesAsText("id")).containsExactly(String.valueOf(wide),
                String.valueOf(busy));
        assertThat(ids(get("/api/v1/issues?since=2026-10-16T00:00:00+09:00"))).containsExactly(semis);
        assertThat(ids(get("/api/v1/issues?since=2026-10-16T00:00:00+09:00&min_docs=1")))
                .containsExactlyInAnyOrder(semis, lone);
        assertThat(get("/api/v1/issues?limit=1")).hasSize(1);
        // 검색: 서명 낱말 접두 일치(조사 뗌) 또는 대표 제목 부분 일치, 낱말은 모두 맞아야 한다
        assertThat(ids(get("/api/v1/issues?q=야스쿠니에"))).containsExactly(wide);
        assertThat(ids(get("/api/v1/issues?q=반도"))).containsExactly(semis);
        assertThat(ids(get("/api/v1/issues?q=생중계 논란"))).containsExactly(busy);
        assertThat(get("/api/v1/issues?q=반도체 야스쿠니")).isEmpty();

        // 상세: 소속 문서를 발행일 오름차순으로, 언론사 이름·유사도와 함께
        JsonNode detail = get("/api/v1/issues/" + semis);
        assertThat(detail.get("title").asText()).isEqualTo("반도체 수출 사상 최대");
        JsonNode docs = detail.get("documents");
        assertThat(docs.findValuesAsText("id")).containsExactly(String.valueOf(earlier), String.valueOf(first));
        assertThat(docs.get(0).get("domain").asText()).isEqualTo("b.example.com");
        assertThat(docs.get(0).get("siteName").isNull()).isTrue();
        assertThat(docs.get(1).get("siteName").asText()).isEqualTo("가일보");
        assertThat(docs.get(1).get("similarity").asDouble()).isEqualTo(0.8, org.assertj.core.data.Offset.offset(1e-6));
        assertThat(docs.get(1).get("url").asText()).isEqualTo("https://a.example.com/1");
        assertThat(call("viewer", "/api/v1/issues/999999").getStatusCode()).isEqualTo(HttpStatus.NOT_FOUND);
        assertThat(call("viewer", "/api/v1/issues?since=어제").getStatusCode()).isEqualTo(HttpStatus.BAD_REQUEST);

        // 문서 목록 행에 이슈 번호와 이슈의 기사 수가 붙는다
        JsonNode documents = get("/api/v1/documents");
        for (JsonNode d : documents) {
            long id = d.get("id").asLong();
            if (id == first || id == earlier) {
                assertThat(d.get("issueId").asLong()).isEqualTo(semis);
                assertThat(d.get("issueDocCount").asInt()).isEqualTo(4);
            } else {
                assertThat(d.get("issueId").asLong()).isEqualTo(lone);
            }
        }
        assertThat(get("/api/v1/documents/" + first).get("issueId").asLong()).isEqualTo(semis);
        assertThat(List.of("viewer", "registrar", "admin")).allSatisfy(user ->
                assertThat(call(user, "/api/v1/issues").getStatusCode()).isEqualTo(HttpStatus.OK));
    }
}
