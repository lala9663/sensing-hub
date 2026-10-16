package io.sensinghub;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import java.io.IOException;
import java.io.InputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.time.ZonedDateTime;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.atomic.AtomicInteger;

import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;
import org.mockito.ArgumentCaptor;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.mock.mockito.MockBean;
import org.springframework.boot.test.web.client.TestRestTemplate;
import org.springframework.http.HttpEntity;
import org.springframework.http.HttpHeaders;
import org.springframework.http.HttpMethod;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.mail.javamail.JavaMailSender;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpServer;

import io.sensinghub.audit.AuditLogger;
import io.sensinghub.auth.CurrentUser;
import io.sensinghub.auth.Role;
import io.sensinghub.digest.DigestComposer;
import io.sensinghub.digest.DigestScheduler;
import io.sensinghub.digest.DigestSender;
import io.sensinghub.digest.DigestService;
import jakarta.mail.Session;
import jakarta.mail.internet.MimeMessage;

/**
 * 일간 다이제스트: 구독 CRUD·권한, 본문 묶음, 웹훅·메일 전송과 digest_run 기록, 스케줄러.
 * 실제 PostgreSQL(+pgvector)이 필요하며 SENSING_IT_DB_URL이 있을 때만 실행된다. 외부 메일 서버·웹훅은 부르지 않는다
 * (메일은 JavaMailSender 목, 웹훅은 테스트 안의 HttpServer).
 */
@EnabledIfEnvironmentVariable(named = "SENSING_IT_DB_URL", matches = "jdbc:postgresql:.+")
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT,
        properties = {"sensing.scheduler.enabled=false", "sensing.digest.enabled=false",
                      "spring.mail.host=smtp.example.test", "sensing.digest.from=sensing-hub@example.test"})
class DigestIT {

    @DynamicPropertySource
    static void db(DynamicPropertyRegistry r) {
        r.add("spring.datasource.url", () -> System.getenv("SENSING_IT_DB_URL"));
        r.add("spring.flyway.clean-disabled", () -> "false");
    }

    @Autowired TestRestTemplate http;
    @Autowired JdbcClient jdbc;
    @Autowired ObjectMapper json;
    @Autowired AuditLogger audit;
    @Autowired DigestService service;
    @Autowired DigestComposer composer;
    @MockBean JavaMailSender mailSender;

    private HttpServer hook;
    private final List<String> hookBodies = new ArrayList<>();
    private final AtomicInteger hookStatus = new AtomicInteger(200);

    @BeforeEach
    void clean() throws IOException {
        jdbc.sql("""
                TRUNCATE digest_run, digest_subscription, chat_feedback, chat_citation, chat_message, chat_session, chunk,
                         attachment, document_version, document_watch, document_enrichment, document, crawl_error,
                         crawl_job, registration_request, audit_log, api_credential
                RESTART IDENTITY CASCADE
                """).update();
        jdbc.sql("UPDATE source SET active_config_version_id = NULL").update();
        jdbc.sql("TRUNCATE source_config_version, source, site RESTART IDENTITY CASCADE").update();
        when(mailSender.createMimeMessage()).thenReturn(new MimeMessage((Session) null));

        hookBodies.clear();
        hookStatus.set(200);
        hook = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        hook.createContext("/hook", ex -> {
            try (InputStream in = ex.getRequestBody()) {
                hookBodies.add(ex.getRequestMethod() + " " + ex.getRequestHeaders().getFirst("Content-Type") + " "
                        + new String(in.readAllBytes(), StandardCharsets.UTF_8));
            }
            ex.sendResponseHeaders(hookStatus.get(), -1);
            ex.close();
        });
        hook.start();
    }

    @AfterEach
    void stopHook() {
        hook.stop(0);
    }

    private String hookUrl() {
        return "http://127.0.0.1:" + hook.getAddress().getPort() + "/hook";
    }

    // ------------------------------------------------------------------ HTTP 도우미

    private ResponseEntity<String> call(String user, HttpMethod method, String path, String body) {
        HttpHeaders h = new HttpHeaders();
        h.setContentType(MediaType.APPLICATION_JSON);
        h.set("X-User", user);
        return http.exchange(path, method, new HttpEntity<>(body, h), String.class);
    }

    private ResponseEntity<String> post(String user, String path, String body) {
        return call(user, HttpMethod.POST, path, body);
    }

    private JsonNode get(String user, String path) {
        ResponseEntity<String> res = call(user, HttpMethod.GET, path, null);
        assertThat(res.getStatusCode()).isEqualTo(HttpStatus.OK);
        return read(res.getBody());
    }

    private JsonNode read(String body) {
        try {
            return json.readTree(body);
        } catch (IOException e) {
            throw new IllegalStateException(e);
        }
    }

    /** 구독을 만들고 id를 돌려준다. 웹훅 구독은 검증(내부망 거부)을 지나기 위해 공개 주소로 만든 뒤 DB에서 테스트 서버로 바꾼다. */
    private long subscribe(String user, String name, String channel, String target, String filtersJson) {
        ResponseEntity<String> res = post(user, "/api/v1/digests", "{\"name\":\"" + name + "\",\"channel\":\"" + channel
                + "\",\"target\":\"" + (channel.equals("WEBHOOK") ? "https://hooks.example.com/services/T/B/x" : target)
                + "\",\"hourKst\":8" + (filtersJson == null ? "" : ",\"filters\":" + filtersJson) + "}");
        assertThat(res.getStatusCode()).as(res.getBody()).isEqualTo(HttpStatus.CREATED);
        long id = read(res.getBody()).get("id").asLong();
        if (channel.equals("WEBHOOK")) {
            jdbc.sql("UPDATE digest_subscription SET target = ? WHERE id = ?").params(target, id).update();
        }
        return id;
    }

    // ------------------------------------------------------------------ 자료 도우미 (RegistrationFlowIT와 같은 모양)

    private long siteSource(String domain, String siteType) {
        long siteId = jdbc.sql("INSERT INTO site (domain, base_url, site_type) VALUES (?, ?, ?) RETURNING id")
                .params(domain, "https://" + domain, siteType).query(Long.class).single();
        return jdbc.sql("INSERT INTO source (site_id, name, status) VALUES (?, ?, 'ACTIVE') RETURNING id")
                .params(siteId, domain).query(Long.class).single();
    }

    private long watch(long siteSourceId, String name) {
        return jdbc.sql("INSERT INTO source (site_id, name, kind, watch_name, method, status, requested_by) "
                + "SELECT site_id, ?, 'WATCH', ?, 'API', 'ACTIVE', 2 FROM source WHERE id = ? RETURNING id")
                .params("watch:" + name, name, siteSourceId).query(Long.class).single();
    }

    private long document(long sourceId, String url, String title, String publishedAt, String collectedAt, String body) {
        return jdbc.sql("""
                INSERT INTO document (source_id, url, url_hash, title, published_at, collected_at, body_text)
                VALUES (?, ?, md5(?) || md5(?), ?, CAST(? AS timestamptz), CAST(? AS timestamptz), ?) RETURNING id
                """).params(sourceId, url, url, url, title, publishedAt, collectedAt, body).query(Long.class).single();
    }

    private void enrichment(long documentId, String summary, String topics) {
        jdbc.sql("""
                INSERT INTO document_enrichment (document_id, document_version, summary, topics, tags, model)
                VALUES (?, 1, ?, CAST(? AS jsonb), '[]'::jsonb, 'fake fake-1')
                """).params(documentId, summary, topics).update();
    }

    private void hit(long documentId, long watchId, String keyword) {
        jdbc.sql("INSERT INTO document_watch (document_id, source_id, keyword) VALUES (?, ?, ?)")
                .params(documentId, watchId, keyword).update();
    }

    /** 표본: 공공기관 공고(요약 있음) + 언론 기사 2건(감시에 걸림, 요약 없음) + 보강 전 1건. 모두 최근 1시간 안에 수집. */
    private record Sample(long gov, long press, long watchOwn, long watchRival, long notice, long article, long rival,
                          long raw) {
    }

    private Sample sample() {
        long gov = siteSource("gov.example.go.kr", "PUBLIC");
        long press = siteSource("news.example.com", "NEWS");
        long own = watch(press, "자사 보도");
        long rivalWatch = watch(press, "경쟁사");
        String recent = ZonedDateTime.now(DigestComposer.KST).minusMinutes(30).truncatedTo(java.time.temporal.ChronoUnit.SECONDS)
                .toOffsetDateTime().toString();
        long notice = document(gov, "https://gov.example.go.kr/view/1", "2026년 지원사업 공고",
                "2026-10-14T09:00:00+09:00", recent, "공고 본문입니다.");
        long article = document(press, "https://news.example.com/articleView?idxno=1", "메타넷, 신제품 공개",
                "2026-10-16T09:00:00+09:00", recent, null);
        long rival = document(press, "https://news.example.com/articleView?idxno=2", "A사, 메타넷 추격",
                "2026-10-16T08:30:00+09:00", recent, null);
        long raw = document(press, "https://news.example.com/articleView?idxno=3", "아직 보강 전", null, recent, null);
        enrichment(notice, "중소벤처기업부가 10월 31일까지 지원사업 신청을 받는다.", "[\"지원사업\",\"정책·규제\"]");
        enrichment(article, null, "[\"자사\",\"산업동향\"]");                        // 언론 기사: 요약은 저장하지 않는다
        enrichment(rival, null, "[\"경쟁사\"]");
        hit(article, own, "메타넷");
        hit(rival, own, "메타넷");
        hit(rival, rivalWatch, "A사");
        return new Sample(gov, press, own, rivalWatch, notice, article, rival, raw);
    }

    // ------------------------------------------------------------------ 테스트

    @Test
    void subscriptionCrudAndOwnership() {
        ResponseEntity<String> created = post("viewer", "/api/v1/digests",
                "{\"name\":\" 아침  보고 \",\"channel\":\"email\",\"target\":\"viewer@example.com\",\"hourKst\":7,"
                        + "\"filters\":{\"topics\":[\"정책·규제\",\"정책·규제\"]}}");
        assertThat(created.getStatusCode()).as(created.getBody()).isEqualTo(HttpStatus.CREATED);
        JsonNode sub = read(created.getBody());
        long id = sub.get("id").asLong();
        assertThat(sub.get("name").asText()).isEqualTo("아침 보고");
        assertThat(sub.get("channel").asText()).isEqualTo("EMAIL");
        assertThat(sub.get("hourKst").asInt()).isEqualTo(7);
        assertThat(sub.get("enabled").asBoolean()).isTrue();
        assertThat(sub.get("lastSentAt").isNull()).isTrue();
        assertThat(sub.get("owner").asText()).isEqualTo("일반 사용자");
        assertThat(sub.get("filters").toString()).isEqualTo("{\"watchIds\":[],\"sourceIds\":[],\"topics\":[\"정책·규제\"]}");
        assertThat(jdbc.sql("SELECT filters::text FROM digest_subscription WHERE id = ?").param(id).query(String.class).single())
                .isEqualTo("{\"topics\": [\"정책·규제\"], \"watch_ids\": [], \"source_ids\": []}");

        // 기본값: hourKst 8, filters 전체
        JsonNode defaults = read(post("registrar", "/api/v1/digests",
                "{\"name\":\"웹훅\",\"channel\":\"WEBHOOK\",\"target\":\"https://hooks.example.com/services/T/B/x\"}").getBody());
        assertThat(defaults.get("hourKst").asInt()).isEqualTo(8);
        assertThat(defaults.get("filters").toString()).isEqualTo("{\"watchIds\":[],\"sourceIds\":[],\"topics\":[]}");

        // 검증: 메일 형식, 웹훅은 절대 http(s) URL, 내부망 주소 거부, 시각 범위, 체계 밖 토픽, 없는 감시 id
        for (String body : List.of(
                "{\"name\":\"x\",\"channel\":\"EMAIL\",\"target\":\"not-an-address\"}",
                "{\"name\":\"x\",\"channel\":\"WEBHOOK\",\"target\":\"hooks.example.com/x\"}",
                "{\"name\":\"x\",\"channel\":\"WEBHOOK\",\"target\":\"ftp://hooks.example.com/x\"}",
                "{\"name\":\"x\",\"channel\":\"WEBHOOK\",\"target\":\"http://localhost:8080/hook\"}",
                "{\"name\":\"x\",\"channel\":\"WEBHOOK\",\"target\":\"http://10.0.0.5/hook\"}",
                "{\"name\":\"x\",\"channel\":\"WEBHOOK\",\"target\":\"http://169.254.169.254/latest\"}",
                "{\"name\":\"x\",\"channel\":\"SMS\",\"target\":\"010\"}",
                "{\"name\":\"x\",\"channel\":\"EMAIL\",\"target\":\"a@b.co\",\"hourKst\":24}",
                "{\"name\":\"x\",\"channel\":\"EMAIL\",\"target\":\"a@b.co\",\"filters\":{\"topics\":[\"없는토픽\"]}}",
                "{\"name\":\"x\",\"channel\":\"EMAIL\",\"target\":\"a@b.co\",\"filters\":{\"watchIds\":[999]}}",
                "{\"name\":\" \",\"channel\":\"EMAIL\",\"target\":\"a@b.co\"}")) {
            assertThat(post("viewer", "/api/v1/digests", body).getStatusCode()).as(body).isEqualTo(HttpStatus.BAD_REQUEST);
        }
        assertThat(jdbc.sql("SELECT count(*) FROM digest_subscription").query(Long.class).single()).isEqualTo(2L);

        // 목록: 내 것만, ADMIN은 전체. 남의 구독은 조회·수정·삭제·미리보기 모두 404
        assertThat(get("viewer", "/api/v1/digests")).hasSize(1);
        assertThat(get("registrar", "/api/v1/digests")).hasSize(1);
        assertThat(get("admin", "/api/v1/digests")).hasSize(2);
        assertThat(call("registrar", HttpMethod.GET, "/api/v1/digests/" + id, null).getStatusCode()).isEqualTo(HttpStatus.NOT_FOUND);
        assertThat(call("registrar", HttpMethod.PUT, "/api/v1/digests/" + id, "{\"name\":\"훔치기\"}").getStatusCode())
                .isEqualTo(HttpStatus.NOT_FOUND);
        assertThat(call("registrar", HttpMethod.DELETE, "/api/v1/digests/" + id, null).getStatusCode()).isEqualTo(HttpStatus.NOT_FOUND);
        assertThat(post("registrar", "/api/v1/digests/" + id + "/preview", "").getStatusCode()).isEqualTo(HttpStatus.NOT_FOUND);
        assertThat(post("registrar", "/api/v1/digests/" + id + "/send-now", "").getStatusCode()).isEqualTo(HttpStatus.NOT_FOUND);
        assertThat(call("admin", HttpMethod.GET, "/api/v1/digests/" + id, null).getStatusCode()).isEqualTo(HttpStatus.OK);

        // 수정: 생략한 필드는 그대로, 채널을 바꾸면 target도 새 채널로 검사한다
        ResponseEntity<String> updated = call("viewer", HttpMethod.PUT, "/api/v1/digests/" + id,
                "{\"hourKst\":9,\"enabled\":false,\"filters\":{\"topics\":[\"입찰·공고\"]}}");
        assertThat(updated.getStatusCode()).as(updated.getBody()).isEqualTo(HttpStatus.OK);
        JsonNode u = read(updated.getBody());
        assertThat(u.get("name").asText()).isEqualTo("아침 보고");
        assertThat(u.get("target").asText()).isEqualTo("viewer@example.com");
        assertThat(u.get("hourKst").asInt()).isEqualTo(9);
        assertThat(u.get("enabled").asBoolean()).isFalse();
        assertThat(u.get("filters").get("topics").toString()).isEqualTo("[\"입찰·공고\"]");
        assertThat(call("viewer", HttpMethod.PUT, "/api/v1/digests/" + id, "{\"channel\":\"WEBHOOK\"}").getStatusCode())
                .isEqualTo(HttpStatus.BAD_REQUEST);                                     // 메일 주소는 웹훅 URL이 아니다
        assertThat(call("viewer", HttpMethod.PUT, "/api/v1/digests/" + id,
                "{\"channel\":\"WEBHOOK\",\"target\":\"https://hooks.example.com/services/T/B/y\"}").getStatusCode())
                .isEqualTo(HttpStatus.OK);

        // 삭제
        assertThat(call("viewer", HttpMethod.DELETE, "/api/v1/digests/" + id, null).getStatusCode()).isEqualTo(HttpStatus.NO_CONTENT);
        assertThat(call("viewer", HttpMethod.GET, "/api/v1/digests/" + id, null).getStatusCode()).isEqualTo(HttpStatus.NOT_FOUND);
        assertThat(get("admin", "/api/v1/digests")).hasSize(1);
        assertThat(jdbc.sql("SELECT string_agg(action, ',' ORDER BY id) FROM audit_log").query(String.class).single())
                .contains("DIGEST_SUBSCRIBED").contains("DIGEST_UPDATED").contains("DIGEST_UNSUBSCRIBED");
    }

    @Test
    void previewGroupsByWatchAndTopicWithoutSending() {
        Sample s = sample();
        long all = subscribe("viewer", "전체", "EMAIL", "viewer@example.com", null);

        ResponseEntity<String> res = post("viewer", "/api/v1/digests/" + all + "/preview", "");
        assertThat(res.getStatusCode()).as(res.getBody()).isEqualTo(HttpStatus.OK);
        JsonNode p = read(res.getBody());
        assertThat(p.get("documentCount").asInt()).isEqualTo(4);
        assertThat(p.get("truncated").asBoolean()).isFalse();
        assertThat(p.get("subject").asText()).startsWith("[sensing-hub] 전체 다이제스트 ").endsWith("(4건)");
        String text = p.get("text").asText();
        // 감시(만든 순서) → 토픽(체계 순서) → 분류 전. 감시에 걸린 기사는 토픽 묶음에 다시 나오지 않는다
        int own = text.indexOf("*감시: 자사 보도*");
        int kw = text.indexOf("_메타넷_");
        int rival = text.indexOf("*감시: 경쟁사*");
        int policy = text.indexOf("*정책·규제*");
        int support = text.indexOf("*지원사업*");
        int unclassified = text.indexOf("*분류 전*");
        assertThat(List.of(own, kw, rival, policy, support, unclassified)).doesNotContain(-1).isSorted();
        assertThat(text).doesNotContain("*자사*").doesNotContain("*산업동향*");
        assertThat(text.substring(own, rival)).contains("메타넷, 신제품 공개").contains("A사, 메타넷 추격");
        assertThat(text.substring(rival, policy)).contains("_A사_").contains("A사, 메타넷 추격");
        assertThat(text.substring(policy, unclassified)).contains("2026년 지원사업 공고 — gov.example.go.kr · 2026-10-14 09:00 · "
                + "https://gov.example.go.kr/view/1\n  중소벤처기업부가 10월 31일까지 지원사업 신청을 받는다.");
        assertThat(text.substring(unclassified)).contains("아직 보강 전 — news.example.com · 발행일 없음");
        // 언론 기사에는 요약 줄이 없다 (API 조회처럼 summary가 있어도 다이제스트에는 워커가 저장한 값만; 여기서는 요약이 null인 rival로 확인)
        assertThat(text).doesNotContain("요약이 저장됐다면 안 됨\n").doesNotContain("A사, 메타넷 추격 —\n  ");
        assertThat(p.get("html").asText()).contains("<a href=\"https://news.example.com/articleView?idxno=1\">메타넷, 신제품 공개</a>")
                .contains("— sensing-hub 다이제스트");
        // 미리보기는 보내지도 기록하지도 않는다
        assertThat(jdbc.sql("SELECT count(*) FROM digest_run").query(Long.class).single()).isZero();
        verify(mailSender, never()).send(any(MimeMessage.class));

        // 필터는 합집합: 경쟁사 감시만 / 공공기관 소스만 / 토픽 자사(감시에 걸린 기사는 감시 묶음으로) / 감시 + 토픽
        long rivalOnly = subscribe("viewer", "경쟁사", "EMAIL", "viewer@example.com", "{\"watchIds\":[" + s.watchRival() + "]}");
        JsonNode r = read(post("viewer", "/api/v1/digests/" + rivalOnly + "/preview", "").getBody());
        assertThat(r.get("documentCount").asInt()).isEqualTo(1);
        assertThat(r.get("text").asText()).contains("*감시: 경쟁사*").doesNotContain("자사 보도").doesNotContain("지원사업 공고");

        long govOnly = subscribe("viewer", "공공", "EMAIL", "viewer@example.com", "{\"sourceIds\":[" + s.gov() + "]}");
        JsonNode g = read(post("viewer", "/api/v1/digests/" + govOnly + "/preview", "").getBody());
        assertThat(g.get("documentCount").asInt()).isEqualTo(1);
        assertThat(g.get("text").asText()).contains("*정책·규제*").contains("*지원사업*").doesNotContain("감시:");

        long mixed = subscribe("viewer", "섞음", "EMAIL", "viewer@example.com",
                "{\"watchIds\":[" + s.watchRival() + "],\"topics\":[\"지원사업\"]}");
        JsonNode m = read(post("viewer", "/api/v1/digests/" + mixed + "/preview", "").getBody());
        assertThat(m.get("documentCount").asInt()).isEqualTo(2);
        assertThat(m.get("text").asText()).contains("*감시: 경쟁사*").contains("*지원사업*").doesNotContain("*감시: 자사 보도*");

        // 기간은 last_sent_at 이후(collected_at). 모두 그 전에 수집됐으면 0건
        jdbc.sql("UPDATE digest_subscription SET last_sent_at = now() WHERE id = ?").param(all).update();
        assertThat(read(post("viewer", "/api/v1/digests/" + all + "/preview", "").getBody()).get("documentCount").asInt()).isZero();
    }

    @Test
    void sendNowPostsWebhookAndRecordsRun() {
        sample();
        long id = subscribe("registrar", "슬랙", "WEBHOOK", hookUrl(), null);

        ResponseEntity<String> res = post("registrar", "/api/v1/digests/" + id + "/send-now", "");
        assertThat(res.getStatusCode()).as(res.getBody()).isEqualTo(HttpStatus.OK);
        JsonNode run = read(res.getBody());
        assertThat(run.get("status").asText()).isEqualTo("SENT");
        assertThat(run.get("documentCount").asInt()).isEqualTo(4);
        assertThat(run.get("error").isNull()).isTrue();
        assertThat(run.get("bodyText").asText()).startsWith("*sensing-hub 일간 다이제스트 — 슬랙*");

        assertThat(hookBodies).hasSize(1);
        assertThat(hookBodies.get(0)).startsWith("POST application/json");
        JsonNode payload = read(hookBodies.get(0).substring(hookBodies.get(0).indexOf('{')));
        assertThat(payload.fieldNames()).toIterable().containsExactly("text");
        assertThat(payload.get("text").asText()).isEqualTo(run.get("bodyText").asText()).contains("*감시: 자사 보도*")
                .contains("https://gov.example.go.kr/view/1").endsWith("— sensing-hub 다이제스트\n");

        // digest_run SENT + body_text, last_sent_at = 기간 끝, 감사 로그
        assertThat(jdbc.sql("SELECT status || '|' || document_count || '|' || (body_text IS NOT NULL) FROM digest_run")
                .query(String.class).single()).isEqualTo("SENT|4|true");
        assertThat(jdbc.sql("SELECT (last_sent_at = (SELECT period_to FROM digest_run)) FROM digest_subscription WHERE id = ?")
                .param(id).query(Boolean.class).single()).isTrue();
        assertThat(jdbc.sql("SELECT action || detail::text FROM audit_log WHERE action = 'DIGEST_SENT' ORDER BY id DESC LIMIT 1")
                .query(String.class).single()).startsWith("DIGEST_SENT").contains("\"documents\": 4");
        JsonNode runs = get("registrar", "/api/v1/digests/" + id + "/runs");
        assertThat(runs).hasSize(1);
        assertThat(runs.get(0).get("bodyText").asText()).contains("메타넷, 신제품 공개");

        // 바로 다시 보내면 새 문서가 없어 EMPTY: 보내지 않고 기록만, last_sent_at은 앞으로
        JsonNode empty = read(post("registrar", "/api/v1/digests/" + id + "/send-now", "").getBody());
        assertThat(empty.get("status").asText()).isEqualTo("EMPTY");
        assertThat(empty.get("documentCount").asInt()).isZero();
        assertThat(hookBodies).hasSize(1);
        assertThat(jdbc.sql("SELECT (last_sent_at = (SELECT max(period_to) FROM digest_run)) FROM digest_subscription WHERE id = ?")
                .param(id).query(Boolean.class).single()).isTrue();

        // 서버가 500이면 FAILED, last_sent_at은 그대로(다음 틱에 다시), 본문은 남긴다
        jdbc.sql("UPDATE digest_subscription SET last_sent_at = now() - interval '1 day' WHERE id = ?").param(id).update();
        hookStatus.set(500);
        JsonNode failed = read(post("registrar", "/api/v1/digests/" + id + "/send-now", "").getBody());
        assertThat(failed.get("status").asText()).isEqualTo("FAILED");
        assertThat(failed.get("error").asText()).isEqualTo("웹훅 응답 500");
        assertThat(failed.get("documentCount").asInt()).isEqualTo(4);
        assertThat(jdbc.sql("SELECT (last_sent_at < now() - interval '23 hours') FROM digest_subscription WHERE id = ?")
                .param(id).query(Boolean.class).single()).isTrue();
        assertThat(jdbc.sql("SELECT count(*) FROM audit_log WHERE action = 'DIGEST_FAILED'").query(Long.class).single())
                .isEqualTo(1L);
    }

    @Test
    void sendNowMailsHtmlAndFailsWhenMailIsNotConfigured() throws Exception {
        sample();
        long id = subscribe("viewer", "아침 보고", "EMAIL", "viewer@example.com", null);

        JsonNode run = read(post("viewer", "/api/v1/digests/" + id + "/send-now", "").getBody());
        assertThat(run.get("status").asText()).isEqualTo("SENT");

        ArgumentCaptor<MimeMessage> sent = ArgumentCaptor.forClass(MimeMessage.class);
        verify(mailSender).send(sent.capture());
        MimeMessage message = sent.getValue();
        assertThat(message.getAllRecipients()).hasSize(1);
        assertThat(message.getAllRecipients()[0].toString()).isEqualTo("viewer@example.com");
        assertThat(message.getFrom()[0].toString()).isEqualTo("sensing-hub@example.test");
        assertThat(message.getSubject()).startsWith("[sensing-hub] 아침 보고 다이제스트 ").endsWith("(4건)");
        assertThat(message.getDataHandler().getContentType()).startsWith("text/html");
        String html = (String) message.getContent();
        assertThat(html).startsWith("<!DOCTYPE html>").contains("감시: 자사 보도")
                .contains("<a href=\"https://gov.example.go.kr/view/1\">2026년 지원사업 공고</a>")
                .contains("중소벤처기업부가 10월 31일까지").contains("— sensing-hub 다이제스트");
        assertThat(hookBodies).isEmpty();

        // 메일 서버가 설정되지 않았으면 FAILED로 남기고 예외는 삼킨다 (비밀번호는 어디에도 없다)
        DigestService unconfigured = new DigestService(jdbc, json, audit, composer,
                new DigestSender(mailSender, "", "", 10));
        jdbc.sql("UPDATE digest_subscription SET last_sent_at = now() - interval '1 day' WHERE id = ?").param(id).update();
        CurrentUser viewer = new CurrentUser(3, "viewer", "일반 사용자", Role.VIEWER);
        DigestService.RunView failed = unconfigured.sendNow(viewer, id);
        assertThat(failed.status()).isEqualTo("FAILED");
        assertThat(failed.error()).isEqualTo("메일 서버가 설정되지 않았습니다 (SENSING_MAIL_HOST).");
        assertThat(failed.documentCount()).isEqualTo(4);
        assertThat(jdbc.sql("SELECT string_agg(status, ',' ORDER BY id) FROM digest_run").query(String.class).single())
                .isEqualTo("SENT,FAILED");
        assertThat(jdbc.sql("SELECT string_agg(action, ',' ORDER BY id) FROM audit_log WHERE action LIKE 'DIGEST_%'")
                .query(String.class).single()).endsWith("DIGEST_SENT,DIGEST_FAILED");
        verify(mailSender).send(any(MimeMessage.class));   // 두 번째는 보내지 않았다
    }

    @Test
    void schedulerSendsDueSubscriptionsOnceADayAndIsolatesFailures() {
        sample();
        ZonedDateTime now = ZonedDateTime.now(DigestComposer.KST);
        long due = subscribe("registrar", "슬랙", "WEBHOOK", hookUrl(), null);
        long broken = subscribe("registrar", "고장", "WEBHOOK", hookUrl().replace("/hook", "/nothing"), null);   // 404 → FAILED
        long sentToday = subscribe("registrar", "오늘 보냄", "WEBHOOK", hookUrl(), null);
        long otherHour = subscribe("registrar", "다른 시각", "WEBHOOK", hookUrl(), null);
        long disabled = subscribe("registrar", "꺼짐", "WEBHOOK", hookUrl(), null);
        jdbc.sql("UPDATE digest_subscription SET hour_kst = ?, last_sent_at = ? - interval '1 day' WHERE id IN (?, ?, ?)")
                .params(now.getHour(), now.toOffsetDateTime(), due, broken, disabled).update();
        jdbc.sql("UPDATE digest_subscription SET hour_kst = ?, last_sent_at = ? WHERE id = ?")
                .params(now.getHour(), now.toLocalDate().atStartOfDay(DigestComposer.KST).toOffsetDateTime(), sentToday).update();
        jdbc.sql("UPDATE digest_subscription SET hour_kst = ?, last_sent_at = NULL WHERE id = ?")
                .params((now.getHour() + 1) % 24, otherHour).update();
        jdbc.sql("UPDATE digest_subscription SET enabled = false WHERE id = ?").param(disabled).update();

        DigestScheduler scheduler = new DigestScheduler(jdbc, service);
        assertThat(scheduler.tick(now)).isEqualTo(2);                       // due + broken
        assertThat(hookBodies).hasSize(1);
        assertThat(jdbc.sql("SELECT string_agg(subscription_id || ':' || status, ',' ORDER BY id) FROM digest_run")
                .query(String.class).single()).isEqualTo(due + ":SENT," + broken + ":FAILED");
        assertThat(jdbc.sql("SELECT (last_sent_at >= ?) FROM digest_subscription WHERE id = ?")
                .params(now.toOffsetDateTime(), due).query(Boolean.class).single()).isTrue();
        assertThat(jdbc.sql("SELECT (last_sent_at < now() - interval '23 hours') FROM digest_subscription WHERE id = ?")
                .param(broken).query(Boolean.class).single()).isTrue();      // 실패는 last_sent_at을 두어 다음 틱에 다시
        assertThat(jdbc.sql("SELECT count(*) FROM digest_run WHERE subscription_id IN (?, ?, ?)")
                .params(sentToday, otherHour, disabled).query(Long.class).single()).isZero();

        // 같은 틱이 다시 돌면 보낸 것은 오늘 보냈으니 건너뛰고 실패한 것만 다시 시도한다
        assertThat(scheduler.tick(now)).isEqualTo(1);
        assertThat(hookBodies).hasSize(1);
        assertThat(jdbc.sql("SELECT count(*) FROM digest_run WHERE subscription_id = ? AND status = 'FAILED'").param(broken)
                .query(Long.class).single()).isEqualTo(2L);
    }
}
