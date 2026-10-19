package io.sensinghub;

import static org.assertj.core.api.Assertions.assertThat;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;

import org.junit.jupiter.api.AfterEach;
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
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;

/**
 * 주요 키워드 (네이버 뉴스 탭·챗봇 칩): 기본 9개(V18), 누구나 읽고 관리자만 목록 전체를 바꾼다, 바뀐 전후를 감사 로그에 남긴다,
 * 키워드 감시 데이터는 건드리지 않는다. 테스트마다 끝나면 원래 목록으로 되돌린다 (같은 DB를 다른 IT가 쓴다).
 */
@EnabledIfEnvironmentVariable(named = "SENSING_IT_DB_URL", matches = "jdbc:postgresql:.+")
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT,
        properties = {"sensing.scheduler.enabled=false", "sensing.digest.enabled=false"})
class FeaturedKeywordIT {

    static final List<String> DEFAULTS = List.of("반도체", "메타넷", "메타넷 글로벌", "삼성전자", "DS", "DX", "DSR", "테슬라", "하이닉스");
    static final ObjectMapper JSON = new ObjectMapper();

    @DynamicPropertySource
    static void db(DynamicPropertyRegistry r) {
        r.add("spring.datasource.url", () -> System.getenv("SENSING_IT_DB_URL"));
        r.add("spring.flyway.clean-disabled", () -> "false");
        r.add("sensing.secret-key", () -> "it-secret-key");
        r.add("sensing.llm.provider", () -> "none");
    }

    @Autowired TestRestTemplate http;
    @Autowired JdbcClient jdbc;

    private List<Map<String, Object>> saved;

    @BeforeEach
    void remember() {
        saved = jdbc.sql("SELECT label, query, position FROM featured_keyword ORDER BY position, id").query().listOfRows();
        jdbc.sql("DELETE FROM audit_log WHERE action = 'FEATURED_KEYWORDS_UPDATE'").update();
    }

    @AfterEach
    void restore() {
        jdbc.sql("DELETE FROM featured_keyword").update();
        for (Map<String, Object> row : saved) {
            jdbc.sql("INSERT INTO featured_keyword (label, query, position) VALUES (?, ?, ?)")
                    .params(row.get("label"), row.get("query"), row.get("position")).update();
        }
    }

    private ResponseEntity<JsonNode> call(String user, HttpMethod method, Object body) {
        HttpHeaders h = new HttpHeaders();
        h.set("X-User", user);
        h.setContentType(MediaType.APPLICATION_JSON);
        return http.exchange("/api/v1/featured-keywords", method, new HttpEntity<>(body, h), JsonNode.class);
    }

    private static List<String> labels(JsonNode list) {
        List<String> out = new ArrayList<>();
        list.get("items").forEach(k -> out.add(k.get("label").asText()));
        return out;
    }

    private static Map<String, Object> item(String label, String query) {
        Map<String, Object> m = new java.util.LinkedHashMap<>();
        m.put("label", label);
        m.put("query", query);
        return m;
    }

    @Test
    void defaultsAreTheNineKeywordsInOrderAndEveryoneCanRead() {
        for (String user : List.of("viewer", "registrar", "admin")) {
            ResponseEntity<JsonNode> res = call(user, HttpMethod.GET, null);
            assertThat(res.getStatusCode()).isEqualTo(HttpStatus.OK);
            assertThat(labels(res.getBody())).as(user).containsExactlyElementsOf(DEFAULTS);
            res.getBody().get("items").forEach(k -> assertThat(k.get("query").asText()).isEqualTo(k.get("label").asText()));
        }
    }

    @Test
    void onlyAdminReplacesTheListAndChangesAreAudited() {
        long watchRows = jdbc.sql("SELECT count(*) FROM source WHERE kind = 'WATCH'").query(Long.class).single();
        Map<String, Object> body = Map.of("items", List.of(item("DS", "삼성전자 DS"), item(" 반도체 ", ""),
                item("HBM", null), item("하이닉스", "SK하이닉스")));
        for (String user : List.of("viewer", "registrar")) {
            ResponseEntity<JsonNode> denied = call(user, HttpMethod.PUT, body);
            assertThat(denied.getStatusCode()).as(user).isEqualTo(HttpStatus.FORBIDDEN);
        }
        assertThat(labels(call("viewer", HttpMethod.GET, null).getBody())).containsExactlyElementsOf(DEFAULTS);

        ResponseEntity<JsonNode> res = call("admin", HttpMethod.PUT, body);
        assertThat(res.getStatusCode()).isEqualTo(HttpStatus.OK);
        JsonNode items = res.getBody().get("items");
        assertThat(labels(res.getBody())).containsExactly("DS", "반도체", "HBM", "하이닉스");
        assertThat(items.get(0).get("query").asText()).isEqualTo("삼성전자 DS");
        assertThat(items.get(1).get("query").asText()).isEqualTo("반도체");   // 비우면 이름과 같다 (공백 정리)
        assertThat(items.get(2).get("query").asText()).isEqualTo("HBM");
        assertThat(res.getBody().get("updatedAt").isNull()).isFalse();
        assertThat(labels(call("viewer", HttpMethod.GET, null).getBody())).containsExactly("DS", "반도체", "HBM", "하이닉스");

        long adminId = jdbc.sql("SELECT id FROM app_user WHERE login_id = 'admin'").query(Long.class).single();
        List<Map<String, Object>> audit = jdbc.sql("""
                SELECT actor_id, target_type, detail::text AS detail FROM audit_log WHERE action = 'FEATURED_KEYWORDS_UPDATE'
                """).query().listOfRows();
        assertThat(audit).hasSize(1);
        assertThat(((Number) audit.get(0).get("actor_id")).longValue()).isEqualTo(adminId);
        assertThat(audit.get(0).get("target_type")).isEqualTo("featured_keyword");
        JsonNode detail = parse((String) audit.get(0).get("detail"));
        assertThat(detail.get("before")).hasSize(9);
        assertThat(detail.get("before").get(4).get("label").asText()).isEqualTo("DS");
        assertThat(detail.get("after")).hasSize(4);
        assertThat(detail.get("after").get(0).get("query").asText()).isEqualTo("삼성전자 DS");

        // 같은 목록을 다시 저장하면 감사 로그를 늘리지 않는다
        assertThat(call("admin", HttpMethod.PUT, body).getStatusCode()).isEqualTo(HttpStatus.OK);
        assertThat(jdbc.sql("SELECT count(*) FROM audit_log WHERE action = 'FEATURED_KEYWORDS_UPDATE'").query(Long.class)
                .single()).isEqualTo(1);

        // 빈 목록도 된다 (칩 없음)
        assertThat(call("admin", HttpMethod.PUT, Map.of("items", List.of())).getBody().get("items")).isEmpty();
        // 키워드 감시는 그대로
        assertThat(jdbc.sql("SELECT count(*) FROM source WHERE kind = 'WATCH'").query(Long.class).single()).isEqualTo(watchRows);
    }

    @Test
    void invalidListsAreRejectedWithoutChangingAnything() {
        List<Object> tooMany = new ArrayList<>();
        for (int i = 0; i < 21; i++) {
            tooMany.add(item("키워드" + i, null));
        }
        List<Map<String, Object>> bodies = List.of(
                Map.of("items", List.of(item("DS", null), item("ds", "삼성전자 DS"))),   // 대소문자만 다른 같은 이름
                Map.of("items", List.of(item("  ", "반도체"))),                          // 이름 없음
                Map.of("items", List.of(item("가".repeat(31), null))),                   // 이름 30자 초과
                Map.of("items", List.of(item("반도체", "가".repeat(101)))),              // 검색어 100자 초과
                Map.of("items", tooMany),                                                // 20개 초과
                Map.of());                                                               // items 없음 (목록을 지우지 않는다)
        for (Map<String, Object> body : bodies) {
            ResponseEntity<JsonNode> res = call("admin", HttpMethod.PUT, body);
            assertThat(res.getStatusCode()).as(String.valueOf(body).substring(0, Math.min(60, String.valueOf(body).length())))
                    .isEqualTo(HttpStatus.BAD_REQUEST);
            assertThat(res.getBody().get("error").asText()).isEqualTo("BAD_REQUEST");
        }
        assertThat(labels(call("admin", HttpMethod.GET, null).getBody())).containsExactlyElementsOf(DEFAULTS);
        assertThat(jdbc.sql("SELECT count(*) FROM audit_log WHERE action = 'FEATURED_KEYWORDS_UPDATE'").query(Long.class)
                .single()).isZero();
    }

    private static JsonNode parse(String s) {
        try {
            return JSON.readTree(s);
        } catch (Exception e) {
            throw new AssertionError(e);
        }
    }
}
