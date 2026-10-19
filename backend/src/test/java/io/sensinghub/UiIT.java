package io.sensinghub;

import static org.assertj.core.api.Assertions.assertThat;

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
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;

import com.fasterxml.jackson.databind.JsonNode;

/** 최소 화면: 정적 파일이 서비스되고 /api/v1/me 가 X-User 헤더의 권한을 돌려준다. */
@EnabledIfEnvironmentVariable(named = "SENSING_IT_DB_URL", matches = "jdbc:postgresql:.+")
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT,
        properties = {"sensing.scheduler.enabled=false", "sensing.digest.enabled=false"})
class UiIT {

    @DynamicPropertySource
    static void db(DynamicPropertyRegistry r) {
        r.add("spring.datasource.url", () -> System.getenv("SENSING_IT_DB_URL"));
        r.add("spring.flyway.clean-disabled", () -> "false");
        r.add("sensing.secret-key", () -> "it-secret-key");
    }

    @Autowired TestRestTemplate http;

    @Test
    void rootServesIndexHtmlAndStaticScripts() {
        ResponseEntity<String> index = http.getForEntity("/", String.class);
        assertThat(index.getStatusCode()).isEqualTo(HttpStatus.OK);
        assertThat(index.getBody()).contains("id=\"app\"");

        ResponseEntity<String> app = http.getForEntity("/app.js", String.class);
        assertThat(app.getStatusCode()).isEqualTo(HttpStatus.OK);
        assertThat(app.getBody()).contains("X-User");

        for (String script : new String[] {"registrations.js", "documents.js", "issues.js", "companies.js", "watches.js",
                "digests.js", "admin.js", "ops.js", "caidentia.js", "app.css"}) {
            assertThat(http.getForEntity("/" + script, String.class).getStatusCode())
                    .as(script).isEqualTo(HttpStatus.OK);
        }
    }

    @Test
    void meReturnsRoleOfHeaderUser() {
        assertThat(me("admin").get("role").asText()).isEqualTo("ADMIN");
        assertThat(me("admin").get("loginId").asText()).isEqualTo("admin");
        assertThat(me("viewer").get("role").asText()).isEqualTo("VIEWER");

        // 헤더가 없으면 기본 사용자(registrar)
        ResponseEntity<JsonNode> none = http.getForEntity("/api/v1/me", JsonNode.class);
        assertThat(none.getStatusCode()).isEqualTo(HttpStatus.OK);
        assertThat(none.getBody().get("role").asText()).isEqualTo("REGISTRAR");
    }

    private JsonNode me(String user) {
        HttpHeaders h = new HttpHeaders();
        h.set("X-User", user);
        ResponseEntity<JsonNode> res = http.exchange("/api/v1/me", HttpMethod.GET, new HttpEntity<>(h), JsonNode.class);
        assertThat(res.getStatusCode()).isEqualTo(HttpStatus.OK);
        return res.getBody();
    }
}
