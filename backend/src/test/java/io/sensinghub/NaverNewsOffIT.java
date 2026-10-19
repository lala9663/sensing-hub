package io.sensinghub;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.AfterAll;
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
 * 네이버 뉴스 검색을 끈 모드(SENSING_NAVER_MODE=off): 키가 있어도 탭 검색은 503 NAVER_OFF이고 네이버(가짜 HUB)를 부르지 않는다.
 * 화면은 status(mode off, liveAvailable false)를 보고 안내만 보여 준다. 빈 DB를 가리키는 SENSING_IT_DB_URL이 있을 때만 실행된다.
 */
@EnabledIfEnvironmentVariable(named = "SENSING_IT_DB_URL", matches = "jdbc:postgresql:.+")
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT,
        properties = {"sensing.scheduler.enabled=false", "sensing.digest.enabled=false", "sensing.caidentia.enabled=false"})
class NaverNewsOffIT {

    static final NaverNewsIT.FakeHub hub = NaverNewsIT.FakeHub.start();

    @DynamicPropertySource
    static void props(DynamicPropertyRegistry r) {
        r.add("spring.datasource.url", () -> System.getenv("SENSING_IT_DB_URL"));
        r.add("spring.flyway.clean-disabled", () -> "false");
        r.add("sensing.secret-key", () -> NaverNewsIT.SECRET_KEY);
        r.add("sensing.naver.mode", () -> "off");
        r.add("sensing.naver.api", () -> "hub");
        r.add("sensing.naver.search-url", () -> hub.base() + "/search/v1/news");
        r.add("sensing.llm.provider", () -> "none");
    }

    @AfterAll
    static void stopHub() {
        hub.server.stop(0);
    }

    @Autowired TestRestTemplate http;
    @Autowired JdbcClient jdbc;
    @Autowired ObjectMapper json;

    @Test
    void offModeShowsGuidanceAndNeverCallsNaver() throws Exception {
        jdbc.sql("TRUNCATE audit_log, api_credential RESTART IDENTITY CASCADE").update();
        HttpHeaders admin = new HttpHeaders();
        admin.setContentType(MediaType.APPLICATION_JSON);
        admin.set("X-User", "admin");
        ResponseEntity<String> saved = http.exchange("/api/v1/api-credentials", HttpMethod.POST, new HttpEntity<>(
                "{\"provider\":\"naver_news\",\"secret\":\"" + NaverNewsIT.CLIENT_ID + ":" + NaverNewsIT.CLIENT_SECRET + "\"}", admin),
                String.class);
        assertThat(saved.getStatusCode()).isEqualTo(HttpStatus.OK);

        JsonNode status = get("/api/v1/naver/status").getBody();
        assertThat(status.get("mode").asText()).isEqualTo("off");
        assertThat(status.get("keyRegistered").asBoolean()).isTrue();
        assertThat(status.get("liveAvailable").asBoolean()).isFalse();

        ResponseEntity<JsonNode> search = get("/api/v1/naver/news?query=메타넷");
        assertThat(search.getStatusCode()).isEqualTo(HttpStatus.SERVICE_UNAVAILABLE);
        assertThat(search.getBody().get("error").asText()).isEqualTo("NAVER_OFF");
        assertThat(search.getBody().get("message").asText()).contains("SENSING_NAVER_MODE=off");
        assertThat(hub.requests).isEmpty();
        assertThat(jdbc.sql("SELECT count(*) FROM audit_log WHERE action = 'NAVER_SEARCH'").query(Long.class).single()).isZero();
    }

    private ResponseEntity<JsonNode> get(String path) throws Exception {
        HttpHeaders h = new HttpHeaders();
        h.set("X-User", "viewer");
        ResponseEntity<String> res = http.exchange(path, HttpMethod.GET, new HttpEntity<>(h), String.class);
        return ResponseEntity.status(res.getStatusCode()).body(json.readTree(res.getBody()));
    }
}
