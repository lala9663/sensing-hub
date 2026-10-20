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

/**
 * 키워드 감시 목록 정리: 제외(EXCLUDED)한 감시는 목록에서 빠지고 관리자는 include_excluded로 보며 다시 켤 수 있다.
 * 관리자가 만든 감시는 공용이라 모든 사용자가 읽기 전용으로 본다(목록·단건·감시 문서·네이버 실시간), 고치는 것은 관리자만.
 * 실제 PostgreSQL(+pgvector)이 필요하며 SENSING_IT_DB_URL이 있을 때만 실행된다. 네이버는 기본(표시 전용)이고 키가 없다.
 */
@EnabledIfEnvironmentVariable(named = "SENSING_IT_DB_URL", matches = "jdbc:postgresql:.+")
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT,
        properties = {"sensing.scheduler.enabled=false", "sensing.digest.enabled=false"})
class WatchListIT {

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

    private ResponseEntity<String> call(String user, HttpMethod method, String path, String body) {
        HttpHeaders h = new HttpHeaders();
        if (body != null) {
            h.setContentType(MediaType.APPLICATION_JSON);
        }
        h.set("X-User", user);
        return http.exchange(path, method, new HttpEntity<>(body, h), String.class);
    }

    private ResponseEntity<String> post(String user, String path, String body) {
        return call(user, HttpMethod.POST, path, body == null ? "" : body);
    }

    private ResponseEntity<String> put(String user, String path, String body) {
        return call(user, HttpMethod.PUT, path, body);
    }

    private HttpStatus status(ResponseEntity<String> res) {
        return HttpStatus.valueOf(res.getStatusCode().value());
    }

    private JsonNode get(String user, String path) {
        ResponseEntity<String> res = call(user, HttpMethod.GET, path, null);
        assertThat(res.getStatusCode()).as(res.getBody()).isEqualTo(HttpStatus.OK);
        return read(res.getBody());
    }

    private HttpStatus getStatus(String user, String path) {
        return status(call(user, HttpMethod.GET, path, null));
    }

    private JsonNode read(String body) {
        try {
            return json.readTree(body);
        } catch (IOException e) {
            throw new IllegalStateException(e);
        }
    }

    private long createWatch(String user, String name, String keywords) {
        ResponseEntity<String> res = post(user, "/api/v1/watches", "{\"name\":\"" + name + "\",\"keywords\":" + keywords + "}");
        assertThat(res.getStatusCode()).as(res.getBody()).isEqualTo(HttpStatus.ACCEPTED);
        return read(res.getBody()).get("id").asLong();
    }

    private void exclude(long sourceId) {
        ResponseEntity<String> res = post("admin", "/api/v1/sources/" + sourceId + "/actions/exclude", null);
        assertThat(res.getStatusCode()).as(res.getBody()).isEqualTo(HttpStatus.OK);
    }

    private String names(JsonNode list) {
        return String.join(",", list.findValuesAsText("name"));
    }

    private String one(String sql, Object... params) {
        return jdbc.sql(sql).params(params).query(String.class).single();
    }

    private long count(String sql, Object... params) {
        return jdbc.sql(sql).params(params).query(Long.class).single();
    }

    // ------------------------------------------------------------------ 테스트

    @Test
    void excludedWatchesAreHiddenAndAdminCanShowThemAndReactivate() {
        long chip = createWatch("admin", "반도체", "[\"반도체\"]");
        long meta = createWatch("admin", "메타넷", "[\"메타넷\",\"메타넷 글로벌\"]");
        long rival = createWatch("registrar", "경쟁사", "[\"A사\"]");
        exclude(meta);
        assertThat(one("SELECT status || '|' || status_reason FROM source WHERE id = ?", meta)).isEqualTo("EXCLUDED|관리자 제외");

        // 기본 목록은 제외된 감시를 뺀다 (공용 먼저, 그다음 개인 감시)
        assertThat(names(get("admin", "/api/v1/watches"))).isEqualTo("반도체,경쟁사");
        // 관리자의 "제외된 감시 보기": 함께 돌려주고 순서는 그대로 (공용 만든 순서 → 개인)
        JsonNode all = get("admin", "/api/v1/watches?include_excluded=true");
        assertThat(names(all)).isEqualTo("반도체,메타넷,경쟁사");
        assertThat(all.get(1).get("status").asText()).isEqualTo("EXCLUDED");
        // 관리자가 아니면 include_excluded는 듣지 않는다
        assertThat(names(get("registrar", "/api/v1/watches?include_excluded=true"))).isEqualTo("반도체,경쟁사");
        assertThat(names(get("viewer", "/api/v1/watches?include_excluded=true"))).isEqualTo("반도체");
        // 제외된 감시도 단건으로는 보인다 (감시 문서 필터의 이름 표시)
        assertThat(get("viewer", "/api/v1/watches/" + meta).get("status").asText()).isEqualTo("EXCLUDED");

        // 다시 켜기: ADMIN만, 제외된 감시만
        assertThat(status(post("registrar", "/api/v1/watches/" + meta + "/reactivate", null))).isEqualTo(HttpStatus.FORBIDDEN);
        assertThat(status(post("viewer", "/api/v1/watches/" + meta + "/reactivate", null))).isEqualTo(HttpStatus.FORBIDDEN);
        assertThat(status(post("admin", "/api/v1/watches/" + chip + "/reactivate", null))).isEqualTo(HttpStatus.CONFLICT);
        assertThat(status(post("admin", "/api/v1/watches/99999/reactivate", null))).isEqualTo(HttpStatus.NOT_FOUND);

        ResponseEntity<String> on = post("admin", "/api/v1/watches/" + meta + "/reactivate", null);
        assertThat(on.getStatusCode()).as(on.getBody()).isEqualTo(HttpStatus.OK);
        JsonNode w = read(on.getBody());
        assertThat(w.get("status").asText()).isEqualTo("ACTIVE");
        assertThat(w.get("naver").asBoolean()).isFalse();
        assertThat(w.get("nextRunAt").isNull()).isTrue();
        assertThat(w.get("registrationStatus").asText()).isEqualTo("COMPLETED");
        assertThat(w.get("registrationMessage").asText()).isEqualTo("관리자가 감시를 다시 켰습니다. 등록된 언론사에서 수집한 기사에 키워드를 대조합니다.");
        // 대조 모드 규칙: ACTIVE + next_run_at NULL + 사유 없음, 작업을 넣지 않는다. 설정(키워드)은 그대로 v1
        assertThat(one("SELECT status || '|' || (next_run_at IS NULL) || '|' || (status_reason IS NULL) || '|' || consecutive_failures "
                + "FROM source WHERE id = ?", meta)).isEqualTo("ACTIVE|true|true|0");
        assertThat(count("SELECT count(*) FROM crawl_job WHERE source_id = ?", meta)).isZero();
        assertThat(one("SELECT v.version || '|' || (v.config->>'naver') || '|' || (v.config->'keywords')::text FROM source s "
                + "JOIN source_config_version v ON v.id = s.active_config_version_id WHERE s.id = ?", meta))
                .isEqualTo("1|false|[\"메타넷\", \"메타넷 글로벌\"]");
        assertThat(one("SELECT string_agg(action, ',' ORDER BY id) FROM audit_log WHERE target_type = 'source' AND target_id = ?", meta))
                .isEqualTo("WATCH_CREATED,SOURCE_EXCLUDE,WATCH_REACTIVATED");
        assertThat(names(get("admin", "/api/v1/watches"))).isEqualTo("반도체,메타넷,경쟁사");
        assertThat(names(get("viewer", "/api/v1/watches"))).isEqualTo("반도체,메타넷");

        // 같은 이름의 감시가 이미 켜져 있으면 다시 켜지 않는다 (제외한 뒤 같은 이름으로 새로 만든 경우)
        exclude(chip);
        long chip2 = createWatch("admin", "반도체", "[\"반도체\",\"HBM\"]");
        ResponseEntity<String> clash = post("admin", "/api/v1/watches/" + chip + "/reactivate", null);
        assertThat(clash.getStatusCode()).isEqualTo(HttpStatus.CONFLICT);
        assertThat(clash.getBody()).contains("#" + chip2);
        assertThat(one("SELECT status FROM source WHERE id = ?", chip)).isEqualTo("EXCLUDED");
        assertThat(rival).isPositive();
    }

    @Test
    void adminWatchesAreSharedReadOnlyForEveryone() {
        long shared = createWatch("admin", "주요 키워드", "[\"메타넷\",\"삼성전자\"]");
        long own = createWatch("registrar", "개인 감시", "[\"경쟁사\"]");

        // VIEWER: 공용만, 읽기 전용
        JsonNode viewer = get("viewer", "/api/v1/watches");
        assertThat(names(viewer)).isEqualTo("주요 키워드");
        assertThat(viewer.get(0).get("shared").asBoolean()).isTrue();
        assertThat(viewer.get(0).get("mine").asBoolean()).isFalse();
        assertThat(viewer.get(0).get("editable").asBoolean()).isFalse();
        assertThat(viewer.get(0).get("requestedBy").asText()).isEqualTo("운영 관리자");

        // REGISTRAR: 공용 먼저, 그다음 내 감시 (내 감시만 고칠 수 있다)
        JsonNode registrar = get("registrar", "/api/v1/watches");
        assertThat(names(registrar)).isEqualTo("주요 키워드,개인 감시");
        assertThat(registrar.get(1).get("shared").asBoolean()).isFalse();
        assertThat(registrar.get(1).get("mine").asBoolean()).isTrue();
        assertThat(registrar.get(1).get("editable").asBoolean()).isTrue();
        assertThat(registrar.get(0).get("editable").asBoolean()).isFalse();

        // ADMIN: 전체, 모두 고칠 수 있다
        JsonNode admin = get("admin", "/api/v1/watches");
        assertThat(names(admin)).isEqualTo("주요 키워드,개인 감시");
        assertThat(admin.get(0).get("mine").asBoolean()).isTrue();
        assertThat(admin.get(1).get("mine").asBoolean()).isFalse();
        assertThat(admin.findValuesAsText("editable")).containsExactly("true", "true");

        // 단건: 공용은 누구나, 개인 감시는 만든 사람과 ADMIN만
        assertThat(get("viewer", "/api/v1/watches/" + shared).get("keywords").toString()).isEqualTo("[\"메타넷\",\"삼성전자\"]");
        assertThat(getStatus("viewer", "/api/v1/watches/" + own)).isEqualTo(HttpStatus.NOT_FOUND);

        // 수정: 공용은 ADMIN만 (보이지만 고칠 수 없으면 403), 남의 개인 감시는 여전히 404
        assertThat(status(put("viewer", "/api/v1/watches/" + shared, "{\"keywords\":[\"x\"]}"))).isEqualTo(HttpStatus.FORBIDDEN);
        assertThat(status(put("registrar", "/api/v1/watches/" + shared, "{\"keywords\":[\"x\"]}"))).isEqualTo(HttpStatus.FORBIDDEN);
        assertThat(status(put("viewer", "/api/v1/watches/" + own, "{\"keywords\":[\"x\"]}"))).isEqualTo(HttpStatus.NOT_FOUND);
        assertThat(one("SELECT (v.config->'keywords')::text FROM source s JOIN source_config_version v "
                + "ON v.id = s.active_config_version_id WHERE s.id = ?", shared)).isEqualTo("[\"메타넷\", \"삼성전자\"]");
        ResponseEntity<String> edited = put("admin", "/api/v1/watches/" + shared, "{\"keywords\":[\"메타넷\",\"삼성전자\",\"테슬라\"]}");
        assertThat(edited.getStatusCode()).isEqualTo(HttpStatus.OK);
        assertThat(edited.getBody()).contains("\"configVersion\":2");
        assertThat(status(put("registrar", "/api/v1/watches/" + own, "{\"keywords\":[\"경쟁사\",\"B사\"]}"))).isEqualTo(HttpStatus.OK);
        // 제외도 ADMIN만 (소스 조치)
        assertThat(status(post("viewer", "/api/v1/sources/" + shared + "/actions/exclude", null))).isEqualTo(HttpStatus.FORBIDDEN);
        assertThat(status(post("registrar", "/api/v1/sources/" + shared + "/actions/exclude", null))).isEqualTo(HttpStatus.FORBIDDEN);

        // 감시 문서: 공용 감시에 걸린 기사를 VIEWER도 키워드별로 본다
        long press = jdbc.sql("INSERT INTO site (domain, base_url) VALUES ('press.example.com', 'https://press.example.com') RETURNING id")
                .query(Long.class).single();
        long pressSource = jdbc.sql("INSERT INTO source (site_id, name, status) VALUES (?, 'press', 'ACTIVE') RETURNING id")
                .param(press).query(Long.class).single();
        long doc = jdbc.sql("""
                INSERT INTO document (source_id, url, url_hash, title, published_at)
                VALUES (?, 'https://press.example.com/1', md5('w') || md5('l'), '메타넷 신제품', '2026-10-20T09:00:00+09:00') RETURNING id
                """).param(pressSource).query(Long.class).single();
        jdbc.sql("INSERT INTO document_watch (document_id, source_id, keyword) VALUES (?, ?, '메타넷')").params(doc, shared).update();
        assertThat(get("viewer", "/api/v1/watches").get(0).get("documentCount").asLong()).isEqualTo(1);
        assertThat(get("viewer", "/api/v1/documents?watch_id=" + shared + "&keyword=메타넷").findValuesAsText("title"))
                .containsExactly("메타넷 신제품");

        // 네이버 실시간: 공용 감시는 VIEWER도 부를 수 있다 (키가 없어 409), 남의 개인 감시는 404
        assertThat(getStatus("viewer", "/api/v1/watches/" + shared + "/naver-news")).isEqualTo(HttpStatus.CONFLICT);
        assertThat(getStatus("viewer", "/api/v1/watches/" + own + "/naver-news")).isEqualTo(HttpStatus.NOT_FOUND);
    }

    @Test
    void excludedCompanyWatchShowsAsExcludedAndArchivedCompanyWatchStaysOff() {
        ResponseEntity<String> created = post("admin", "/api/v1/companies", "{\"name\":\"가전자\",\"aliases\":[\"GJ\"]}");
        assertThat(created.getStatusCode()).as(created.getBody()).isEqualTo(HttpStatus.CREATED);
        long company = read(created.getBody()).get("id").asLong();
        long watch = read(created.getBody()).get("watchSourceId").asLong();
        assertThat(names(get("viewer", "/api/v1/watches"))).isEqualTo("회사:가전자");   // 관리자가 만든 회사 감시도 공용

        // 회사는 그대로 두고 감시만 제외: 거래처 화면은 감시 상태 "제외"(EXCLUDED)를 보여 주고, 감시 목록에서는 빠진다
        exclude(watch);
        JsonNode detail = get("viewer", "/api/v1/companies/" + company).get("company");
        assertThat(detail.get("status").asText()).isEqualTo("ACTIVE");
        assertThat(detail.get("watchStatus").asText()).isEqualTo("EXCLUDED");
        assertThat(detail.get("watchStatusReason").asText()).isEqualTo("관리자 제외");
        assertThat(get("viewer", "/api/v1/companies").get(0).get("watchStatus").asText()).isEqualTo("EXCLUDED");
        assertThat(get("viewer", "/api/v1/watches")).isEmpty();
        // 회사 별칭을 고쳐도 깨지지 않는다 (감시는 제외된 채 키워드만 새 버전)
        assertThat(status(put("admin", "/api/v1/companies/" + company, "{\"aliases\":[\"GJ\",\"가전\"]}"))).isEqualTo(HttpStatus.OK);
        assertThat(one("SELECT status FROM source WHERE id = ?", watch)).isEqualTo("EXCLUDED");

        // 다시 켜면 거래처 화면도 수집 기사 대조 중(ACTIVE)
        assertThat(status(post("admin", "/api/v1/watches/" + watch + "/reactivate", null))).isEqualTo(HttpStatus.OK);
        assertThat(get("viewer", "/api/v1/companies/" + company).get("company").get("watchStatus").asText()).isEqualTo("ACTIVE");

        // 회사를 보관하면 감시는 제외되고, 보관한 회사의 감시는 다시 켜지 않는다
        assertThat(status(call("admin", HttpMethod.DELETE, "/api/v1/companies/" + company, null)))
                .isEqualTo(HttpStatus.NO_CONTENT);
        assertThat(one("SELECT status FROM source WHERE id = ?", watch)).isEqualTo("EXCLUDED");
        ResponseEntity<String> refused = post("admin", "/api/v1/watches/" + watch + "/reactivate", null);
        assertThat(refused.getStatusCode()).isEqualTo(HttpStatus.CONFLICT);
        assertThat(refused.getBody()).contains("보관한 회사");
    }
}
