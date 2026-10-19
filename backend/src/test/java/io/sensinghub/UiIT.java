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
        // 테스트 환경에 SENSING_LLM_PROVIDER가 있어도 실제 LLM을 부르지 않는다
        r.add("sensing.llm.provider", () -> "none");
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
                "digests.js", "chat.js", "admin.js", "ops.js", "caidentia.js", "app.css"}) {
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

    /** LLM 설정이 없으면(기본) 챗봇은 오류 대신 "설정되지 않았습니다" 안내를 답으로 저장한다. */
    @Test
    void chatWithoutLlmAnswersThatItIsNotConfigured() {
        HttpHeaders h = new HttpHeaders();
        h.set("X-User", "viewer");
        h.setContentType(org.springframework.http.MediaType.APPLICATION_JSON);
        JsonNode status = http.exchange("/api/v1/chat/status", HttpMethod.GET, new HttpEntity<>(h), JsonNode.class).getBody();
        assertThat(status.get("configured").asBoolean()).isFalse();

        ResponseEntity<JsonNode> created = http.exchange("/api/v1/chat/sessions", HttpMethod.POST,
                new HttpEntity<>("{}", h), JsonNode.class);
        assertThat(created.getStatusCode()).isEqualTo(HttpStatus.CREATED);
        long id = created.getBody().get("id").asLong();
        ResponseEntity<JsonNode> sent = http.exchange("/api/v1/chat/sessions/" + id + "/messages", HttpMethod.POST,
                new HttpEntity<>("{\"content\":\"오늘 이슈 알려 줘\"}", h), JsonNode.class);
        assertThat(sent.getStatusCode()).isEqualTo(HttpStatus.OK);
        JsonNode answer = sent.getBody().get("assistant");
        assertThat(answer.get("error").asBoolean()).isTrue();
        assertThat(answer.get("content").asText()).contains("LLM이 설정되지 않았습니다");
    }

    private JsonNode me(String user) {
        HttpHeaders h = new HttpHeaders();
        h.set("X-User", user);
        ResponseEntity<JsonNode> res = http.exchange("/api/v1/me", HttpMethod.GET, new HttpEntity<>(h), JsonNode.class);
        assertThat(res.getStatusCode()).isEqualTo(HttpStatus.OK);
        return res.getBody();
    }
}
