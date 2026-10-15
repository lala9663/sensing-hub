package io.sensinghub;

import static org.assertj.core.api.Assertions.assertThat;

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

import io.sensinghub.scheduler.CrawlScheduler;

/**
 * 실제 PostgreSQL(+pgvector)로 등록 → 작업 생성 → 스케줄러 흐름을 확인한다.
 * 빈 DB를 가리키는 SENSING_IT_DB_URL 환경 변수가 있을 때만 실행된다.
 */
@EnabledIfEnvironmentVariable(named = "SENSING_IT_DB_URL", matches = "jdbc:postgresql:.+")
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT,
        properties = "sensing.scheduler.enabled=false")
class RegistrationFlowIT {

    @DynamicPropertySource
    static void db(DynamicPropertyRegistry r) {
        r.add("spring.datasource.url", () -> System.getenv("SENSING_IT_DB_URL"));
        r.add("spring.flyway.clean-disabled", () -> "false");
    }

    @Autowired TestRestTemplate http;
    @Autowired JdbcClient jdbc;
    @Autowired org.springframework.context.ApplicationContext ctx;

    @BeforeEach
    void clean() {
        jdbc.sql("""
                TRUNCATE chat_feedback, chat_citation, chat_message, chat_session, chunk, attachment,
                         document_version, document, crawl_error, crawl_job, registration_request, audit_log
                RESTART IDENTITY CASCADE
                """).update();
        jdbc.sql("UPDATE source SET active_config_version_id = NULL").update();
        jdbc.sql("TRUNCATE source_config_version, source, site RESTART IDENTITY CASCADE").update();
    }

    private ResponseEntity<String> post(String user, String path, String json) {
        HttpHeaders h = new HttpHeaders();
        h.setContentType(MediaType.APPLICATION_JSON);
        h.set("X-User", user);
        return http.exchange(path, HttpMethod.POST, new HttpEntity<>(json, h), String.class);
    }

    @Test
    void registrarRegistersArticleUrlAndOnboardJobIsQueued() {
        ResponseEntity<String> res = post("registrar", "/api/v1/registrations",
                "{\"url\":\"https://daily.hankooki.com/news/articleView.html?idxno=1415376\"}");
        assertThat(res.getStatusCode()).isEqualTo(HttpStatus.ACCEPTED);
        assertThat(res.getBody()).contains("\"status\":\"PROCESSING\"").contains("daily.hankooki.com");

        String jobType = jdbc.sql("SELECT job_type FROM crawl_job").query(String.class).single();
        assertThat(jobType).isEqualTo("ONBOARD");

        // 같은 도메인을 다시 등록하면 중복으로 안내
        ResponseEntity<String> dup = post("viewer", "/api/v1/registrations", "{\"url\":\"daily.hankooki.com\"}");
        assertThat(dup.getBody()).contains("\"status\":\"DUPLICATE\"");
    }

    @Test
    void viewerRequestWaitsForApprovalThenAdminApproves() {
        String body = post("viewer", "/api/v1/registrations", "{\"url\":\"https://www.example.com\"}").getBody();
        assertThat(body).contains("AWAITING_APPROVAL");
        assertThat(jdbc.sql("SELECT count(*) FROM crawl_job").query(Long.class).single()).isZero();

        long id = jdbc.sql("SELECT id FROM registration_request").query(Long.class).single();
        assertThat(post("viewer", "/api/v1/registrations/" + id + "/approve", "").getStatusCode())
                .isEqualTo(HttpStatus.FORBIDDEN);
        assertThat(post("admin", "/api/v1/registrations/" + id + "/approve", "").getBody()).contains("PROCESSING");
        assertThat(jdbc.sql("SELECT count(*) FROM crawl_job").query(Long.class).single()).isEqualTo(1L);
    }

    @Test
    void internalAddressIsRejected() {
        ResponseEntity<String> res = post("registrar", "/api/v1/registrations", "{\"url\":\"http://169.254.169.254\"}");
        assertThat(res.getStatusCode()).isEqualTo(HttpStatus.BAD_REQUEST);
    }

    @Test
    void schedulerEnqueuesDueActiveSourceOnlyOnce() {
        post("registrar", "/api/v1/registrations", "{\"url\":\"https://news.example.org\"}");
        // 워커가 온보딩을 끝냈다고 가정
        jdbc.sql("UPDATE crawl_job SET status = 'SUCCESS'").update();
        jdbc.sql("UPDATE source SET status = 'ACTIVE', next_run_at = now() - interval '1 minute'").update();

        CrawlScheduler scheduler = new CrawlScheduler(jdbc);
        assertThat(scheduler.enqueueDueSources()).isEqualTo(1);
        jdbc.sql("UPDATE source SET next_run_at = now() - interval '1 minute'").update();
        assertThat(scheduler.enqueueDueSources()).isZero(); // 열린 작업이 있으면 건너뜀
    }

    @Test
    void expiredLeaseIsReclaimed() {
        post("registrar", "/api/v1/registrations", "{\"url\":\"https://lease.example.org\"}");
        jdbc.sql("UPDATE crawl_job SET status = 'RUNNING', attempt = 1, lease_until = now() - interval '1 minute'").update();
        new CrawlScheduler(jdbc).reclaimExpiredLeases();
        assertThat(jdbc.sql("SELECT status FROM crawl_job").query(String.class).single()).isEqualTo("QUEUED");
    }
}
