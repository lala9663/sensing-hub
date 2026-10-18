package io.sensinghub;

import static org.assertj.core.api.Assertions.assertThat;

import java.io.IOException;

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
import com.fasterxml.jackson.databind.ObjectMapper;

/**
 * 케이던시아 연동: 우리 쪽 키가 틀리면(가짜 서버가 401) 동기화는 FAILED로 기록되고 전달은 보낸 것 없이 다음을 기다리며, 호출자에게 예외가 나가지 않는다.
 * 우리 키({@code sensing.caidentia.api-key})와 가짜 서버의 키({@code sensing.caidentia.mock.api-key})를 다르게 둔 별도 컨텍스트.
 */
@EnabledIfEnvironmentVariable(named = "SENSING_IT_DB_URL", matches = "jdbc:postgresql:.+")
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT,
        properties = {"sensing.scheduler.enabled=false", "sensing.digest.enabled=false", "sensing.caidentia.enabled=false",
                "sensing.caidentia.api-key=wrong-key", "sensing.caidentia.mock.api-key=mock-key"})
class CaidentiaAuthFailureIT {

    @DynamicPropertySource
    static void db(DynamicPropertyRegistry r) {
        r.add("spring.datasource.url", () -> System.getenv("SENSING_IT_DB_URL"));
        r.add("spring.flyway.clean-disabled", () -> "false");
    }

    @Autowired TestRestTemplate http;
    @Autowired JdbcClient jdbc;
    @Autowired ObjectMapper json;

    @BeforeEach
    void clean() {
        jdbc.sql("TRUNCATE caidentia_sync_run, company_signal, company, crawl_job, audit_log RESTART IDENTITY CASCADE").update();
        jdbc.sql("UPDATE source SET active_config_version_id = NULL").update();
        jdbc.sql("TRUNCATE source_config_version, source, site RESTART IDENTITY CASCADE").update();
    }

    private ResponseEntity<String> call(HttpMethod method, String path) {
        HttpHeaders h = new HttpHeaders();
        h.set("X-User", "admin");
        return http.exchange(path, method, new HttpEntity<>(null, h), String.class);
    }

    private JsonNode read(ResponseEntity<String> res) {
        assertThat(res.getStatusCode()).as(res.getBody()).isEqualTo(HttpStatus.OK);
        try {
            return json.readTree(res.getBody());
        } catch (IOException e) {
            throw new IllegalStateException(e);
        }
    }

    @Test
    void wrongKeyRecordsFailedSyncAndLeavesSignalsPendingWithoutThrowing() {
        JsonNode run = read(call(HttpMethod.POST, "/api/v1/integrations/caidentia/sync-now"));
        assertThat(run.get("status").asText()).isEqualTo("FAILED");
        assertThat(run.get("error").asText()).contains("401").contains("SENSING_CAIDENTIA_API_KEY");
        assertThat(run.get("fetched").asInt()).isZero();
        assertThat(run.get("finishedAt").isNull()).isFalse();
        assertThat(jdbc.sql("SELECT count(*) FROM company").query(Long.class).single()).isZero();
        assertThat(jdbc.sql("SELECT status || '|' || error FROM caidentia_sync_run").query(String.class).single())
                .startsWith("FAILED|공급사 조회 실패: 케이던시아 응답 401");

        // 전달: 보낸 것 0, 실패 1, 대기 1, pushed_at은 그대로 NULL (다음 틱에 다시)
        long id = jdbc.sql("INSERT INTO company (name, external_id) VALUES ('나부품', 'SUP-0001') RETURNING id").query(Long.class).single();
        jdbc.sql("INSERT INTO company_signal (company_id, kind, severity, fingerprint, title, observed_at) "
                + "VALUES (?, 'BIZ_STATUS', 'HIGH', 'f', '폐업자', now())").param(id).update();
        JsonNode push = read(call(HttpMethod.POST, "/api/v1/integrations/caidentia/push-now"));
        assertThat(push.get("pushed").asInt()).isZero();
        assertThat(push.get("failed").asInt()).isEqualTo(1);
        assertThat(push.get("pending").asLong()).isEqualTo(1L);
        assertThat(push.get("error").asText()).contains("401");
        assertThat(jdbc.sql("SELECT count(*) FROM company_signal WHERE pushed_at IS NULL").query(Long.class).single()).isEqualTo(1L);

        JsonNode status = read(call(HttpMethod.GET, "/api/v1/integrations/caidentia"));
        assertThat(status.get("lastSync").get("status").asText()).isEqualTo("FAILED");
        assertThat(status.get("pendingCount").asLong()).isEqualTo(1L);
        // 받은 이벤트 조회도 같은 키를 쓰므로 502로 답한다 (예외 스택이 아니라 메시지)
        ResponseEntity<String> events = call(HttpMethod.GET, "/api/v1/integrations/caidentia/received-events");
        assertThat(events.getStatusCode()).isEqualTo(HttpStatus.BAD_GATEWAY);
        assertThat(events.getBody()).contains("401");
    }
}
