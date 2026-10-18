package io.sensinghub;

import static org.assertj.core.api.Assertions.assertThat;

import java.util.ArrayList;
import java.util.List;

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

/**
 * 사이트 일괄 등록과 대량 소스 운영 (M3): bulk 50건(중복·형식 오류·이미 등록 섞어서)과 묶음 요약, ops/overview의 각 절,
 * sources/health 정렬, PUT으로 정한 주기 잠금. 실제 PostgreSQL이 필요하며 SENSING_IT_DB_URL이 있을 때만 실행된다.
 * 워커가 하는 일(온보딩·수집)은 SQL로 흉내 낸다.
 */
@EnabledIfEnvironmentVariable(named = "SENSING_IT_DB_URL", matches = "jdbc:postgresql:.+")
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT,
        properties = {"sensing.scheduler.enabled=false", "sensing.digest.enabled=false"})
class BulkOpsIT {

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
                TRUNCATE digest_run, digest_subscription, chat_feedback, chat_citation, chat_message, chat_session, chunk,
                         attachment, document_version, document_watch, document_enrichment, document_company, company_signal,
                         company, document, crawl_error, crawl_job, registration_request, audit_log, api_credential
                RESTART IDENTITY CASCADE
                """).update();
        jdbc.sql("UPDATE source SET active_config_version_id = NULL").update();
        jdbc.sql("TRUNCATE source_config_version, source, site RESTART IDENTITY CASCADE").update();
    }

    // ------------------------------------------------------------------ HTTP 도우미

    private ResponseEntity<String> call(String user, HttpMethod method, String path, String body, MediaType type) {
        HttpHeaders h = new HttpHeaders();
        h.set("X-User", user);
        if (type != null) {
            h.setContentType(type);
        }
        return http.exchange(path, method, new HttpEntity<>(body, h), String.class);
    }

    private ResponseEntity<String> post(String user, String path, String json) {
        return call(user, HttpMethod.POST, path, json, MediaType.APPLICATION_JSON);
    }

    private ResponseEntity<String> put(String user, String path, String json) {
        return call(user, HttpMethod.PUT, path, json, MediaType.APPLICATION_JSON);
    }

    private ResponseEntity<JsonNode> getRaw(String user, String path) {
        HttpHeaders h = new HttpHeaders();
        h.set("X-User", user);
        return http.exchange(path, HttpMethod.GET, new HttpEntity<>(h), JsonNode.class);
    }

    private JsonNode get(String user, String path) {
        ResponseEntity<JsonNode> res = getRaw(user, path);
        assertThat(res.getStatusCode()).isEqualTo(HttpStatus.OK);
        return res.getBody();
    }

    private JsonNode json(ResponseEntity<String> res) {
        try {
            return new com.fasterxml.jackson.databind.ObjectMapper().readTree(res.getBody());
        } catch (java.io.IOException e) {
            throw new IllegalStateException(e);
        }
    }

    private long count(String sql, Object... params) {
        return jdbc.sql(sql).params(params).query(Long.class).single();
    }

    // ------------------------------------------------------------------ 1. 일괄 등록

    @Test
    void bulkRegistersFiftyUrlsAndBatchSummaryShowsProgress() {
        // 이미 한 건씩 등록된 사이트 둘
        post("registrar", "/api/v1/registrations", "{\"url\":\"https://already1.example.org\"}");
        post("registrar", "/api/v1/registrations", "{\"url\":\"https://already2.example.org\"}");

        List<String> urls = new ArrayList<>();
        for (int i = 1; i <= 44; i++) {
            // 기사 주소·www.·포트 없는 주소가 섞여도 도메인 단위로 등록된다
            urls.add(i % 3 == 0 ? "https://www.site%02d.example.org/news/articleView.html?idxno=%d".formatted(i, i)
                    : i % 3 == 1 ? "site%02d.example.org".formatted(i) : "https://site%02d.example.org".formatted(i));
        }
        urls.add(10, "https://www.site01.example.org/other/path");      // 같은 묶음 안의 중복 도메인
        urls.add(20, "site02.example.org");                              // 같은 묶음 안의 중복 도메인
        urls.add(30, "https://already1.example.org/x");                  // 이미 등록된 사이트
        urls.add(31, "already2.example.org");
        urls.add(40, "http://127.0.0.1/admin");                          // 내부망 → 형식 오류
        urls.add("not a url");                                            // 형식 오류
        assertThat(urls).hasSize(50);
        String body = "{\"urls\":" + toJsonArray(urls) + "}";

        // VIEWER는 일괄 등록을 못 한다 (한 건 등록·승인 대기 흐름 그대로)
        assertThat(post("viewer", "/api/v1/registrations/bulk", body).getStatusCode()).isEqualTo(HttpStatus.FORBIDDEN);
        assertThat(count("SELECT count(*) FROM registration_request")).isEqualTo(2);

        ResponseEntity<String> res = post("registrar", "/api/v1/registrations/bulk", body);
        assertThat(res.getStatusCode()).isEqualTo(HttpStatus.ACCEPTED);
        JsonNode result = json(res);
        String batchId = result.get("batchId").asText();
        assertThat(batchId).hasSize(32);
        assertThat(result.get("total").asInt()).isEqualTo(50);
        assertThat(result.get("accepted").asInt()).isEqualTo(44);
        assertThat(result.get("duplicate").asInt()).isEqualTo(4);
        assertThat(result.get("invalid").asInt()).isEqualTo(2);
        JsonNode items = result.get("items");
        assertThat(items).hasSize(50);
        assertThat(items.get(0).get("url").asText()).isEqualTo("site01.example.org");
        assertThat(items.get(0).get("status").asText()).isEqualTo("PROCESSING");
        assertThat(items.get(0).get("domain").asText()).isEqualTo("site01.example.org");
        assertThat(items.get(0).get("registrationId").isNull()).isFalse();
        assertThat(items.get(10).get("status").asText()).isEqualTo("DUPLICATE");
        assertThat(items.get(10).get("domain").asText()).isEqualTo("site01.example.org");
        assertThat(items.get(10).get("message").asText()).contains("첫 건만");
        assertThat(items.get(30).get("status").asText()).isEqualTo("DUPLICATE");
        assertThat(items.get(30).get("message").asText()).contains("이미 등록된 사이트");
        assertThat(items.get(40).get("status").asText()).isEqualTo("INVALID");
        assertThat(items.get(40).get("domain").isNull()).isTrue();
        assertThat(items.get(40).get("message").asText()).contains("내부망");
        assertThat(items.get(49).get("status").asText()).isEqualTo("INVALID");
        assertThat(items.get(49).get("url").asText()).isEqualTo("not a url");

        // 묶음의 50건이 모두 registration_request에 남고(형식 오류 포함), 소스·ONBOARD 작업은 새 도메인 44곳에만 생긴다
        assertThat(count("SELECT count(*) FROM registration_request WHERE batch_id = ?", batchId)).isEqualTo(50);
        assertThat(count("SELECT count(*) FROM registration_request WHERE batch_id IS NULL")).isEqualTo(2);
        assertThat(count("SELECT count(*) FROM registration_request WHERE batch_id = ? AND status = 'INVALID'", batchId))
                .isEqualTo(2);
        assertThat(count("SELECT count(*) FROM source")).isEqualTo(46);
        assertThat(count("SELECT count(*) FROM crawl_job WHERE job_type = 'ONBOARD' AND status = 'QUEUED'")).isEqualTo(46);
        assertThat(jdbc.sql("SELECT detail::text FROM audit_log WHERE action = 'REGISTRATION_BULK'").query(String.class).single())
                .contains("\"accepted\": 44").contains(batchId);

        // 몇 분 뒤: 워커가 30곳은 등록을 끝냈고 5곳은 관리자 큐(REVIEW), 2곳은 차단(REJECTED), 7곳은 아직 진행 중이라고 치자
        List<Long> processing = jdbc.sql("SELECT id FROM registration_request WHERE batch_id = ? AND status = 'PROCESSING' ORDER BY id")
                .param(batchId).query(Long.class).list();
        assertThat(processing).hasSize(44);
        for (int i = 0; i < 44; i++) {
            String status = i < 30 ? "COMPLETED" : i < 35 ? "REVIEW" : i < 37 ? "REJECTED" : "PROCESSING";
            String step = i < 30 ? "DONE" : i < 35 ? "REVIEW" : i < 37 ? "REJECTED" : i < 40 ? "DISCOVERY" : "QUEUED";
            jdbc.sql("UPDATE registration_request SET status = ?, step = ? WHERE id = ?").params(status, step, processing.get(i)).update();
        }

        JsonNode summary = get("registrar", "/api/v1/registrations/batches/" + batchId);
        assertThat(summary.get("batchId").asText()).isEqualTo(batchId);
        assertThat(summary.get("total").asInt()).isEqualTo(50);
        assertThat(summary.get("completed").asLong()).isEqualTo(30);
        assertThat(summary.get("review").asLong()).isEqualTo(5);
        assertThat(summary.get("rejected").asLong()).isEqualTo(2);
        assertThat(summary.get("processing").asLong()).isEqualTo(7);
        assertThat(summary.get("duplicate").asLong()).isEqualTo(4);
        assertThat(summary.get("invalid").asLong()).isEqualTo(2);
        assertThat(summary.get("byStatus").get("COMPLETED").asLong()).isEqualTo(30);
        assertThat(summary.get("byStatus").get("REVIEW").asLong()).isEqualTo(5);
        assertThat(summary.get("byStatus").get("DUPLICATE").asLong()).isEqualTo(4);
        assertThat(summary.get("byStatus").get("INVALID").asLong()).isEqualTo(2);
        assertThat(summary.get("byStep").get("DONE").asLong()).isEqualTo(30);
        assertThat(summary.get("byStep").get("DISCOVERY").asLong()).isEqualTo(3);
        assertThat(summary.get("byStep").get("QUEUED").asLong()).isEqualTo(4);
        assertThat(summary.get("items")).hasSize(50);
        assertThat(summary.get("items").get(0).get("domain").asText()).isEqualTo("site01.example.org");

        // 요청자 본인과 ADMIN만 본다. 없는 묶음은 404
        assertThat(getRaw("viewer", "/api/v1/registrations/batches/" + batchId).getStatusCode()).isEqualTo(HttpStatus.NOT_FOUND);
        assertThat(get("admin", "/api/v1/registrations/batches/" + batchId).get("total").asInt()).isEqualTo(50);
        assertThat(getRaw("admin", "/api/v1/registrations/batches/nope").getStatusCode()).isEqualTo(HttpStatus.NOT_FOUND);

        // 상한 500건, 빈 목록은 400
        List<String> tooMany = new ArrayList<>();
        for (int i = 0; i < 501; i++) {
            tooMany.add("https://many%03d.example.org".formatted(i));
        }
        assertThat(post("registrar", "/api/v1/registrations/bulk", "{\"urls\":" + toJsonArray(tooMany) + "}").getStatusCode())
                .isEqualTo(HttpStatus.BAD_REQUEST);
        assertThat(post("registrar", "/api/v1/registrations/bulk", "{\"urls\":[]}").getStatusCode())
                .isEqualTo(HttpStatus.BAD_REQUEST);
        assertThat(count("SELECT count(*) FROM registration_request")).isEqualTo(52);
    }

    @Test
    void bulkAcceptsPlainTextOneUrlPerLineWithComments() {
        String text = """
                # 대외협력 모니터링 목록
                https://t1.example.org        # 부처 보도자료

                t2.example.org 협회 공지
                https://t1.example.org/news/1 # 같은 사이트 (중복)
                """;
        ResponseEntity<String> res = call("admin", HttpMethod.POST, "/api/v1/registrations/bulk", text, MediaType.TEXT_PLAIN);
        assertThat(res.getStatusCode()).isEqualTo(HttpStatus.ACCEPTED);
        JsonNode result = json(res);
        assertThat(result.get("total").asInt()).isEqualTo(3);
        assertThat(result.get("accepted").asInt()).isEqualTo(2);
        assertThat(result.get("duplicate").asInt()).isEqualTo(1);
        assertThat(result.get("items").findValuesAsText("url")).containsExactly("https://t1.example.org", "t2.example.org",
                "https://t1.example.org/news/1");
        assertThat(count("SELECT count(*) FROM source")).isEqualTo(2);

        // 빈 본문은 400
        assertThat(call("admin", HttpMethod.POST, "/api/v1/registrations/bulk", "# 주석뿐\n", MediaType.TEXT_PLAIN).getStatusCode())
                .isEqualTo(HttpStatus.BAD_REQUEST);
    }

    private static String toJsonArray(List<String> values) {
        StringBuilder sb = new StringBuilder("[");
        for (int i = 0; i < values.size(); i++) {
            if (i > 0) {
                sb.append(',');
            }
            sb.append('"').append(values.get(i).replace("\"", "\\\"")).append('"');
        }
        return sb.append(']').toString();
    }

    // ------------------------------------------------------------------ 2. 운영 지표

    /** 워커가 만들어 둔 소스를 흉내 낸다. */
    private long source(String domain, String status, int intervalMinutes, String lastSuccessAgo) {
        long siteId = jdbc.sql("INSERT INTO site (domain, base_url) VALUES (?, ?) RETURNING id")
                .params(domain, "https://" + domain).query(Long.class).single();
        // lastSuccessAgo가 null이면 now() - NULL = NULL (한 번도 성공한 적 없음)
        return jdbc.sql("""
                INSERT INTO source (site_id, name, status, interval_minutes, last_success_at, next_run_at)
                VALUES (?, ?, ?, ?, now() - CAST(? AS interval), now())
                RETURNING id
                """).params(siteId, domain, status, intervalMinutes, lastSuccessAgo).query(Long.class).single();
    }

    /** startedAgo는 created_at에도 쓴다 (대기 작업은 그때 만들어져 아직 시작하지 않은 것). finishedAgo가 null이면 끝나지 않은 작업. */
    private long job(long sourceId, String type, String status, String lockedBy, String startedAgo, String finishedAgo,
                     int found, int newCount) {
        return jdbc.sql("""
                INSERT INTO crawl_job (source_id, job_type, status, locked_by, created_at, started_at, finished_at, found_count, new_count)
                VALUES (?, ?, ?, ?,
                        now() - COALESCE(CAST(? AS interval), interval '0 seconds'),
                        CASE WHEN ? = 'QUEUED' THEN NULL ELSE now() - CAST(? AS interval) END,
                        now() - CAST(? AS interval), ?, ?)
                RETURNING id
                """).params(sourceId, type, status, lockedBy, startedAgo, status, startedAgo, finishedAgo, found, newCount)
                .query(Long.class).single();
    }

    private void error(long jobId, String code, String createdAgo) {
        jdbc.sql("INSERT INTO crawl_error (job_id, stage, code, created_at) VALUES (?, 'LIST', ?, now() - CAST(? AS interval))")
                .params(jobId, code, createdAgo).update();
    }

    @Test
    void opsOverviewReturnsEverySection() {
        long busy = source("busy.example.org", "ACTIVE", 30, "1 minute");
        long calm = source("calm.example.org", "ACTIVE", 60, "2 minutes");
        long broken = source("broken.example.org", "BROKEN", 60, "3 hours");
        long review = source("review.example.org", "REVIEW", 60, null);
        long pending = source("pending.example.org", "PENDING", 60, null);
        source("blocked.example.org", "BLOCKED", 60, "2 days");

        job(pending, "ONBOARD", "QUEUED", null, "10 minutes", null, 0, 0);            // 10분째 대기
        job(review, "ONBOARD", "RUNNING", "worker-1", "30 seconds", null, 0, 0);       // 실행 중
        long ok1 = job(busy, "CRAWL", "SUCCESS", "worker-1", "2 minutes", "1 minute", 20, 5);
        long ok2 = job(calm, "CRAWL", "SUCCESS", "worker-2", "3 minutes", "2 minutes", 10, 3);
        long part = job(busy, "CRAWL", "PARTIAL", "worker-1", "2 hours 1 minute", "2 hours", 15, 1);
        long failed = job(broken, "CRAWL", "FAILED", "worker-3", "3 hours 1 minute", "3 hours", 0, 0);
        job(calm, "CRAWL", "SUCCESS", "worker-9", "2 days 1 minute", "2 days", 10, 7);  // 24시간 밖: 결과·수집량에 안 들어간다
        error(failed, "PARSE_EMPTY", "3 hours");
        error(part, "BOT_CHALLENGE", "2 hours");
        error(ok1, "BOT_CHALLENGE", "1 minute");
        error(ok2, "TIMEOUT", "2 days");                                                // 24시간 밖

        HttpHeaders viewer = new HttpHeaders();
        viewer.set("X-User", "viewer");
        assertThat(http.exchange("/api/v1/ops/overview", HttpMethod.GET, new HttpEntity<>(viewer), String.class).getStatusCode())
                .isEqualTo(HttpStatus.FORBIDDEN);

        JsonNode o = get("admin", "/api/v1/ops/overview");
        assertThat(o.get("generatedAt").isNull()).isFalse();

        // 소스 상태별 (모든 상태가 0이라도 나온다)
        JsonNode sources = o.get("sources");
        assertThat(sources.get("ACTIVE").asLong()).isEqualTo(2);
        assertThat(sources.get("BROKEN").asLong()).isEqualTo(1);
        assertThat(sources.get("REVIEW").asLong()).isEqualTo(1);
        assertThat(sources.get("PENDING").asLong()).isEqualTo(1);
        assertThat(sources.get("BLOCKED").asLong()).isEqualTo(1);
        assertThat(sources.get("PAUSED").asLong()).isZero();
        assertThat(sources.get("EXCLUDED").asLong()).isZero();

        // 처리 대기 큐 종류별 (review-queue/counts와 같다)
        assertThat(o.get("reviewQueue").toString())
                .isEqualTo("{\"APPROVAL\":0,\"API_KEY\":0,\"REVIEW\":1,\"BROKEN\":1,\"BLOCKED\":1}");

        // 작업 큐 적체: 대기 1(10분째), 실행 1, 최근 5분 안에 일한 워커 2 (worker-1·worker-2; worker-3은 3시간 전)
        JsonNode q = o.get("jobQueue");
        assertThat(q.get("queued").asLong()).isEqualTo(1);
        assertThat(q.get("oldestQueuedWaitSeconds").asLong()).isBetween(590L, 700L);
        assertThat(q.get("running").asLong()).isEqualTo(1);
        assertThat(q.get("workers").asLong()).isEqualTo(2);

        // 최근 24시간 결과
        JsonNode j = o.get("jobs24h");
        assertThat(j.get("success").asLong()).isEqualTo(2);
        assertThat(j.get("partial").asLong()).isEqualTo(1);
        assertThat(j.get("failed").asLong()).isEqualTo(1);
        assertThat(j.get("avgDurationSeconds").asDouble()).isBetween(59.0, 61.0);

        // 최근 24시간 수집량: 5 + 3 + 1 = 9, 소스 상위는 busy(6) → calm(3)
        JsonNode d = o.get("documents24h");
        assertThat(d.get("newDocuments").asLong()).isEqualTo(9);
        assertThat(d.get("topSources").findValuesAsText("domain")).containsExactly("busy.example.org", "calm.example.org");
        assertThat(d.get("topSources").get(0).get("count").asLong()).isEqualTo(6);
        assertThat(d.get("topSources").get(0).get("sourceId").asLong()).isEqualTo(busy);

        // 실패 코드 상위 (24시간 안)
        assertThat(o.get("topErrors24h").toString())
                .isEqualTo("[{\"code\":\"BOT_CHALLENGE\",\"count\":2},{\"code\":\"PARSE_EMPTY\",\"count\":1}]");

        // 처리량: ACTIVE 두 곳의 시간당 예정 실행 60/30 + 60/60 = 3, 최근 1시간 완료 2 → 1.5 → 워커 부족
        JsonNode t = o.get("throughput");
        assertThat(t.get("scheduledRunsPerHour").asDouble()).isEqualTo(3.0);
        assertThat(t.get("completedLastHour").asLong()).isEqualTo(2);
        assertThat(t.get("demandRatio").asDouble()).isEqualTo(1.5);
        assertThat(t.get("workersShort").asBoolean()).isTrue();

        // 완료가 넉넉하면 비율이 1 아래로 내려가고 부족 판정이 풀린다
        for (int i = 0; i < 4; i++) {
            job(calm, "CRAWL", "SUCCESS", "worker-2", "20 minutes", "19 minutes", 1, 0);
        }
        t = get("admin", "/api/v1/ops/overview").get("throughput");
        assertThat(t.get("completedLastHour").asLong()).isEqualTo(6);
        assertThat(t.get("demandRatio").asDouble()).isEqualTo(0.5);
        assertThat(t.get("workersShort").asBoolean()).isFalse();
    }

    @Test
    void sourceHealthListsSilentSourcesFirstAndPutLocksInterval() {
        long never = source("never.example.org", "ACTIVE", 60, null);
        long stale = source("stale.example.org", "ACTIVE", 60, "10 days");
        long paused = source("paused.example.org", "PAUSED", 60, "1 day");
        long fresh = source("fresh.example.org", "ACTIVE", 60, "10 minutes");
        source("excluded.example.org", "EXCLUDED", 60, "1 hour");
        job(fresh, "CRAWL", "SUCCESS", "w", "40 minutes", "39 minutes", 10, 1);
        job(fresh, "CRAWL", "SUCCESS", "w", "25 minutes", "24 minutes", 20, 2);
        long last = job(fresh, "CRAWL", "PARTIAL", "w", "11 minutes", "10 minutes", 30, 3);
        jdbc.sql("UPDATE crawl_job SET note = '주기 자동 조정 60→120분 (최근 10회 평균 새 문서 0.2건)' WHERE id = ?").param(last).update();
        job(stale, "CRAWL", "FAILED", "w", "1 hour", "59 minutes", 0, 0);
        jdbc.sql("UPDATE source SET consecutive_failures = 4, status_reason = NULL WHERE id = ?").param(stale).update();

        HttpHeaders viewer = new HttpHeaders();
        viewer.set("X-User", "viewer");
        assertThat(http.exchange("/api/v1/ops/sources/health", HttpMethod.GET, new HttpEntity<>(viewer), String.class)
                .getStatusCode()).isEqualTo(HttpStatus.FORBIDDEN);

        // 마지막 성공이 없는 것 → 오래된 순. EXCLUDED는 빠진다
        JsonNode health = get("admin", "/api/v1/ops/sources/health");
        assertThat(health.findValuesAsText("domain")).containsExactly("never.example.org", "stale.example.org",
                "paused.example.org", "fresh.example.org");
        JsonNode n = health.get(0);
        assertThat(n.get("id").asLong()).isEqualTo(never);
        assertThat(n.get("lastSuccessAt").isNull()).isTrue();
        assertThat(n.get("avgFound").isNull()).isTrue();
        assertThat(n.get("lastJobStatus").isNull()).isTrue();
        assertThat(n.get("overdue").asBoolean()).isTrue();
        JsonNode s = health.get(1);
        assertThat(s.get("id").asLong()).isEqualTo(stale);
        assertThat(s.get("consecutiveFailures").asInt()).isEqualTo(4);
        assertThat(s.get("lastJobStatus").asText()).isEqualTo("FAILED");
        assertThat(s.get("overdue").asBoolean()).isTrue();
        assertThat(health.get(2).get("overdue").asBoolean()).isFalse();                 // PAUSED는 멈춘 것이 정상
        JsonNode f = health.get(3);
        assertThat(f.get("id").asLong()).isEqualTo(fresh);
        assertThat(f.get("avgFound").asDouble()).isEqualTo(20.0);
        assertThat(f.get("avgNew").asDouble()).isEqualTo(2.0);
        assertThat(f.get("lastJobStatus").asText()).isEqualTo("PARTIAL");
        assertThat(f.get("overdue").asBoolean()).isFalse();
        assertThat(f.get("intervalLocked").asBoolean()).isFalse();

        assertThat(get("admin", "/api/v1/ops/sources/health?status=active").findValuesAsText("domain"))
                .containsExactly("never.example.org", "stale.example.org", "fresh.example.org");
        assertThat(get("admin", "/api/v1/ops/sources/health?limit=1")).hasSize(1);

        // 소스 상세에 자동 조정 메모와 잠금 여부가 보인다
        JsonNode detail = get("admin", "/api/v1/sources/" + fresh);
        assertThat(detail.get("source").get("intervalLocked").asBoolean()).isFalse();
        assertThat(detail.get("jobs").get(0).get("note").asText()).startsWith("주기 자동 조정 60→120분");
        assertThat(detail.get("jobs").get(1).get("note").isNull()).isTrue();

        // 사람이 PUT으로 주기를 정하면 잠긴다 (워커의 자동 조정이 덮지 않는다). 다음 실행도 새 주기 안으로 당겨진다
        assertThat(put("viewer", "/api/v1/sources/" + fresh, "{\"intervalMinutes\":120}").getStatusCode()).isEqualTo(HttpStatus.FORBIDDEN);
        jdbc.sql("UPDATE source SET next_run_at = now() + interval '10 hours' WHERE id = ?").param(fresh).update();
        ResponseEntity<String> updated = put("admin", "/api/v1/sources/" + fresh, "{\"intervalMinutes\":120}");
        assertThat(updated.getStatusCode()).isEqualTo(HttpStatus.OK);
        assertThat(updated.getBody()).contains("\"intervalMinutes\":120").contains("\"intervalLocked\":true");
        assertThat(jdbc.sql("SELECT interval_minutes FROM source WHERE id = ?").param(fresh).query(Integer.class).single()).isEqualTo(120);
        assertThat(jdbc.sql("SELECT interval_locked FROM source WHERE id = ?").param(fresh).query(Boolean.class).single()).isTrue();
        assertThat(jdbc.sql("SELECT next_run_at <= now() + interval '121 minutes' FROM source WHERE id = ?").param(fresh)
                .query(Boolean.class).single()).isTrue();
        assertThat(get("admin", "/api/v1/ops/sources/health").get(3).get("intervalLocked").asBoolean()).isTrue();

        // 잠금만 풀 수도 있다 (값은 그대로, 다음 실행부터 자동 조정의 기준)
        assertThat(put("admin", "/api/v1/sources/" + fresh, "{\"intervalLocked\":false}").getBody())
                .contains("\"intervalMinutes\":120").contains("\"intervalLocked\":false");
        // 범위 밖·빈 요청은 400
        assertThat(put("admin", "/api/v1/sources/" + fresh, "{\"intervalMinutes\":1}").getStatusCode()).isEqualTo(HttpStatus.BAD_REQUEST);
        assertThat(put("admin", "/api/v1/sources/" + fresh, "{}").getStatusCode()).isEqualTo(HttpStatus.BAD_REQUEST);
        assertThat(put("admin", "/api/v1/sources/999999", "{\"intervalMinutes\":60}").getStatusCode()).isEqualTo(HttpStatus.NOT_FOUND);
        assertThat(jdbc.sql("SELECT string_agg(action, ',' ORDER BY id) FROM audit_log").query(String.class).single())
                .isEqualTo("SOURCE_UPDATED,SOURCE_UPDATED");
    }
}
