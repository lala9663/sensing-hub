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
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;

import io.sensinghub.caidentia.mock.MockCaidentiaStore;

/**
 * 케이던시아 연동 (M3, 목업): 공급사 마스터 동기화(생성·그대로·보관·복구, external_id → 사업자번호 매칭), 리스크 이벤트 전달(HIGH·MEDIUM만,
 * pushed_at, 재전송 없음), 상태 API와 권한. 가짜 케이던시아는 같은 서버의 /mock/caidentia/api 이고 연동 코드는 HTTP로 그것을 부른다.
 * 실제 PostgreSQL이 필요하며 SENSING_IT_DB_URL이 있을 때만 실행된다. 틀린 키는 {@link CaidentiaAuthFailureIT}.
 */
@EnabledIfEnvironmentVariable(named = "SENSING_IT_DB_URL", matches = "jdbc:postgresql:.+")
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT,
        properties = {"sensing.scheduler.enabled=false", "sensing.digest.enabled=false", "sensing.caidentia.enabled=false"})
class CaidentiaIT {

    static final String MOCK_KEY = "mock-key";

    @DynamicPropertySource
    static void db(DynamicPropertyRegistry r) {
        r.add("spring.datasource.url", () -> System.getenv("SENSING_IT_DB_URL"));
        r.add("spring.flyway.clean-disabled", () -> "false");
    }

    @Autowired TestRestTemplate http;
    @Autowired JdbcClient jdbc;
    @Autowired ObjectMapper json;
    @Autowired MockCaidentiaStore mock;

    @BeforeEach
    void clean() {
        jdbc.sql("""
                TRUNCATE caidentia_sync_run, digest_run, digest_subscription, chat_feedback, chat_citation, chat_message, chat_session,
                         chunk, attachment, document_version, document_watch, document_enrichment, document_company, company_signal,
                         company, document, crawl_error, crawl_job, registration_request, audit_log, api_credential
                RESTART IDENTITY CASCADE
                """).update();
        jdbc.sql("UPDATE source SET active_config_version_id = NULL").update();
        jdbc.sql("TRUNCATE source_config_version, source, site RESTART IDENTITY CASCADE").update();
        mock.reset();
    }

    // ------------------------------------------------------------------ HTTP 도우미

    private ResponseEntity<String> call(String user, HttpMethod method, String path, String body) {
        HttpHeaders h = new HttpHeaders();
        if (body != null) {
            h.setContentType(MediaType.APPLICATION_JSON);
        }
        h.set("X-User", user);
        return http.exchange(path, method, new HttpEntity<>(body, h), String.class);
    }

    private JsonNode post(String user, String path) {
        ResponseEntity<String> res = call(user, HttpMethod.POST, path, null);
        assertThat(res.getStatusCode()).as(res.getBody()).isEqualTo(HttpStatus.OK);
        return read(res.getBody());
    }

    private JsonNode get(String user, String path) {
        ResponseEntity<String> res = call(user, HttpMethod.GET, path, null);
        assertThat(res.getStatusCode()).as(res.getBody()).isEqualTo(HttpStatus.OK);
        return read(res.getBody());
    }

    /** 가짜 케이던시아를 "케이던시아 쪽 사람"처럼 직접 부른다 (데모의 PUT). */
    private ResponseEntity<String> mockCall(String key, HttpMethod method, String path, String body) {
        HttpHeaders h = new HttpHeaders();
        if (body != null) {
            h.setContentType(MediaType.APPLICATION_JSON);
        }
        if (key != null) {
            h.set("X-Caidentia-Key", key);
        }
        return http.exchange("/mock/caidentia/api" + path, method, new HttpEntity<>(body, h), String.class);
    }

    private JsonNode read(String body) {
        try {
            return json.readTree(body);
        } catch (IOException e) {
            throw new IllegalStateException(e);
        }
    }

    private String one(String sql, Object... params) {
        return jdbc.sql(sql).params(params).query(String.class).single();
    }

    private long count(String sql, Object... params) {
        return jdbc.sql(sql).params(params).query(Long.class).single();
    }

    private static String counts(JsonNode run) {
        return run.get("status").asText() + "|" + run.get("fetched").asInt() + "|" + run.get("created").asInt() + "|"
                + run.get("updated").asInt() + "|" + run.get("archived").asInt() + "|" + run.get("unchanged").asInt();
    }

    // ------------------------------------------------------------------ 테스트

    @Test
    void syncCreatesCompaniesThenReportsUnchangedThenArchivesAndRevives() {
        // 권한: 상태·동기화·전달은 ADMIN만
        assertThat(call("viewer", HttpMethod.GET, "/api/v1/integrations/caidentia", null).getStatusCode()).isEqualTo(HttpStatus.FORBIDDEN);
        assertThat(call("registrar", HttpMethod.POST, "/api/v1/integrations/caidentia/sync-now", null).getStatusCode())
                .isEqualTo(HttpStatus.FORBIDDEN);
        assertThat(call("registrar", HttpMethod.POST, "/api/v1/integrations/caidentia/push-now", null).getStatusCode())
                .isEqualTo(HttpStatus.FORBIDDEN);

        // 처음: 상태에 동기화 기록 없음, 주소는 목업(임의 포트가 실제 포트로 바뀌어 있다)
        JsonNode before = get("admin", "/api/v1/integrations/caidentia");
        assertThat(before.get("mock").asBoolean()).isTrue();
        assertThat(before.get("baseUrl").asText()).contains("/mock/caidentia/api").doesNotContain(":0/");
        assertThat(before.get("lastSync").isNull()).isTrue();
        assertThat(before.get("runs")).isEmpty();

        // 첫 동기화: 씨앗 공급사 9건 → 회사 9건 생성 (감시 9개 + 점검 소스 4개)
        JsonNode first = post("admin", "/api/v1/integrations/caidentia/sync-now");
        assertThat(counts(first)).as(first.toString()).isEqualTo("SUCCESS|9|9|0|0|0");
        assertThat(first.get("trigger").asText()).isEqualTo("MANUAL");
        assertThat(first.get("error").isNull()).isTrue();
        assertThat(first.get("finishedAt").isNull()).isFalse();
        assertThat(count("SELECT count(*) FROM company WHERE status = 'ACTIVE'")).isEqualTo(9L);
        assertThat(count("SELECT count(*) FROM source WHERE kind = 'WATCH'")).isEqualTo(9L);
        assertThat(count("SELECT count(*) FROM source WHERE kind = 'CHECK'")).isEqualTo(4L);
        assertThat(count("SELECT count(*) FROM company WHERE external_id IS NULL OR watch_source_id IS NULL")).isZero();
        // 필드 매핑: supplierId → external_id, bizRegNo(하이픈) → biz_no 10자리, dartCorpCode → corp_code, tier+category → tags
        assertThat(one("SELECT external_id || '|' || biz_no || '|' || corp_code || '|' || aliases::text || '|' || tags::text || '|' || status "
                + "FROM company WHERE name = '나부품'"))
                .isEqualTo("SUP-0001|1234567890|00123456|[\"NABU\", \"나부품(주)\"]|[\"1차\", \"전장부품\"]|ACTIVE");
        assertThat(one("SELECT external_id || '|' || biz_no || '|' || coalesce(corp_code, '-') || '|' || tags::text FROM company WHERE name = '다정밀'"))
                .isEqualTo("SUP-0003|2018100001|-|[\"2차\", \"정밀가공\"]");
        assertThat(one("SELECT v.config->>'keywords' FROM source_config_version v JOIN source s ON s.active_config_version_id = v.id "
                + "JOIN company c ON c.watch_source_id = s.id WHERE c.name = '나부품'")).isEqualTo("[\"나부품\", \"NABU\", \"나부품(주)\"]");
        assertThat(one("SELECT u.login_id FROM company c JOIN app_user u ON u.id = c.created_by WHERE c.name = '나부품'")).isEqualTo("admin");
        assertThat(get("viewer", "/api/v1/companies").get(0).get("externalId").asText()).startsWith("SUP-");
        assertThat(count("SELECT count(*) FROM audit_log WHERE action = 'CAIDENTIA_SYNC'")).isEqualTo(1L);

        // 두 번째 동기화: 모두 그대로
        JsonNode second = post("admin", "/api/v1/integrations/caidentia/sync-now");
        assertThat(counts(second)).isEqualTo("SUCCESS|9|0|0|0|9");
        assertThat(count("SELECT count(*) FROM company")).isEqualTo(9L);
        assertThat(count("SELECT count(*) FROM source_config_version")).isEqualTo(13L);   // 감시 설정이 새 버전으로 바뀌지 않았다

        // 케이던시아 쪽에서 다정밀을 INACTIVE로, 가전자의 이름을 바꿈 (틀린 키·없는 키는 401, 없는 공급사는 404)
        // (401 검사는 본문 없는 GET으로: JDK HttpURLConnection은 본문을 스트리밍하다 401을 받으면 예외를 낸다)
        assertThat(mockCall("nope", HttpMethod.GET, "/suppliers", null).getStatusCode()).isEqualTo(HttpStatus.UNAUTHORIZED);
        assertThat(mockCall("nope", HttpMethod.GET, "/risk-events", null).getStatusCode()).isEqualTo(HttpStatus.UNAUTHORIZED);
        assertThat(mockCall(null, HttpMethod.GET, "/suppliers", null).getStatusCode()).isEqualTo(HttpStatus.UNAUTHORIZED);
        assertThat(mockCall(MOCK_KEY, HttpMethod.PUT, "/suppliers/SUP-9999", "{\"status\":\"INACTIVE\"}").getStatusCode())
                .isEqualTo(HttpStatus.NOT_FOUND);
        assertThat(mockCall(MOCK_KEY, HttpMethod.PUT, "/suppliers/SUP-0003", "{\"status\":\"BOGUS\"}").getStatusCode())
                .isEqualTo(HttpStatus.BAD_REQUEST);
        ResponseEntity<String> inactive = mockCall(MOCK_KEY, HttpMethod.PUT, "/suppliers/SUP-0003", "{\"status\":\"INACTIVE\"}");
        assertThat(inactive.getStatusCode()).as(inactive.getBody()).isEqualTo(HttpStatus.OK);
        assertThat(read(inactive.getBody()).get("status").asText()).isEqualTo("INACTIVE");
        assertThat(read(inactive.getBody()).get("name").asText()).isEqualTo("다정밀");   // 준 필드만 바뀐다
        assertThat(mockCall(MOCK_KEY, HttpMethod.PUT, "/suppliers/SUP-0002", "{\"name\":\"가전자 주식회사\",\"tier\":\"2차\"}").getStatusCode())
                .isEqualTo(HttpStatus.OK);
        // updatedSince로 바뀐 것만 받을 수도 있다
        assertThat(read(mockCall(MOCK_KEY, HttpMethod.GET, "/suppliers?updatedSince=2026-10-02T00:00:00Z", null).getBody())).hasSize(2);
        assertThat(read(mockCall(MOCK_KEY, HttpMethod.GET, "/suppliers", null).getBody())).hasSize(9);

        JsonNode third = post("admin", "/api/v1/integrations/caidentia/sync-now");
        assertThat(counts(third)).as(third.toString()).isEqualTo("SUCCESS|9|0|1|1|7");
        long da = jdbc.sql("SELECT id FROM company WHERE external_id = 'SUP-0003'").query(Long.class).single();
        assertThat(one("SELECT c.status || '|' || s.status || '|' || s.status_reason FROM company c JOIN source s ON s.id = c.watch_source_id "
                + "WHERE c.id = ?", da)).isEqualTo("ARCHIVED|EXCLUDED|회사 보관: 다정밀");
        assertThat(one("SELECT name || '|' || tags::text FROM company WHERE external_id = 'SUP-0002'")).isEqualTo("가전자 주식회사|[\"2차\", \"반도체\"]");
        assertThat(one("SELECT v.version || '|' || (v.config->>'keywords') FROM source_config_version v JOIN source s ON s.active_config_version_id = v.id "
                + "JOIN company c ON c.watch_source_id = s.id WHERE c.external_id = 'SUP-0002'")).isEqualTo("2|[\"가전자 주식회사\", \"GA Electronics\"]");
        assertThat(get("viewer", "/api/v1/companies")).hasSize(8);

        // 다시 INACTIVE인 채로 동기화하면 이미 보관된 회사는 그대로, 다시 ACTIVE가 되면 복구(갱신)
        assertThat(counts(post("admin", "/api/v1/integrations/caidentia/sync-now"))).isEqualTo("SUCCESS|9|0|0|0|9");
        mockCall(MOCK_KEY, HttpMethod.PUT, "/suppliers/SUP-0003", "{\"status\":\"ACTIVE\"}");
        assertThat(counts(post("admin", "/api/v1/integrations/caidentia/sync-now"))).isEqualTo("SUCCESS|9|0|1|0|8");
        assertThat(one("SELECT c.status || '|' || s.status FROM company c JOIN source s ON s.id = c.watch_source_id WHERE c.id = ?", da))
                .isEqualTo("ACTIVE|PENDING");
        assertThat(count("SELECT count(*) FROM company")).isEqualTo(9L);

        // 상태: 마지막 동기화와 기록 5건 (최근 순)
        JsonNode status = get("admin", "/api/v1/integrations/caidentia");
        assertThat(status.get("lastSync").get("id").asLong()).isEqualTo(5L);
        assertThat(status.get("runs")).hasSize(5);
        assertThat(status.get("runs").get(0).get("id").asLong()).isEqualTo(5L);
        assertThat(status.get("runs").get(4).get("created").asInt()).isEqualTo(9);
        assertThat(status.get("pushedCount").asLong()).isZero();
        assertThat(status.get("pendingCount").asLong()).isZero();
        assertThat(status.get("syncCron").asText()).isEqualTo("0 0 6 * * *");
        assertThat(status.get("pushEnabled").asBoolean()).isTrue();
    }

    @Test
    void syncMatchesByBizNoWhenCompanyHasNoExternalId() {
        // CSV 등으로 먼저 넣어 둔 회사(외부 ID 없음)는 사업자번호로 맞춰 갱신되고 외부 ID가 채워진다
        ResponseEntity<String> created = call("registrar", HttpMethod.POST, "/api/v1/companies",
                "{\"name\":\"자포장(구)\",\"bizNo\":\"801-81-00007\",\"tags\":[\"핵심\"]}");
        assertThat(created.getStatusCode()).as(created.getBody()).isEqualTo(HttpStatus.CREATED);
        long id = read(created.getBody()).get("id").asLong();

        JsonNode run = post("admin", "/api/v1/integrations/caidentia/sync-now");
        assertThat(counts(run)).as(run.toString()).isEqualTo("SUCCESS|9|8|1|0|0");
        assertThat(count("SELECT count(*) FROM company")).isEqualTo(9L);
        assertThat(one("SELECT name || '|' || external_id || '|' || tags::text FROM company WHERE id = ?", id)).isEqualTo("자포장|SUP-0009|[\"2차\", \"포장\"]");

        // 외부 ID가 같으면 사업자번호가 바뀌어도 그 회사를 갱신한다 (external_id 우선)
        mockCall(MOCK_KEY, HttpMethod.PUT, "/suppliers/SUP-0009", "{\"bizRegNo\":\"801-81-00008\"}");
        assertThat(counts(post("admin", "/api/v1/integrations/caidentia/sync-now"))).isEqualTo("SUCCESS|9|0|1|0|8");
        assertThat(one("SELECT biz_no FROM company WHERE id = ?", id)).isEqualTo("8018100008");

        // 다른 회사가 쓰는 사업자번호를 주면 그 건만 실패로 기록하고 나머지는 반영된다
        mockCall(MOCK_KEY, HttpMethod.PUT, "/suppliers/SUP-0009", "{\"bizRegNo\":\"123-45-67890\"}");
        JsonNode conflict = post("admin", "/api/v1/integrations/caidentia/sync-now");
        assertThat(counts(conflict)).as(conflict.toString()).isEqualTo("FAILED|9|0|0|0|8");
        assertThat(conflict.get("error").asText()).contains("SUP-0009").contains("사업자등록번호");
        assertThat(one("SELECT biz_no FROM company WHERE id = ?", id)).isEqualTo("8018100008");
    }

    @Test
    void pushSendsHighAndMediumSignalsOfCaidentiaCompaniesOnce() {
        post("admin", "/api/v1/integrations/caidentia/sync-now");
        long nabu = jdbc.sql("SELECT id FROM company WHERE external_id = 'SUP-0001'").query(Long.class).single();
        ResponseEntity<String> other = call("registrar", HttpMethod.POST, "/api/v1/companies", "{\"name\":\"무소속\"}");
        long outsider = read(other.getBody()).get("id").asLong();
        // 워커가 남겼을 신호: 나부품 HIGH·MEDIUM·LOW, 케이던시아가 모르는 회사(외부 ID 없음)의 HIGH
        jdbc.sql("""
                INSERT INTO company_signal (company_id, kind, severity, fingerprint, title, detail, observed_at)
                VALUES (?, 'BIZ_STATUS', 'HIGH', '0320260930', '폐업자 (폐업일 2026-09-30)', '{"b_stt_cd":"03"}', '2026-10-16T09:00:00+09:00'),
                       (?, 'NEWS', 'MEDIUM', 'hash1', '나부품 공장 화재', '{"topics":["사건·사고"]}', '2026-10-16T07:00:00+09:00'),
                       (?, 'BIZ_STATUS', 'LOW', 'tax:01', '과세유형 변경', '{}', '2026-10-15T09:00:00+09:00'),
                       (?, 'DEBARMENT', 'HIGH', 'd1', '입찰참가자격 제한', '{}', '2026-10-16T08:00:00+09:00')
                """).params(nabu, nabu, nabu, outsider).update();
        JsonNode before = get("admin", "/api/v1/integrations/caidentia");
        assertThat(before.get("pendingCount").asLong()).isEqualTo(2L);
        assertThat(get("admin", "/api/v1/integrations/caidentia/received-events")).isEmpty();

        JsonNode pushed = post("admin", "/api/v1/integrations/caidentia/push-now");
        assertThat(pushed.get("pushed").asInt()).as(pushed.toString()).isEqualTo(2);
        assertThat(pushed.get("failed").asInt()).isZero();
        assertThat(pushed.get("pending").asLong()).isZero();
        assertThat(pushed.get("error").isNull()).isTrue();
        assertThat(one("SELECT string_agg(severity || ':' || (pushed_at IS NOT NULL)::text, ',' ORDER BY id) FROM company_signal"))
                .isEqualTo("HIGH:true,MEDIUM:true,LOW:false,HIGH:false");

        // 케이던시아가 받은 것: 오래된 신호(id 순)부터, 우리 필드 → 이벤트 필드
        JsonNode events = get("admin", "/api/v1/integrations/caidentia/received-events");
        assertThat(events).hasSize(2);
        JsonNode e = events.get(0);
        assertThat(e.get("eventId").asText()).isEqualTo("EVT-1");
        assertThat(e.get("supplierId").asText()).isEqualTo("SUP-0001");
        assertThat(e.get("companyName").asText()).isEqualTo("나부품");
        assertThat(e.get("kind").asText()).isEqualTo("BIZ_STATUS");
        assertThat(e.get("severity").asText()).isEqualTo("HIGH");
        assertThat(e.get("title").asText()).isEqualTo("폐업자 (폐업일 2026-09-30)");
        assertThat(e.get("detail").get("b_stt_cd").asText()).isEqualTo("03");
        assertThat(e.get("observedAt").asText()).startsWith("2026-10-16");
        assertThat(e.get("receivedAt").isNull()).isFalse();
        assertThat(events.get(1).get("kind").asText()).isEqualTo("NEWS");
        assertThat(read(mockCall(MOCK_KEY, HttpMethod.GET, "/risk-events", null).getBody())).hasSize(2);

        // 다시 보내도 같은 신호는 두 번 가지 않는다
        assertThat(post("admin", "/api/v1/integrations/caidentia/push-now").get("pushed").asInt()).isZero();
        assertThat(get("admin", "/api/v1/integrations/caidentia/received-events")).hasSize(2);
        JsonNode after = get("admin", "/api/v1/integrations/caidentia");
        assertThat(after.get("pushedCount").asLong()).isEqualTo(2L);
        assertThat(after.get("pendingCount").asLong()).isZero();

        // 새 신호가 생기면 그것만 간다
        jdbc.sql("""
                INSERT INTO company_signal (company_id, kind, severity, fingerprint, title, observed_at)
                VALUES (?, 'DISCLOSURE', 'HIGH', 'rcept1', '회생절차 개시신청', '2026-10-17T09:00:00+09:00')
                """).param(nabu).update();
        assertThat(post("admin", "/api/v1/integrations/caidentia/push-now").get("pushed").asInt()).isEqualTo(1);
        assertThat(get("admin", "/api/v1/integrations/caidentia/received-events").get(2).get("kind").asText()).isEqualTo("DISCLOSURE");
    }
}
