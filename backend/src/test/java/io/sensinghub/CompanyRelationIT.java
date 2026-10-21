package io.sensinghub;

import static org.assertj.core.api.Assertions.assertThat;

import java.util.ArrayList;
import java.util.LinkedHashMap;
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

import io.sensinghub.relation.CompanyRelationService;

/**
 * 회사 관계 목록 (자사·경쟁사·고객사·협력사 라벨): 기본은 자사 = 삼성전자(V20, '삼성' 단독 별칭 없음), 누구나 읽고 관리자만
 * 추가·수정·삭제한다, 바꿀 때마다 감사 로그(전후 값)가 남고 관리자만 변경 기록을 본다, 이름·별칭은 목록 전체에서 하나뿐이다.
 * 테스트마다 끝나면 기본 목록으로 되돌린다 (같은 DB를 다른 IT가 쓴다).
 */
@EnabledIfEnvironmentVariable(named = "SENSING_IT_DB_URL", matches = "jdbc:postgresql:.+")
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT,
        properties = {"sensing.scheduler.enabled=false", "sensing.digest.enabled=false"})
class CompanyRelationIT {

    static final ObjectMapper JSON = new ObjectMapper();
    static final String BASE = "/api/v1/company-relations";

    @DynamicPropertySource
    static void db(DynamicPropertyRegistry r) {
        r.add("spring.datasource.url", () -> System.getenv("SENSING_IT_DB_URL"));
        r.add("spring.flyway.clean-disabled", () -> "false");
        r.add("sensing.secret-key", () -> "it-secret-key");
        r.add("sensing.llm.provider", () -> "none");
    }

    @Autowired TestRestTemplate http;
    @Autowired JdbcClient jdbc;
    @Autowired CompanyRelationService relations;

    private List<Map<String, Object>> saved;

    @BeforeEach
    void remember() {
        saved = jdbc.sql("SELECT id, name, aliases::text AS aliases, exclude::text AS exclude, relation, note FROM company_relation")
                .query().listOfRows();
        jdbc.sql("DELETE FROM audit_log WHERE target_type = 'company_relation'").update();
    }

    @AfterEach
    void restore() {
        jdbc.sql("DELETE FROM company_relation").update();
        for (Map<String, Object> row : saved) {
            jdbc.sql("""
                    INSERT INTO company_relation (id, name, aliases, exclude, relation, note)
                    VALUES (?, ?, CAST(? AS jsonb), CAST(? AS jsonb), ?, ?)
                    """)
                    .params(row.get("id"), row.get("name"), row.get("aliases"), row.get("exclude"), row.get("relation"),
                            row.get("note"))
                    .update();
        }
        jdbc.sql("DELETE FROM audit_log WHERE target_type = 'company_relation'").update();
    }

    private ResponseEntity<JsonNode> call(String user, HttpMethod method, String path, Object body) {
        HttpHeaders h = new HttpHeaders();
        h.set("X-User", user);
        h.setContentType(MediaType.APPLICATION_JSON);
        return http.exchange(BASE + path, method, new HttpEntity<>(body, h), JsonNode.class);
    }

    private static Map<String, Object> input(String name, String relation, List<String> aliases, List<String> exclude) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("name", name);
        m.put("relation", relation);
        m.put("aliases", aliases);
        m.put("exclude", exclude);
        return m;
    }

    private static List<String> texts(JsonNode array) {
        List<String> out = new ArrayList<>();
        array.forEach(n -> out.add(n.asText()));
        return out;
    }

    private long auditCount() {
        return jdbc.sql("SELECT count(*) FROM audit_log WHERE target_type = 'company_relation'").query(Long.class).single();
    }

    @Test
    void defaultIsSamsungElectronicsAsOwnCompanyAndEveryoneCanRead() {
        for (String user : List.of("viewer", "registrar", "admin")) {
            ResponseEntity<JsonNode> res = call(user, HttpMethod.GET, "", null);
            assertThat(res.getStatusCode()).as(user).isEqualTo(HttpStatus.OK);
            JsonNode items = res.getBody().get("items");
            assertThat(items).as(user).hasSize(1);
            assertThat(items.get(0).get("name").asText()).isEqualTo("삼성전자");
            assertThat(items.get(0).get("relation").asText()).isEqualTo("OWN");
            assertThat(items.get(0).get("label").asText()).isEqualTo("자사");
            assertThat(texts(items.get(0).get("aliases")))
                    .containsExactly("Samsung Electronics", "삼성 DS", "삼성 DX", "DS부문", "DX부문")
                    .doesNotContain("삼성", "Samsung");                       // 계열사·프로야구까지 걸리는 넓은 별칭은 기본에서 뺐다
            assertThat(res.getBody().get("relations").get("COMPETITOR").asText()).isEqualTo("경쟁사");
        }
        // 챗봇 프롬프트에 넣는 한 줄
        assertThat(relations.summary()).isEqualTo("자사: 삼성전자 / 경쟁사: (아직 없음) / 고객사: (아직 없음) / 협력사: (아직 없음)");
    }

    @Test
    void onlyAdminChangesTheListAndEveryChangeIsAudited() {
        Map<String, Object> rival = input(" SK하이닉스 ", "COMPETITOR", List.of(" 하이닉스 ", "sk하이닉스", "SK hynix", "SK  HYNIX", ""),
                List.of());
        for (String user : List.of("viewer", "registrar")) {
            assertThat(call(user, HttpMethod.POST, "", rival).getStatusCode()).as(user).isEqualTo(HttpStatus.FORBIDDEN);
            assertThat(call(user, HttpMethod.GET, "/history", null).getStatusCode()).as(user).isEqualTo(HttpStatus.FORBIDDEN);
        }
        long samsung = ((Number) saved.get(0).get("id")).longValue();
        for (String user : List.of("viewer", "registrar")) {
            assertThat(call(user, HttpMethod.PUT, "/" + samsung, input("삼성전자", "COMPETITOR", List.of(), List.of()))
                    .getStatusCode()).as(user).isEqualTo(HttpStatus.FORBIDDEN);
            assertThat(call(user, HttpMethod.DELETE, "/" + samsung, null).getStatusCode()).as(user).isEqualTo(HttpStatus.FORBIDDEN);
        }
        assertThat(auditCount()).isZero();

        // 추가: 공백 정리, 이름과 같거나 대소문자·공백만 다른 별칭은 뺀다
        ResponseEntity<JsonNode> created = call("admin", HttpMethod.POST, "", rival);
        assertThat(created.getStatusCode()).isEqualTo(HttpStatus.CREATED);
        long id = created.getBody().get("id").asLong();
        assertThat(created.getBody().get("name").asText()).isEqualTo("SK하이닉스");
        assertThat(created.getBody().get("label").asText()).isEqualTo("경쟁사");
        assertThat(texts(created.getBody().get("aliases"))).containsExactly("하이닉스", "SK hynix");
        assertThat(created.getBody().get("updatedBy").asText()).isEqualTo("운영 관리자");
        JsonNode items = call("viewer", HttpMethod.GET, "", null).getBody().get("items");
        assertThat(items).hasSize(2);
        assertThat(items.get(0).get("label").asText()).isEqualTo("자사");     // 자사 → 경쟁사 → 고객사 → 협력사 순서
        assertThat(items.get(1).get("name").asText()).isEqualTo("SK하이닉스");

        // 수정: 관계·제외어를 바꾸면 전후가 남는다. 같은 내용으로 다시 저장하면 감사 로그를 늘리지 않는다
        Map<String, Object> changed = input("SK하이닉스", "PARTNER", List.of("하이닉스"), List.of("하이닉스반도체"));
        changed.put("note", "메모리 협력");
        ResponseEntity<JsonNode> updated = call("admin", HttpMethod.PUT, "/" + id, changed);
        assertThat(updated.getStatusCode()).isEqualTo(HttpStatus.OK);
        assertThat(updated.getBody().get("label").asText()).isEqualTo("협력사");
        assertThat(texts(updated.getBody().get("exclude"))).containsExactly("하이닉스반도체");
        assertThat(call("admin", HttpMethod.PUT, "/" + id, changed).getStatusCode()).isEqualTo(HttpStatus.OK);

        // 삭제
        assertThat(call("admin", HttpMethod.DELETE, "/" + id, null).getStatusCode()).isEqualTo(HttpStatus.NO_CONTENT);
        assertThat(call("admin", HttpMethod.DELETE, "/" + id, null).getStatusCode()).isEqualTo(HttpStatus.NOT_FOUND);
        assertThat(call("viewer", HttpMethod.GET, "", null).getBody().get("items")).hasSize(1);

        long adminId = jdbc.sql("SELECT id FROM app_user WHERE login_id = 'admin'").query(Long.class).single();
        List<Map<String, Object>> audit = jdbc.sql("""
                SELECT actor_id, action, target_id, detail::text AS detail FROM audit_log
                WHERE target_type = 'company_relation' ORDER BY id
                """).query().listOfRows();
        assertThat(audit).extracting(r -> r.get("action"))
                .containsExactly("COMPANY_RELATION_CREATE", "COMPANY_RELATION_UPDATE", "COMPANY_RELATION_DELETE");
        assertThat(audit).allSatisfy(r -> {
            assertThat(((Number) r.get("actor_id")).longValue()).isEqualTo(adminId);
            assertThat(((Number) r.get("target_id")).longValue()).isEqualTo(id);
        });
        JsonNode create = parse((String) audit.get(0).get("detail"));
        assertThat(create.get("after").get("relation").asText()).isEqualTo("COMPETITOR");
        assertThat(create.has("before")).isFalse();
        JsonNode update = parse((String) audit.get(1).get("detail"));
        assertThat(update.get("before").get("relation").asText()).isEqualTo("COMPETITOR");
        assertThat(update.get("after").get("relation").asText()).isEqualTo("PARTNER");
        assertThat(texts(update.get("before").get("aliases"))).containsExactly("하이닉스", "SK hynix");
        assertThat(texts(update.get("after").get("exclude"))).containsExactly("하이닉스반도체");
        assertThat(update.get("after").get("note").asText()).isEqualTo("메모리 협력");
        JsonNode delete = parse((String) audit.get(2).get("detail"));
        assertThat(delete.get("before").get("name").asText()).isEqualTo("SK하이닉스");

        // 변경 기록 (관리자): 새것부터, 누가 했는지 이름으로
        JsonNode history = call("admin", HttpMethod.GET, "/history", null).getBody();
        assertThat(history).hasSize(3);
        assertThat(history.get(0).get("action").asText()).isEqualTo("COMPANY_RELATION_DELETE");
        assertThat(history.get(0).get("actor").asText()).isEqualTo("운영 관리자");
        assertThat(history.get(2).get("detail").get("after").get("name").asText()).isEqualTo("SK하이닉스");
    }

    @Test
    void invalidInputsAreRejectedWithoutChangingAnything() {
        List<String> many = new ArrayList<>();
        for (int i = 0; i < 21; i++) {
            many.add("별칭" + i);
        }
        List<Map<String, Object>> bodies = List.of(
                input("  ", "COMPETITOR", List.of(), List.of()),                       // 이름 없음
                input("마이크론", "FRIEND", List.of(), List.of()),                      // 모르는 관계
                input("마이크론", null, List.of(), List.of()),                          // 관계 없음
                input("삼", "COMPETITOR", List.of(), List.of()),                        // 한 글자
                input("마이크론", "COMPETITOR", List.of("M"), List.of()),               // 한 글자 별칭
                input("가".repeat(101), "COMPETITOR", List.of(), List.of()),            // 100자 초과
                input("마이크론", "COMPETITOR", many, List.of()),                        // 별칭 20개 초과
                input("삼성전자", "COMPETITOR", List.of(), List.of()),                   // 이미 있는 이름
                input("삼성디에스", "OWN", List.of("ds부문"), List.of()),                // 다른 항목의 별칭 (대소문자 무시)
                input("Samsung  electronics", "CUSTOMER", List.of(), List.of()));        // 다른 항목의 별칭 (공백 무시)
        for (Map<String, Object> body : bodies) {
            ResponseEntity<JsonNode> res = call("admin", HttpMethod.POST, "", body);
            assertThat(res.getStatusCode()).as(String.valueOf(body.get("name")) + "/" + body.get("relation"))
                    .isEqualTo(HttpStatus.BAD_REQUEST);
            assertThat(res.getBody().get("error").asText()).isEqualTo("BAD_REQUEST");
        }
        assertThat(call("admin", HttpMethod.PUT, "/999999", input("마이크론", "COMPETITOR", List.of(), List.of()))
                .getStatusCode()).isEqualTo(HttpStatus.NOT_FOUND);
        // 자기 자신의 이름·별칭과 겹치는 것은 괜찮다 (수정)
        long samsung = ((Number) saved.get(0).get("id")).longValue();
        Map<String, Object> unchanged = input("삼성전자", "OWN", List.of("Samsung Electronics", "삼성 DS", "삼성 DX", "DS부문", "DX부문"),
                List.of());
        unchanged.put("note", saved.get(0).get("note"));
        ResponseEntity<JsonNode> same = call("admin", HttpMethod.PUT, "/" + samsung, unchanged);
        assertThat(same.getStatusCode()).isEqualTo(HttpStatus.OK);
        assertThat(call("admin", HttpMethod.GET, "", null).getBody().get("items")).hasSize(1);
        assertThat(auditCount()).isZero();                                              // 실패와 같은 내용 저장은 기록하지 않는다
    }

    private static JsonNode parse(String s) {
        try {
            return JSON.readTree(s);
        } catch (Exception e) {
            throw new AssertionError(e);
        }
    }
}
