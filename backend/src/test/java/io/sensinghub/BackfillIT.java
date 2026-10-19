package io.sensinghub;

import static org.assertj.core.api.Assertions.assertThat;

import java.time.LocalDate;

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

import io.sensinghub.backfill.BackfillService;
import io.sensinghub.scheduler.CrawlScheduler;

/**
 * 과거 기사 채우기(백필) API·스케줄러 양보. 워커가 하는 일(묶음 실행)은 SQL로 흉내 낸다.
 * 실제 PostgreSQL이 필요하며 SENSING_IT_DB_URL이 있을 때만 실행된다.
 */
@EnabledIfEnvironmentVariable(named = "SENSING_IT_DB_URL", matches = "jdbc:postgresql:.+")
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT,
        properties = {"sensing.scheduler.enabled=false", "sensing.digest.enabled=false", "sensing.caidentia.enabled=false"})
class BackfillIT {

    @DynamicPropertySource
    static void db(DynamicPropertyRegistry r) {
        r.add("spring.datasource.url", () -> System.getenv("SENSING_IT_DB_URL"));
        r.add("spring.flyway.clean-disabled", () -> "false");
    }

    @Autowired TestRestTemplate http;
    @Autowired JdbcClient jdbc;

    private final ObjectMapper mapper = new ObjectMapper();

    @BeforeEach
    void clean() {
        jdbc.sql("""
                TRUNCATE backfill_url, backfill, chunk, attachment, document_version, document_watch, document_enrichment,
                         document_company, document, crawl_error, crawl_job, registration_request, audit_log
                RESTART IDENTITY CASCADE
                """).update();
        jdbc.sql("UPDATE source SET active_config_version_id = NULL").update();
        jdbc.sql("TRUNCATE source_config_version, source, site RESTART IDENTITY CASCADE").update();
    }

    // ------------------------------------------------------------------ 도우미

    private ResponseEntity<String> post(String user, String path, String json) {
        HttpHeaders h = new HttpHeaders();
        h.set("X-User", user);
        h.setContentType(MediaType.APPLICATION_JSON);
        return http.exchange(path, HttpMethod.POST, new HttpEntity<>(json, h), String.class);
    }

    private ResponseEntity<String> get(String user, String path) {
        HttpHeaders h = new HttpHeaders();
        h.set("X-User", user);
        return http.exchange(path, HttpMethod.GET, new HttpEntity<>(h), String.class);
    }

    private JsonNode json(ResponseEntity<String> res) {
        try {
            return mapper.readTree(res.getBody());
        } catch (java.io.IOException e) {
            throw new IllegalStateException(e);
        }
    }

    private long count(String sql, Object... params) {
        return jdbc.sql(sql).params(params).query(Long.class).single();
    }

    /** ACTIVE 사이트 소스와 활성 설정 (워커가 등록을 끝낸 모습). next_run_at은 한 시간 뒤. */
    private long siteSource(String domain, String status, String kind, String method) {
        long siteId = jdbc.sql("INSERT INTO site (domain, base_url, site_type) VALUES (?, ?, 'NEWS') RETURNING id")
                .params(domain, "https://www." + domain).query(Long.class).single();
        long sourceId = jdbc.sql("""
                INSERT INTO source (site_id, name, status, kind, method, interval_minutes, next_run_at)
                VALUES (?, ?, ?, ?, ?, 60, now() + interval '1 hour') RETURNING id
                """).params(siteId, domain, status, kind, method).query(Long.class).single();
        long configId = jdbc.sql("""
                INSERT INTO source_config_version (source_id, version, config, status)
                VALUES (?, 1, CAST(? AS jsonb), 'ACTIVE') RETURNING id
                """).params(sourceId, "{\"method\":\"" + method + "\",\"hubs\":[\"https://www." + domain + "/\"],"
                        + "\"article_pattern\":{\"path\":\"^/news/\\\\d+$\",\"signature\":\"/news/{n}\"}}")
                .query(Long.class).single();
        jdbc.sql("UPDATE source SET active_config_version_id = ? WHERE id = ?").params(configId, sourceId).update();
        return sourceId;
    }

    // ------------------------------------------------------------------ 요청·검증

    @Test
    void requestDefaultsToLastThreeMonthsAndShowsProgress() {
        long id = siteSource("donga.example", "ACTIVE", "SITE", "SITEMAP");
        ResponseEntity<String> res = post("registrar", "/api/v1/sources/" + id + "/backfills", "{}");
        assertThat(res.getStatusCode()).isEqualTo(HttpStatus.ACCEPTED);
        JsonNode b = json(res);
        LocalDate today = LocalDate.now(BackfillService.KST);
        assertThat(b.get("to").asText()).isEqualTo(today.toString());
        assertThat(b.get("from").asText()).isEqualTo(today.minusMonths(3).toString());
        assertThat(b.get("status").asText()).isEqualTo("QUEUED");
        assertThat(count("SELECT count(*) FROM audit_log WHERE action = 'BACKFILL_REQUESTED'")).isEqualTo(1);

        // 워커가 주소를 찾고 몇 건을 처리한 모습
        long bid = b.get("id").asLong();
        jdbc.sql("""
                INSERT INTO backfill_url (backfill_id, url_hash, url, status)
                SELECT ?, md5(g::text) || md5(g::text), 'https://www.donga.example/news/' || g,
                       CASE WHEN g <= 3 THEN 'SAVED' WHEN g = 4 THEN 'SKIPPED' ELSE 'PENDING' END
                FROM generate_series(1, 10) g
                """).param(bid).update();
        jdbc.sql("UPDATE backfill SET status = 'RUNNING', method = 'SITEMAP', found_count = 10, saved_count = 3, "
                + "skipped_count = 1, request_count = 7, chunk_count = 1 WHERE id = ?").param(bid).update();

        JsonNode list = json(get("registrar", "/api/v1/sources/" + id + "/backfills"));
        assertThat(list).hasSize(1);
        JsonNode row = list.get(0);
        assertThat(row.get("status").asText()).isEqualTo("RUNNING");
        assertThat(row.get("found").asInt()).isEqualTo(10);
        assertThat(row.get("saved").asInt()).isEqualTo(3);
        assertThat(row.get("skipped").asInt()).isEqualTo(1);
        assertThat(row.get("remaining").asLong()).isEqualTo(6);
        assertThat(row.get("requestedBy").asText()).isNotBlank();

        // 소스당 진행 중인 채우기는 하나
        ResponseEntity<String> dup = post("admin", "/api/v1/sources/" + id + "/backfills", "{}");
        assertThat(dup.getStatusCode()).isEqualTo(HttpStatus.CONFLICT);
        assertThat(json(dup).get("message").asText()).contains("#" + bid);
    }

    @Test
    void periodIsValidated() {
        long id = siteSource("hani.example", "ACTIVE", "SITE", "STATIC");
        LocalDate today = LocalDate.now(BackfillService.KST);
        String path = "/api/v1/sources/" + id + "/backfills";
        assertThat(post("registrar", path, "{\"from\":\"" + today.minusYears(2) + "\",\"to\":\"" + today + "\"}")
                .getStatusCode()).isEqualTo(HttpStatus.BAD_REQUEST);                       // 1년 넘음
        assertThat(post("registrar", path, "{\"to\":\"" + today.plusDays(1) + "\"}").getStatusCode())
                .isEqualTo(HttpStatus.BAD_REQUEST);                                         // 미래
        assertThat(post("registrar", path, "{\"from\":\"" + today + "\",\"to\":\"" + today.minusDays(5) + "\"}")
                .getStatusCode()).isEqualTo(HttpStatus.BAD_REQUEST);                       // 거꾸로
        assertThat(post("registrar", path, "{\"from\":\"2026/07/01\"}").getStatusCode())
                .isEqualTo(HttpStatus.BAD_REQUEST);                                         // 형식
        // 정확히 1년은 된다
        ResponseEntity<String> ok = post("registrar", path,
                "{\"from\":\"" + today.minusYears(1) + "\",\"to\":\"" + today + "\"}");
        assertThat(ok.getStatusCode()).isEqualTo(HttpStatus.ACCEPTED);
        assertThat(post("viewer", "/api/v1/sources/" + id + "/backfills", "{}").getStatusCode())
                .isEqualTo(HttpStatus.FORBIDDEN);
        assertThat(get("viewer", path).getStatusCode()).isEqualTo(HttpStatus.FORBIDDEN);
    }

    @Test
    void onlyActiveSiteSourcesCanBeBackfilled() {
        long paused = siteSource("paused.example", "PAUSED", "SITE", "STATIC");
        long api = siteSource("dart.example", "ACTIVE", "SITE", "API");
        long watch = siteSource("watch.example", "ACTIVE", "WATCH", "API");
        for (long id : new long[] {paused, api, watch}) {
            assertThat(post("admin", "/api/v1/sources/" + id + "/backfills", "{}").getStatusCode())
                    .isEqualTo(HttpStatus.CONFLICT);
        }
        assertThat(post("admin", "/api/v1/sources/9999/backfills", "{}").getStatusCode()).isEqualTo(HttpStatus.NOT_FOUND);
    }

    @Test
    void cancelStopsBackfillAndClosesQueuedChunk() {
        long id = siteSource("chosun.example", "ACTIVE", "SITE", "SITEMAP");
        long bid = json(post("registrar", "/api/v1/sources/" + id + "/backfills", "{}")).get("id").asLong();
        long jobId = jdbc.sql("INSERT INTO crawl_job (source_id, job_type, trigger, backfill_id) VALUES (?, 'BACKFILL', 'MANUAL', ?) RETURNING id")
                .params(id, bid).query(Long.class).single();

        ResponseEntity<String> res = post("registrar", "/api/v1/sources/" + id + "/backfills/" + bid + "/cancel", null);
        assertThat(res.getStatusCode()).isEqualTo(HttpStatus.OK);
        assertThat(json(res).get("status").asText()).isEqualTo("CANCELLED");
        assertThat(jdbc.sql("SELECT status FROM crawl_job WHERE id = ?").param(jobId).query(String.class).single())
                .isEqualTo("SUCCESS");
        // 두 번 취소하면 409, 다른 소스 주소로는 404
        assertThat(post("registrar", "/api/v1/sources/" + id + "/backfills/" + bid + "/cancel", null).getStatusCode())
                .isEqualTo(HttpStatus.CONFLICT);
        long other = siteSource("other.example", "ACTIVE", "SITE", "STATIC");
        assertThat(post("registrar", "/api/v1/sources/" + other + "/backfills/" + bid + "/cancel", null).getStatusCode())
                .isEqualTo(HttpStatus.NOT_FOUND);
        // 취소한 뒤에는 다시 요청할 수 있다
        assertThat(post("registrar", "/api/v1/sources/" + id + "/backfills", "{}").getStatusCode())
                .isEqualTo(HttpStatus.ACCEPTED);
    }

    // ------------------------------------------------------------------ 평소 수집에 양보

    @Test
    void schedulerKeepsSourceDueWhileBackfillChunkRunsAndReplacesQueuedChunk() {
        CrawlScheduler scheduler = new CrawlScheduler(jdbc);   // 테스트에서는 스케줄러 빈을 끈다
        long running = siteSource("running.example", "ACTIVE", "SITE", "SITEMAP");
        long queued = siteSource("queued.example", "ACTIVE", "SITE", "SITEMAP");
        jdbc.sql("UPDATE source SET next_run_at = now() - interval '1 minute'").update();
        jdbc.sql("INSERT INTO crawl_job (source_id, job_type, trigger, status, started_at) VALUES (?, 'BACKFILL', 'MANUAL', 'RUNNING', now())")
                .param(running).update();
        jdbc.sql("INSERT INTO crawl_job (source_id, job_type, trigger) VALUES (?, 'BACKFILL', 'MANUAL')").param(queued).update();

        scheduler.enqueueDueSources();

        // 실행 중인 묶음: CRAWL은 못 넣었지만 차례를 미루지 않는다 (워커가 보고 양보하면 다음 틱에 들어간다)
        assertThat(count("SELECT count(*) FROM crawl_job WHERE source_id = ? AND job_type = 'CRAWL'", running)).isZero();
        assertThat(count("SELECT count(*) FROM source WHERE id = ? AND next_run_at <= now()", running)).isEqualTo(1);
        // 대기 중인 묶음: 거두고 CRAWL을 넣는다
        assertThat(jdbc.sql("SELECT status FROM crawl_job WHERE source_id = ? AND job_type = 'BACKFILL'").param(queued)
                .query(String.class).single()).isEqualTo("SUCCESS");
        assertThat(count("SELECT count(*) FROM crawl_job WHERE source_id = ? AND job_type = 'CRAWL' AND status = 'QUEUED'",
                queued)).isEqualTo(1);
        assertThat(count("SELECT count(*) FROM source WHERE id = ? AND next_run_at > now()", queued)).isEqualTo(1);

        // 워커가 묶음을 멈춘 뒤 다음 틱
        jdbc.sql("UPDATE crawl_job SET status = 'SUCCESS', finished_at = now() WHERE source_id = ?").param(running).update();
        scheduler.enqueueDueSources();
        assertThat(count("SELECT count(*) FROM crawl_job WHERE source_id = ? AND job_type = 'CRAWL'", running)).isEqualTo(1);
    }

    @Test
    void manualRunDuringBackfillChunkPullsTheCrawlForward() {
        long id = siteSource("run.example", "ACTIVE", "SITE", "SITEMAP");
        jdbc.sql("INSERT INTO crawl_job (source_id, job_type, trigger, status, started_at) VALUES (?, 'BACKFILL', 'MANUAL', 'RUNNING', now())")
                .param(id).update();
        ResponseEntity<String> res = post("admin", "/api/v1/sources/" + id + "/actions/run", null);
        assertThat(res.getStatusCode()).isEqualTo(HttpStatus.OK);
        assertThat(count("SELECT count(*) FROM source WHERE id = ? AND next_run_at <= now()", id)).isEqualTo(1);
        // 다시 탐색(ONBOARD)은 채우기를 취소한 뒤에
        assertThat(post("admin", "/api/v1/sources/" + id + "/actions/rediscover", null).getStatusCode())
                .isEqualTo(HttpStatus.CONFLICT);

        // 대기 중인 묶음이면 거두고 바로 넣는다
        jdbc.sql("UPDATE crawl_job SET status = 'QUEUED', started_at = NULL WHERE source_id = ?").param(id).update();
        assertThat(post("admin", "/api/v1/sources/" + id + "/actions/run", null).getStatusCode()).isEqualTo(HttpStatus.OK);
        assertThat(count("SELECT count(*) FROM crawl_job WHERE source_id = ? AND job_type = 'CRAWL' AND trigger = 'MANUAL'", id))
                .isEqualTo(1);
    }
}
