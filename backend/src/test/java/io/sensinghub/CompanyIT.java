package io.sensinghub;

import static org.assertj.core.api.Assertions.assertThat;

import java.io.IOException;
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
import com.fasterxml.jackson.databind.ObjectMapper;

/**
 * 회사(거래처) 마스터 (M3): 등록 시 회사 감시·점검 소스 생성, import upsert, 사업자번호 검증, 목록·상세·신호 API, 수정·보관.
 * 실제 PostgreSQL(+pgvector)이 필요하며 SENSING_IT_DB_URL이 있을 때만 실행된다. 외부 API는 부르지 않는다 (워커 몫).
 */
@EnabledIfEnvironmentVariable(named = "SENSING_IT_DB_URL", matches = "jdbc:postgresql:.+")
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT,
        properties = {"sensing.scheduler.enabled=false", "sensing.digest.enabled=false"})
class CompanyIT {

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
        if (type != null) {
            h.setContentType(type);
        }
        h.set("X-User", user);
        return http.exchange(path, method, new HttpEntity<>(body, h), String.class);
    }

    private ResponseEntity<String> post(String user, String path, String body) {
        return call(user, HttpMethod.POST, path, body, MediaType.APPLICATION_JSON);
    }

    private ResponseEntity<String> put(String user, String path, String body) {
        return call(user, HttpMethod.PUT, path, body, MediaType.APPLICATION_JSON);
    }

    private JsonNode get(String user, String path) {
        ResponseEntity<String> res = call(user, HttpMethod.GET, path, null, null);
        assertThat(res.getStatusCode()).as(res.getBody()).isEqualTo(HttpStatus.OK);
        return read(res.getBody());
    }

    private JsonNode read(String body) {
        try {
            return json.readTree(body);
        } catch (IOException e) {
            throw new IllegalStateException(e);
        }
    }

    private long createCompany(String body) {
        ResponseEntity<String> res = post("registrar", "/api/v1/companies", body);
        assertThat(res.getStatusCode()).as(res.getBody()).isEqualTo(HttpStatus.CREATED);
        return read(res.getBody()).get("id").asLong();
    }

    private String one(String sql, Object... params) {
        return jdbc.sql(sql).params(params).query(String.class).single();
    }

    private long count(String sql, Object... params) {
        return jdbc.sql(sql).params(params).query(Long.class).single();
    }

    // ------------------------------------------------------------------ 테스트

    @Test
    void registrarCreatesCompanyWithWatchAndCheckSource() {
        assertThat(post("viewer", "/api/v1/companies", "{\"name\":\"나부품\"}").getStatusCode()).isEqualTo(HttpStatus.FORBIDDEN);

        ResponseEntity<String> res = post("registrar", "/api/v1/companies",
                "{\"name\":\" 나부품 \",\"aliases\":[\"NABU\",\"nabu\",\" \",\"나부품\"],\"bizNo\":\"123-45-67890\","
                        + "\"corpCode\":\"00123456\",\"externalId\":\"CAI-001\",\"tags\":[\"1차 공급사\",\"핵심\"]}");
        assertThat(res.getStatusCode()).as(res.getBody()).isEqualTo(HttpStatus.CREATED);
        JsonNode c = read(res.getBody());
        long id = c.get("id").asLong();
        assertThat(c.get("name").asText()).isEqualTo("나부품");
        assertThat(c.get("aliases").toString()).isEqualTo("[\"NABU\",\"나부품\"]");       // 공백·대소문자 중복 제거
        assertThat(c.get("bizNo").asText()).isEqualTo("1234567890");                        // 하이픈 제거
        assertThat(c.get("corpCode").asText()).isEqualTo("00123456");
        assertThat(c.get("externalId").asText()).isEqualTo("CAI-001");
        assertThat(c.get("tags").toString()).isEqualTo("[\"1차 공급사\",\"핵심\"]");
        assertThat(c.get("status").asText()).isEqualTo("ACTIVE");
        assertThat(c.get("watchSourceId").isNull()).isFalse();
        assertThat(c.get("watchStatus").asText()).isEqualTo("PENDING");
        assertThat(c.get("lastBizStatus").isNull()).isTrue();
        assertThat(c.get("signalCount").asLong()).isZero();
        assertThat(c.get("documentCount").asLong()).isZero();
        assertThat(c.get("lastSignal").isNull()).isTrue();
        assertThat(c.get("createdBy").asText()).isEqualTo("등록 담당자");

        // 회사 감시: kind WATCH, 이름 "회사:나부품", 360분, 키워드 = 이름 + 별칭 (별칭의 중복 이름은 cleanTerms가 걸러낸다), ONBOARD 작업
        long watchId = c.get("watchSourceId").asLong();
        assertThat(one("SELECT kind || '|' || watch_name || '|' || method || '|' || status || '|' || interval_minutes || '|' || name "
                + "FROM source WHERE id = ?", watchId)).isEqualTo("WATCH|회사:나부품|API|PENDING|360|watch:회사:나부품");
        assertThat(one("SELECT v.config::text FROM source_config_version v JOIN source s ON s.active_config_version_id = v.id "
                + "WHERE s.id = ?", watchId))
                .isEqualTo("{\"method\": \"API\", \"exclude\": [], \"keywords\": [\"나부품\", \"NABU\"], \"provider\": \"naver_news\"}");
        assertThat(one("SELECT t.domain || '|' || (t.risk_flags->>'api_provider') FROM site t JOIN source s ON s.site_id = t.id "
                + "WHERE s.id = ?", watchId)).isEqualTo("news.naver.com|naver_news");
        assertThat(one("SELECT job_type || ':' || status FROM crawl_job WHERE source_id = ?", watchId)).isEqualTo("ONBOARD:QUEUED");
        assertThat(get("registrar", "/api/v1/watches").toString()).contains("\"name\":\"회사:나부품\"");

        // 점검 소스: kind CHECK 하나, api.odcloud.kr, 1440분, config {provider data_go_kr, check nts_status}, ONBOARD 작업
        assertThat(count("SELECT count(*) FROM source WHERE kind = 'CHECK'")).isEqualTo(1L);
        long checkId = jdbc.sql("SELECT id FROM source WHERE kind = 'CHECK'").query(Long.class).single();
        assertThat(one("SELECT name || '|' || method || '|' || status || '|' || interval_minutes FROM source WHERE id = ?", checkId))
                .isEqualTo("check:nts_status|API|PENDING|1440");
        assertThat(one("SELECT t.domain || '|' || t.site_type || '|' || (t.risk_flags->>'api_provider') FROM site t "
                + "JOIN source s ON s.site_id = t.id WHERE s.id = ?", checkId)).isEqualTo("api.odcloud.kr|PUBLIC|data_go_kr");
        assertThat(one("SELECT v.version || '|' || v.status || '|' || v.config::text FROM source_config_version v "
                + "JOIN source s ON s.active_config_version_id = v.id WHERE s.id = ?", checkId))
                .isEqualTo("1|ACTIVE|{\"check\": \"nts_status\", \"method\": \"API\", \"provider\": \"data_go_kr\"}");
        assertThat(one("SELECT job_type || ':' || status || ':' || trigger FROM crawl_job WHERE source_id = ?", checkId))
                .isEqualTo("ONBOARD:QUEUED:REGISTRATION");
        // 관리자 소스 목록에 kind CHECK로 보인다
        assertThat(get("admin", "/api/v1/sources").toString()).contains("\"kind\":\"CHECK\"");

        // 두 번째 회사: 점검 소스는 그대로 하나, 감시는 하나 더
        createCompany("{\"name\":\"가전자\",\"bizNo\":\"1018112345\"}");
        assertThat(count("SELECT count(*) FROM source WHERE kind = 'CHECK'")).isEqualTo(1L);
        assertThat(count("SELECT count(*) FROM source WHERE kind = 'WATCH'")).isEqualTo(2L);
        assertThat(count("SELECT count(*) FROM crawl_job")).isEqualTo(3L);

        // 검증: 사업자번호 형식·중복, 고유번호 형식, 이름 없음
        assertThat(post("registrar", "/api/v1/companies", "{\"name\":\"다\",\"bizNo\":\"12345\"}").getStatusCode())
                .isEqualTo(HttpStatus.BAD_REQUEST);
        assertThat(post("registrar", "/api/v1/companies", "{\"name\":\"다\",\"bizNo\":\"123-45-6789A\"}").getStatusCode())
                .isEqualTo(HttpStatus.BAD_REQUEST);
        assertThat(post("registrar", "/api/v1/companies", "{\"name\":\"다\",\"bizNo\":\"1234567890\"}").getStatusCode())
                .isEqualTo(HttpStatus.CONFLICT);
        assertThat(post("registrar", "/api/v1/companies", "{\"name\":\"다\",\"corpCode\":\"123\"}").getStatusCode())
                .isEqualTo(HttpStatus.BAD_REQUEST);
        assertThat(post("registrar", "/api/v1/companies", "{\"name\":\"  \"}").getStatusCode()).isEqualTo(HttpStatus.BAD_REQUEST);
        assertThat(count("SELECT count(*) FROM company")).isEqualTo(2L);
        assertThat(one("SELECT string_agg(action, ',' ORDER BY id) FROM audit_log"))
                .contains("COMPANY_CREATED").contains("CHECK_SOURCE_CREATED").contains("WATCH_CREATED");
        assertThat(get("viewer", "/api/v1/companies/" + id).get("company").get("name").asText()).isEqualTo("나부품");   // 조회는 VIEWER도
    }

    @Test
    void importUpsertsByBizNoFromJsonAndCsv() {
        ResponseEntity<String> first = post("registrar", "/api/v1/companies/import",
                "[{\"name\":\"나부품\",\"aliases\":[\"NABU\"],\"bizNo\":\"123-45-67890\"},{\"name\":\"가전자\",\"bizNo\":\"1018112345\"}]");
        assertThat(first.getStatusCode()).as(first.getBody()).isEqualTo(HttpStatus.OK);
        assertThat(read(first.getBody()).get("created").asInt()).isEqualTo(2);
        assertThat(read(first.getBody()).get("updated").asInt()).isZero();
        long nabu = jdbc.sql("SELECT id FROM company WHERE biz_no = '1234567890'").query(Long.class).single();
        long nabuWatch = jdbc.sql("SELECT watch_source_id FROM company WHERE id = ?").param(nabu).query(Long.class).single();

        // CSV: 사업자번호가 같은 나부품은 갱신(별칭 추가 → 감시 설정 v2), 다라인은 새로. aliases·tags는 ';' 구분, 따옴표 셀 지원
        String csv = "name,aliases,biz_no,corp_code,external_id,tags\n"
                + "나부품 주식회사,\"NABU;나부품\",1234567890,00123456,CAI-001,1차 공급사;핵심\n"
                + "다라인,,201-81-00001,,CAI-002,\n";
        ResponseEntity<String> second = call("registrar", HttpMethod.POST, "/api/v1/companies/import", csv, MediaType.valueOf("text/csv"));
        assertThat(second.getStatusCode()).as(second.getBody()).isEqualTo(HttpStatus.OK);
        JsonNode r = read(second.getBody());
        assertThat(r.get("created").asInt()).isEqualTo(1);
        assertThat(r.get("updated").asInt()).isEqualTo(1);
        assertThat(count("SELECT count(*) FROM company")).isEqualTo(3L);
        assertThat(one("SELECT name || '|' || aliases::text || '|' || corp_code || '|' || external_id || '|' || tags::text "
                + "FROM company WHERE id = ?", nabu))
                .isEqualTo("나부품 주식회사|[\"NABU\", \"나부품\"]|00123456|CAI-001|[\"1차 공급사\", \"핵심\"]");
        assertThat(one("SELECT string_agg(version || ':' || status, ',' ORDER BY version) FROM source_config_version WHERE source_id = ?",
                nabuWatch)).isEqualTo("1:RETIRED,2:ACTIVE");
        assertThat(one("SELECT v.config->>'keywords' FROM source_config_version v JOIN source s ON s.active_config_version_id = v.id "
                + "WHERE s.id = ?", nabuWatch)).isEqualTo("[\"나부품 주식회사\", \"NABU\", \"나부품\"]");
        assertThat(one("SELECT watch_name FROM source WHERE id = ?", nabuWatch)).isEqualTo("회사:나부품 주식회사");
        assertThat(one("SELECT biz_no FROM company WHERE name = '다라인'")).isEqualTo("2018100001");
        assertThat(count("SELECT count(*) FROM source WHERE kind = 'WATCH'")).isEqualTo(3L);
        assertThat(count("SELECT count(*) FROM source WHERE kind = 'CHECK'")).isEqualTo(1L);

        // 한 행이라도 사업자번호 형식이 틀리면 전체 400 (부분 반영 없음), 헤더 없는 CSV도 400
        ResponseEntity<String> bad = call("registrar", HttpMethod.POST, "/api/v1/companies/import",
                "name,biz_no\n라물류,3018100002\n마상사,12-34\n", MediaType.TEXT_PLAIN);
        assertThat(bad.getStatusCode()).isEqualTo(HttpStatus.BAD_REQUEST);
        assertThat(bad.getBody()).contains("2번째 행");
        assertThat(count("SELECT count(*) FROM company")).isEqualTo(3L);
        assertThat(call("registrar", HttpMethod.POST, "/api/v1/companies/import", "라물류,3018100002\n", MediaType.TEXT_PLAIN)
                .getStatusCode()).isEqualTo(HttpStatus.BAD_REQUEST);
        assertThat(post("viewer", "/api/v1/companies/import", "[{\"name\":\"x\"}]").getStatusCode()).isEqualTo(HttpStatus.FORBIDDEN);
        assertThat(get("viewer", "/api/v1/companies")).hasSize(3);
    }

    @Test
    void listDetailSignalsUpdateAndArchive() {
        long nabu = createCompany("{\"name\":\"나부품\",\"aliases\":[\"NABU\"],\"bizNo\":\"1234567890\"}");
        long ga = createCompany("{\"name\":\"가전자\"}");
        long watchId = jdbc.sql("SELECT watch_source_id FROM company WHERE id = ?").param(nabu).query(Long.class).single();

        // 워커가 남겼을 자료: 감시로 걸린 기사 1건(연결 WATCH) + 신호 3건 (BIZ_STATUS HIGH, NEWS MEDIUM, 과세유형 LOW)
        long doc = jdbc.sql("""
                INSERT INTO document (source_id, url, url_hash, title, published_at)
                VALUES (?, 'https://news.example.com/1', md5('a') || md5('b'), '나부품 공장 화재', '2026-10-16T07:00:00+09:00') RETURNING id
                """).param(watchId).query(Long.class).single();
        jdbc.sql("INSERT INTO document_company (document_id, company_id, matched_by) VALUES (?, ?, 'WATCH')").params(doc, nabu).update();
        jdbc.sql("""
                INSERT INTO company_signal (company_id, kind, severity, fingerprint, title, detail, observed_at, document_id)
                VALUES (?, 'NEWS', 'MEDIUM', 'hash1', '나부품 공장 화재', '{"topics":["사건·사고"]}', '2026-10-16T07:00:00+09:00', ?),
                       (?, 'BIZ_STATUS', 'LOW', 'tax:01', '과세유형 변경: 간이 → 일반', '{}', '2026-10-15T09:00:00+09:00', NULL),
                       (?, 'BIZ_STATUS', 'HIGH', '0320260930', '폐업자 (폐업일 2026-09-30)', '{"b_stt_cd":"03"}', '2026-10-16T09:00:00+09:00', NULL)
                """).params(nabu, doc, nabu, nabu).update();
        jdbc.sql("UPDATE company SET last_biz_status = '{\"b_stt\":\"폐업자\",\"b_stt_cd\":\"03\"}'::jsonb WHERE id = ?").param(nabu).update();

        // 목록: 이름순(가전자, 나부품), 신호 수·HIGH 수·최근 신호·연결 문서 수
        JsonNode list = get("viewer", "/api/v1/companies");
        assertThat(list).hasSize(2);
        assertThat(list.get(0).get("name").asText()).isEqualTo("가전자");
        JsonNode n = list.get(1);
        assertThat(n.get("name").asText()).isEqualTo("나부품");
        assertThat(n.get("signalCount").asLong()).isEqualTo(3);
        assertThat(n.get("highSignalCount").asLong()).isEqualTo(1);
        assertThat(n.get("documentCount").asLong()).isEqualTo(1);
        assertThat(n.get("lastSignal").get("title").asText()).isEqualTo("폐업자 (폐업일 2026-09-30)");   // observed_at 최신
        assertThat(n.get("lastBizStatus").get("b_stt").asText()).isEqualTo("폐업자");

        // 상세: 신호는 심각도 → 시각 내림차순, 문서는 연결 순
        JsonNode d = get("viewer", "/api/v1/companies/" + nabu);
        assertThat(d.get("company").get("id").asLong()).isEqualTo(nabu);
        assertThat(d.get("signals").findValuesAsText("severity")).containsExactly("HIGH", "MEDIUM", "LOW");
        assertThat(d.get("signals").get(1).get("documentUrl").asText()).isEqualTo("https://news.example.com/1");
        assertThat(d.get("signals").get(1).get("detail").get("topics").get(0).asText()).isEqualTo("사건·사고");
        assertThat(d.get("documents")).hasSize(1);
        assertThat(d.get("documents").get(0).get("matchedBy").asText()).isEqualTo("WATCH");
        assertThat(d.get("documents").get(0).get("title").asText()).isEqualTo("나부품 공장 화재");
        assertThat(call("viewer", HttpMethod.GET, "/api/v1/companies/999", null, null).getStatusCode()).isEqualTo(HttpStatus.NOT_FOUND);

        // 신호 API: since, kind·severity 필터, 잘못된 kind는 400
        assertThat(get("viewer", "/api/v1/companies/" + nabu + "/signals")).hasSize(3);
        assertThat(get("viewer", "/api/v1/companies/" + nabu + "/signals?since=2026-10-15T20:00:00Z")).hasSize(2);   // LOW(15일)만 빠진다
        JsonNode all = get("viewer", "/api/v1/company-signals");
        assertThat(all).hasSize(3);
        assertThat(all.get(0).get("companyName").asText()).isEqualTo("나부품");
        assertThat(get("viewer", "/api/v1/company-signals?severity=high")).hasSize(1);
        assertThat(get("viewer", "/api/v1/company-signals?kind=BIZ_STATUS&since=2026-10-16T00:00:00%2B09:00")).hasSize(1);
        assertThat(call("viewer", HttpMethod.GET, "/api/v1/company-signals?kind=RUMOR", null, null).getStatusCode())
                .isEqualTo(HttpStatus.BAD_REQUEST);

        // 수정: 별칭이 바뀌면 감시 키워드가 새 버전으로, 생략한 필드는 그대로. 사업자번호가 바뀌면 마지막 상태는 지운다
        ResponseEntity<String> updated = put("registrar", "/api/v1/companies/" + nabu,
                "{\"aliases\":[\"NABU\",\"나부품(주)\"],\"tags\":[\"핵심\"]}");
        assertThat(updated.getStatusCode()).as(updated.getBody()).isEqualTo(HttpStatus.OK);
        JsonNode u = read(updated.getBody());
        assertThat(u.get("name").asText()).isEqualTo("나부품");
        assertThat(u.get("bizNo").asText()).isEqualTo("1234567890");
        assertThat(u.get("aliases").toString()).isEqualTo("[\"NABU\",\"나부품(주)\"]");
        assertThat(u.get("lastBizStatus").get("b_stt").asText()).isEqualTo("폐업자");
        assertThat(one("SELECT v.version || '|' || (v.config->>'keywords') FROM source_config_version v "
                + "JOIN source s ON s.active_config_version_id = v.id WHERE s.id = ?", watchId))
                .isEqualTo("2|[\"나부품\", \"NABU\", \"나부품(주)\"]");
        assertThat(put("registrar", "/api/v1/companies/" + nabu, "{\"tags\":[\"1차\"]}").getStatusCode()).isEqualTo(HttpStatus.OK);
        assertThat(count("SELECT count(*) FROM source_config_version WHERE source_id = ?", watchId)).isEqualTo(2L);   // 태그만 바꾸면 그대로
        assertThat(put("registrar", "/api/v1/companies/" + nabu, "{\"bizNo\":\"999\"}").getStatusCode()).isEqualTo(HttpStatus.BAD_REQUEST);
        assertThat(put("registrar", "/api/v1/companies/" + ga, "{\"bizNo\":\"123-45-67890\"}").getStatusCode()).isEqualTo(HttpStatus.CONFLICT);
        JsonNode moved = read(put("registrar", "/api/v1/companies/" + nabu, "{\"bizNo\":\"201-81-00001\"}").getBody());
        assertThat(moved.get("bizNo").asText()).isEqualTo("2018100001");
        assertThat(moved.get("lastBizStatus").isNull()).isTrue();
        assertThat(put("viewer", "/api/v1/companies/" + nabu, "{\"tags\":[]}").getStatusCode()).isEqualTo(HttpStatus.FORBIDDEN);

        // 보관: ARCHIVED, 감시는 EXCLUDED, 기본 목록에서 빠짐, 신호는 남음, 수정은 409, 다시 지워도 204
        assertThat(call("registrar", HttpMethod.DELETE, "/api/v1/companies/" + nabu, null, null).getStatusCode())
                .isEqualTo(HttpStatus.NO_CONTENT);
        assertThat(one("SELECT status FROM company WHERE id = ?", nabu)).isEqualTo("ARCHIVED");
        assertThat(one("SELECT status || '|' || status_reason FROM source WHERE id = ?", watchId)).isEqualTo("EXCLUDED|회사 보관: 나부품");
        assertThat(get("viewer", "/api/v1/companies")).hasSize(1);
        assertThat(get("viewer", "/api/v1/companies?includeArchived=true")).hasSize(2);
        assertThat(count("SELECT count(*) FROM company_signal WHERE company_id = ?", nabu)).isEqualTo(3L);
        assertThat(put("registrar", "/api/v1/companies/" + nabu, "{\"tags\":[]}").getStatusCode()).isEqualTo(HttpStatus.CONFLICT);
        assertThat(call("registrar", HttpMethod.DELETE, "/api/v1/companies/" + nabu, null, null).getStatusCode())
                .isEqualTo(HttpStatus.NO_CONTENT);
        // 같은 이름의 회사를 다시 만들 수 있다 (감시 이름 중복은 EXCLUDED를 빼고 본다)
        createCompany("{\"name\":\"나부품\"}");
        assertThat(one("SELECT string_agg(action, ',' ORDER BY id) FROM audit_log"))
                .contains("COMPANY_UPDATED").contains("COMPANY_ARCHIVED").contains("WATCH_EXCLUDED");
        assertThat(List.of(get("viewer", "/api/v1/companies").size())).containsExactly(2);
    }
}
