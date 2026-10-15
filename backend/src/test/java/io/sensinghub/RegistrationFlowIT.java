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

import com.fasterxml.jackson.databind.JsonNode;
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

    private JsonNode get(String user, String path) {
        HttpHeaders h = new HttpHeaders();
        h.set("X-User", user);
        ResponseEntity<JsonNode> res = http.exchange(path, HttpMethod.GET, new HttpEntity<>(h), JsonNode.class);
        assertThat(res.getStatusCode()).isEqualTo(HttpStatus.OK);
        return res.getBody();
    }

    /** 워커가 처리한 결과를 흉내 낸다: 소스 상태 + 실패 작업 + 실패 기록. */
    private long workerLeft(String domain, String status, String reason, String evidencePath, String jobStatus) {
        post("registrar", "/api/v1/registrations", "{\"url\":\"https://" + domain + "\"}");
        long sourceId = jdbc.sql("SELECT s.id FROM source s JOIN site t ON t.id = s.site_id WHERE t.domain = ?")
                .param(domain).query(Long.class).single();
        jdbc.sql("UPDATE source SET status = ?, status_reason = ? WHERE id = ?").params(status, reason, sourceId).update();
        jdbc.sql("UPDATE crawl_job SET status = ? WHERE source_id = ?").params(jobStatus, sourceId).update();
        long jobId = jdbc.sql("SELECT id FROM crawl_job WHERE source_id = ?").param(sourceId).query(Long.class).single();
        jdbc.sql("""
                INSERT INTO crawl_error (job_id, url, stage, code, message, evidence_path)
                VALUES (?, ?, 'DISCOVERY', ?, ?, ?)
                """).params(jobId, "https://" + domain, reason, reason + " 메시지", evidencePath).update();
        if (status.equals("REVIEW")) {
            jdbc.sql("UPDATE registration_request SET status = 'REVIEW' WHERE source_id = ?").param(sourceId).update();
        }
        return sourceId;
    }

    @Test
    void reviewQueueCollectsItemsNeedingAdminInPriorityOrder() {
        workerLeft("blocked.example.org", "BLOCKED", "BOT_CHALLENGE", "20261016/job1-bot_challenge-a.html", "FAILED");
        long broken = workerLeft("broken.example.org", "BROKEN", "ANOMALY_DROP", null, "QUEUED"); // 재점검 대기 중
        long review = workerLeft("review.example.org", "REVIEW", "DISCOVERY_FAILED",
                "20261016/job3-discovery_failed-b.html", "FAILED");
        post("viewer", "/api/v1/registrations", "{\"url\":\"https://approval.example.org\"}");

        HttpHeaders viewer = new HttpHeaders();
        viewer.set("X-User", "viewer");
        assertThat(http.exchange("/api/v1/review-queue", HttpMethod.GET, new HttpEntity<>(viewer), String.class)
                .getStatusCode()).isEqualTo(HttpStatus.FORBIDDEN);

        JsonNode items = get("admin", "/api/v1/review-queue");
        assertThat(items).hasSize(4);
        assertThat(items.findValuesAsText("kind")).containsExactly("APPROVAL", "REVIEW", "BROKEN", "BLOCKED");

        JsonNode approval = items.get(0);
        assertThat(approval.get("domain").asText()).isEqualTo("approval.example.org");
        assertThat(approval.get("requestedBy").asText()).isEqualTo("일반 사용자");

        JsonNode rev = items.get(1);
        assertThat(rev.get("sourceId").asLong()).isEqualTo(review);
        assertThat(rev.get("reasonCode").asText()).isEqualTo("DISCOVERY_FAILED");
        assertThat(rev.get("message").asText()).isEqualTo("DISCOVERY_FAILED 메시지");
        assertThat(rev.get("evidencePath").asText()).isEqualTo("20261016/job3-discovery_failed-b.html");
        assertThat(rev.get("registrationId").isNull()).isFalse();
        assertThat(rev.get("actions").toString()).isEqualTo("[\"rediscover\",\"exclude\"]");

        JsonNode brk = items.get(2);
        assertThat(brk.get("sourceId").asLong()).isEqualTo(broken);
        assertThat(brk.get("inProgress").asBoolean()).isTrue();

        assertThat(get("admin", "/api/v1/review-queue?kind=broken")).hasSize(1);
        JsonNode counts = get("admin", "/api/v1/review-queue/counts");
        assertThat(counts.toString()).isEqualTo("{\"APPROVAL\":1,\"REVIEW\":1,\"BROKEN\":1,\"BLOCKED\":1}");
    }

    @Test
    void adminResolvesQueueItemsByRejectAndExclude() {
        long review = workerLeft("review.example.org", "REVIEW", "QUALITY_FAILED", null, "FAILED");
        post("viewer", "/api/v1/registrations", "{\"url\":\"https://approval.example.org\"}");
        long approvalId = jdbc.sql("SELECT id FROM registration_request WHERE status = 'AWAITING_APPROVAL'")
                .query(Long.class).single();

        String rejected = post("admin", "/api/v1/registrations/" + approvalId + "/reject",
                "{\"reason\":\"업무와 관련 없는 사이트\"}").getBody();
        assertThat(rejected).contains("\"status\":\"REJECTED\"").contains("업무와 관련 없는 사이트");

        assertThat(post("admin", "/api/v1/sources/" + review + "/actions/exclude", "").getStatusCode())
                .isEqualTo(HttpStatus.OK);
        assertThat(jdbc.sql("SELECT status FROM registration_request WHERE source_id = ?").param(review)
                .query(String.class).single()).isEqualTo("REJECTED");

        assertThat(get("admin", "/api/v1/review-queue")).isEmpty();
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
