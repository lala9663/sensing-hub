package io.sensinghub;

import static org.assertj.core.api.Assertions.assertThat;

import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.URLDecoder;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import org.junit.jupiter.api.AfterAll;
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
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

/**
 * 네이버 뉴스 검색 (NAVER API HUB) — 기본 표시 전용(display) 모드.
 * 가짜 HUB 서버(127.0.0.1)로 요청 형식(경로·헤더·파라미터)과 오류 처리를 확인하고, 실시간 보기가 DB(document 등)에 아무것도 남기지 않고
 * LLM·임베딩을 부르지 않는지 본다 (LLM·임베딩 주소도 같은 가짜 서버로 돌려 요청이 오는지 센다). 실제 네이버는 부르지 않는다.
 * 빈 DB를 가리키는 SENSING_IT_DB_URL 환경 변수가 있을 때만 실행된다.
 */
@EnabledIfEnvironmentVariable(named = "SENSING_IT_DB_URL", matches = "jdbc:postgresql:.+")
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT,
        properties = {"sensing.scheduler.enabled=false", "sensing.digest.enabled=false", "sensing.caidentia.enabled=false"})
class NaverNewsIT {

    static final String SECRET_KEY = "it-secret-key";
    static final String CLIENT_ID = "hub-client-id";
    static final String CLIENT_SECRET = "hub-client-secret";
    static final FakeHub hub = FakeHub.start();

    @DynamicPropertySource
    static void props(DynamicPropertyRegistry r) {
        r.add("spring.datasource.url", () -> System.getenv("SENSING_IT_DB_URL"));
        r.add("spring.flyway.clean-disabled", () -> "false");
        r.add("sensing.secret-key", () -> SECRET_KEY);
        // 기본값 그대로(표시 전용, HUB) — 개발 PC의 환경 변수와 관계없이 고정한다. 주소만 가짜 서버로 바꾼다
        r.add("sensing.naver.mode", () -> "display");
        r.add("sensing.naver.api", () -> "hub");
        r.add("sensing.naver.search-url", () -> hub.base() + "/search/v1/news");
        // LLM·임베딩도 가짜 서버로 — 네이버 결과가 LLM으로 흘러가면 /llm 요청이 찍힌다
        r.add("sensing.llm.provider", () -> "openai");
        r.add("sensing.llm.base-url", () -> hub.base() + "/llm/v1");
        r.add("sensing.llm.model", () -> "fake-model");
        r.add("sensing.embed.provider", () -> "openai");
        r.add("sensing.embed.base-url", () -> hub.base() + "/llm/v1");
        r.add("sensing.embed.model", () -> "fake-embed");
    }

    @AfterAll
    static void stopHub() {
        hub.server.stop(0);
    }

    @Autowired TestRestTemplate http;
    @Autowired JdbcClient jdbc;
    @Autowired ObjectMapper json;

    @BeforeEach
    void clean() {
        jdbc.sql("""
                TRUNCATE chat_feedback, chat_citation, chat_message, chat_session, chunk, attachment,
                         document_version, document_watch, document_enrichment, document_company, company_signal, company,
                         document, crawl_error, crawl_job, registration_request, audit_log, api_credential
                RESTART IDENTITY CASCADE
                """).update();
        jdbc.sql("UPDATE source SET active_config_version_id = NULL").update();
        jdbc.sql("TRUNCATE source_config_version, source, site RESTART IDENTITY CASCADE").update();
        hub.requests.clear();
    }

    // ------------------------------------------------------------------ 표시 전용: 실시간 보기

    @Test
    void displayModeShowsLiveNaverNewsWithoutStoringOrSendingToLlm() throws Exception {
        JsonNode status = get("registrar", "/api/v1/naver/status").body;
        assertThat(status.get("mode").asText()).isEqualTo("display");
        assertThat(status.get("api").asText()).isEqualTo("hub");
        assertThat(status.get("keyRegistered").asBoolean()).isFalse();
        assertThat(status.get("liveAvailable").asBoolean()).isFalse();                // 키가 없으면 화면이 숨긴다
        assertThat(status.get("sourceLabel").asText()).isEqualTo("NAVER 검색 결과");

        long watch = createWatch("registrar", "자사 보도", "[\"메타넷\",\"경쟁사\"]", "[\"채용\"]");
        assertThat(get("registrar", "/api/v1/watches/" + watch + "/naver-news").status).isEqualTo(HttpStatus.CONFLICT);

        // 형식이 틀린 키는 저장하지 않는다
        assertThat(post("admin", "/api/v1/api-credentials", "{\"provider\":\"naver_news\",\"secret\":\"no-colon\"}")
                .getStatusCode()).isEqualTo(HttpStatus.BAD_REQUEST);
        // 키 저장: 표시 전용이라 감시에 네이버 저장 수집을 붙이지 않는다 (ONBOARD 없음, 설정 그대로 naver false)
        ResponseEntity<String> saved = saveKey(CLIENT_ID + ":" + CLIENT_SECRET);
        assertThat(saved.getStatusCode()).isEqualTo(HttpStatus.OK);
        JsonNode result = json.readTree(saved.getBody());
        assertThat(result.get("attachedWatches").asInt()).isZero();
        assertThat(result.get("note").asText()).contains("표시 전용");
        assertThat(saved.getBody()).doesNotContain(CLIENT_SECRET);
        assertThat(count("crawl_job")).isZero();
        assertThat(mode(watch)).isEqualTo("1|false|ACTIVE");
        assertThat(get("registrar", "/api/v1/watches/" + watch).body.get("naver").asBoolean()).isFalse();
        assertThat(hub.requests).isEmpty();                                             // 저장만으로는 부르지 않는다

        status = get("registrar", "/api/v1/naver/status").body;
        assertThat(status.get("liveAvailable").asBoolean()).isTrue();
        assertThat(status.get("keyStatus").asText()).isEqualTo("UNVERIFIED");

        jdbc.sql("INSERT INTO site (domain, base_url, name) VALUES ('press.example.com', 'https://press.example.com', '예시일보')")
                .update();
        Map<String, Long> before = counts();

        // 실시간 보기: 백엔드가 HUB를 대신 부르고 제목·언론사·시각·원문/네이버 링크만 돌려준다
        Reply live = get("registrar", "/api/v1/watches/" + watch + "/naver-news?keyword=메타넷");
        assertThat(live.status).isEqualTo(HttpStatus.OK);
        assertThat(live.headers.getCacheControl()).contains("no-store");
        JsonNode body = live.body;
        assertThat(body.get("keyword").asText()).isEqualTo("메타넷");
        assertThat(body.get("sourceLabel").asText()).isEqualTo("NAVER 검색 결과");
        assertThat(body.get("mode").asText()).isEqualTo("display");
        assertThat(body.get("total").asLong()).isEqualTo(1234);
        assertThat(body.get("start").asInt()).isEqualTo(1);
        assertThat(body.get("nextStart").asInt()).isEqualTo(21);
        assertThat(body.get("excluded").asInt()).isEqualTo(1);                          // 제외어 "채용"
        JsonNode items = body.get("items");
        assertThat(items).hasSize(3);
        assertThat(items.get(0).get("title").asText()).isEqualTo("메타넷, \"신제품\" 공개");    // <b>·엔티티 정리
        assertThat(items.get(0).get("press").asText()).isEqualTo("예시일보");                 // 등록된 사이트 이름
        assertThat(items.get(0).get("originalUrl").asText()).isEqualTo("https://press.example.com/a/1");
        assertThat(items.get(0).get("naverUrl").asText()).isEqualTo("https://n.news.naver.com/mnews/article/001/0001");
        assertThat(items.get(0).get("publishedAt").asText()).isEqualTo("2026-10-16T00:30:00Z");
        assertThat(items.get(1).get("press").asText()).isEqualTo("other.example.org");      // 모르는 언론사는 호스트
        assertThat(items.get(1).get("naverUrl").isNull()).isTrue();                          // 네이버에 없는 기사
        assertThat(items.get(2).get("originalUrl").asText()).startsWith("https://n.news.naver.com/");   // 원문 링크 없음
        assertThat(items.get(2).get("press").asText()).isEqualTo("네이버 뉴스");
        assertThat(body.toString()).doesNotContain("description").doesNotContain("요약 설명")
                .doesNotContain(CLIENT_ID).doesNotContain(CLIENT_SECRET);

        // HUB 요청 형식: GET /search/v1/news, query·display·start·sort·format, X-NCP-APIGW-API-KEY-ID/KEY 헤더
        assertThat(hub.requests).hasSize(1);
        FakeHub.Req req = hub.requests.get(0);
        assertThat(req.method()).isEqualTo("GET");
        assertThat(req.path()).isEqualTo("/search/v1/news");
        assertThat(req.params()).containsExactly(Map.entry("query", "메타넷"), Map.entry("display", "20"),
                Map.entry("start", "1"), Map.entry("sort", "date"), Map.entry("format", "json"));
        assertThat(req.keyId()).isEqualTo(CLIENT_ID);
        assertThat(req.key()).isEqualTo(CLIENT_SECRET);
        assertThat(req.rawQuery()).doesNotContain(CLIENT_SECRET);

        // 캐시 없음: 다시 부르면 다시 요청한다. 다음 쪽·크기도 그대로 넘긴다
        Reply page2 = get("registrar", "/api/v1/watches/" + watch + "/naver-news?keyword=경쟁사&start=21&display=5");
        assertThat(page2.status).isEqualTo(HttpStatus.OK);
        assertThat(hub.requests).hasSize(2);
        assertThat(hub.requests.get(1).params()).containsEntry("query", "경쟁사").containsEntry("start", "21")
                .containsEntry("display", "5");
        assertThat(get("registrar", "/api/v1/watches/" + watch + "/naver-news?keyword=메타넷").status).isEqualTo(HttpStatus.OK);
        assertThat(hub.requests).hasSize(3);
        // 범위를 넘는 값은 API 한도 안으로 줄인다
        get("registrar", "/api/v1/watches/" + watch + "/naver-news?display=500&start=5000");
        assertThat(hub.requests.get(3).params()).containsEntry("query", "메타넷").containsEntry("display", "50")
                .containsEntry("start", "1000");

        // 감시 키워드가 아니면 400, 남의 감시는 404 (VIEWER는 자기 감시만)
        assertThat(get("registrar", "/api/v1/watches/" + watch + "/naver-news?keyword=아무거나").status)
                .isEqualTo(HttpStatus.BAD_REQUEST);
        assertThat(get("viewer", "/api/v1/watches/" + watch + "/naver-news").status).isEqualTo(HttpStatus.NOT_FOUND);
        assertThat(hub.requests).hasSize(4);

        // DB에는 아무것도 남지 않았다 (문서·감시 기록·조각·보강·작업·대화·감사 로그). 키 상태만 VALID로 바뀐다
        assertThat(counts()).isEqualTo(before);
        assertThat(jdbc.sql("SELECT status FROM api_credential WHERE provider = 'naver_news'").query(String.class).single())
                .isEqualTo("VALID");
        // LLM·임베딩 요청 없음
        assertThat(hub.requests).allMatch(r -> r.path().equals("/search/v1/news"));
        assertThat(hub.otherPaths).isEmpty();
    }

    @Test
    void naverErrorsAreExplainedAndBadKeyIsMarkedInvalid() throws Exception {
        long watch = createWatch("registrar", "오류", "[\"한도초과\",\"SE02\",\"서버오류\",\"메타넷\"]", "[]");
        saveKey(CLIENT_ID + ":" + CLIENT_SECRET);

        Reply limited = get("registrar", "/api/v1/watches/" + watch + "/naver-news?keyword=한도초과");
        assertThat(limited.status).isEqualTo(HttpStatus.TOO_MANY_REQUESTS);
        assertThat(limited.body.get("error").asText()).isEqualTo("NAVER_RATE_LIMITED");
        assertThat(limited.body.get("message").asText()).contains("하루 허용량");

        Reply badParam = get("registrar", "/api/v1/watches/" + watch + "/naver-news?keyword=SE02");
        assertThat(badParam.status).isEqualTo(HttpStatus.BAD_GATEWAY);
        assertThat(badParam.body.get("message").asText()).contains("display 값이 허용 범위").contains("SE02");

        Reply server = get("registrar", "/api/v1/watches/" + watch + "/naver-news?keyword=서버오류");
        assertThat(server.status).isEqualTo(HttpStatus.BAD_GATEWAY);
        assertThat(server.body.get("message").asText()).contains("네이버 검색 서버 내부 오류");
        // 한도·요청 오류는 키 상태를 바꾸지 않는다
        assertThat(credentialStatus()).isEqualTo("UNVERIFIED");

        // 관리자 시험 호출 (검색어 "뉴스" 1건)
        assertThat(post("registrar", "/api/v1/api-credentials/naver_news/verify", "").getStatusCode())
                .isEqualTo(HttpStatus.FORBIDDEN);
        JsonNode ok = json.readTree(post("admin", "/api/v1/api-credentials/naver_news/verify", "").getBody());
        assertThat(ok.get("status").asText()).isEqualTo("VALID");
        assertThat(ok.get("message").asText()).contains("시험 호출 성공");
        FakeHub.Req probe = hub.requests.get(hub.requests.size() - 1);
        assertThat(probe.params()).containsEntry("query", "뉴스").containsEntry("display", "1");

        // 틀린 키: 401 (API Gateway 오류 본문) → INVALID, 메시지에 키가 섞이지 않는다
        saveKey(CLIENT_ID + ":wrong-secret");
        ResponseEntity<String> bad = post("admin", "/api/v1/api-credentials/naver_news/verify", "");
        JsonNode badBody = json.readTree(bad.getBody());
        assertThat(badBody.get("status").asText()).isEqualTo("INVALID");
        assertThat(badBody.get("message").asText()).contains("Client ID").contains("Authentication Failed");
        assertThat(bad.getBody()).doesNotContain("wrong-secret").doesNotContain(CLIENT_ID);
        String lastError = jdbc.sql("SELECT last_error FROM api_credential WHERE provider = 'naver_news'").query(String.class).single();
        assertThat(lastError).contains("인증 실패").doesNotContain("wrong-secret").doesNotContain(CLIENT_ID);

        // INVALID 키면 화면이 실시간 보기를 숨기고, 직접 불러도 네이버를 부르지 않는다
        assertThat(get("registrar", "/api/v1/naver/status").body.get("liveAvailable").asBoolean()).isFalse();
        int calls = hub.requests.size();
        Reply invalid = get("registrar", "/api/v1/watches/" + watch + "/naver-news");
        assertThat(invalid.status).isEqualTo(HttpStatus.CONFLICT);
        assertThat(invalid.body.get("error").asText()).isEqualTo("NAVER_KEY_INVALID");
        assertThat(hub.requests).hasSize(calls);

        // 실시간 보기 중 401이 나도 INVALID로 표시한다
        saveKey(CLIENT_ID + ":also-wrong");
        Reply denied = get("registrar", "/api/v1/watches/" + watch + "/naver-news");
        assertThat(denied.status).isEqualTo(HttpStatus.BAD_GATEWAY);
        assertThat(denied.body.get("error").asText()).isEqualTo("NAVER_KEY_INVALID");
        assertThat(credentialStatus()).isEqualTo("INVALID");

        assertThat(count("document")).isZero();
        assertThat(hub.otherPaths).isEmpty();
    }

    // ------------------------------------------------------------------

    private long createWatch(String user, String name, String keywords, String exclude) throws Exception {
        ResponseEntity<String> res = post(user, "/api/v1/watches",
                "{\"name\":\"" + name + "\",\"keywords\":" + keywords + ",\"exclude\":" + exclude + "}");
        assertThat(res.getStatusCode().is2xxSuccessful()).as(res.getBody()).isTrue();
        return json.readTree(res.getBody()).get("id").asLong();
    }

    private ResponseEntity<String> saveKey(String secret) {
        return post("admin", "/api/v1/api-credentials", "{\"provider\":\"naver_news\",\"secret\":\"" + secret + "\"}");
    }

    private String credentialStatus() {
        return jdbc.sql("SELECT status FROM api_credential WHERE provider = 'naver_news'").query(String.class).single();
    }

    /** "활성 설정 버전|naver 표시|소스 상태" */
    private String mode(long sourceId) {
        return jdbc.sql("SELECT v.version || '|' || COALESCE(v.config->>'naver', '-') || '|' || s.status FROM source s "
                + "JOIN source_config_version v ON v.id = s.active_config_version_id WHERE s.id = ?")
                .param(sourceId).query(String.class).single();
    }

    private long count(String table) {
        return jdbc.sql("SELECT count(*) FROM " + table).query(Long.class).single();
    }

    private Map<String, Long> counts() {
        Map<String, Long> out = new LinkedHashMap<>();
        for (String t : List.of("document", "document_watch", "document_version", "document_enrichment", "document_company",
                "chunk", "attachment", "crawl_job", "crawl_error", "registration_request", "source", "source_config_version",
                "site", "company_signal", "issue", "issue_document", "chat_session", "chat_message", "digest_run", "audit_log")) {
            out.put(t, count(t));
        }
        return out;
    }

    private ResponseEntity<String> post(String user, String path, String body) {
        HttpHeaders h = new HttpHeaders();
        h.setContentType(MediaType.APPLICATION_JSON);
        h.set("X-User", user);
        return http.exchange(path, HttpMethod.POST, new HttpEntity<>(body, h), String.class);
    }

    private record Reply(HttpStatus status, HttpHeaders headers, JsonNode body) {
    }

    private Reply get(String user, String path) throws Exception {
        HttpHeaders h = new HttpHeaders();
        h.set("X-User", user);
        ResponseEntity<String> res = http.exchange(path, HttpMethod.GET, new HttpEntity<>(h), String.class);
        return new Reply(HttpStatus.valueOf(res.getStatusCode().value()), res.getHeaders(),
                res.getBody() == null ? null : json.readTree(res.getBody()));
    }

    /**
     * 가짜 NAVER API HUB 뉴스 검색. 헤더의 Client ID/Secret이 맞으면 결과, 아니면 401(API Gateway 오류 본문, 받은 값을 일부러 되돌린다).
     * 검색어로 장애를 흉내 낸다: 한도초과 → 429, SE02 → 400 SE02, 서버오류 → 500 SE99. 그 밖의 경로(/llm 등)는 otherPaths에 남긴다.
     */
    static final class FakeHub {
        record Req(String method, String path, String rawQuery, Map<String, String> params, String keyId, String key) {
        }

        final HttpServer server;
        final List<Req> requests = Collections.synchronizedList(new ArrayList<>());
        final List<String> otherPaths = Collections.synchronizedList(new ArrayList<>());

        private FakeHub(HttpServer server) {
            this.server = server;
        }

        static FakeHub start() {
            try {
                HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
                FakeHub hub = new FakeHub(server);
                server.createContext("/", hub::handle);
                server.start();
                return hub;
            } catch (IOException e) {
                throw new IllegalStateException(e);
            }
        }

        String base() {
            return "http://127.0.0.1:" + server.getAddress().getPort();
        }

        private void handle(HttpExchange ex) throws IOException {
            String path = ex.getRequestURI().getPath();
            if (!path.equals("/search/v1/news")) {
                otherPaths.add(path);
                send(ex, 404, "{\"errorMessage\":\"Invalid search api\",\"errorCode\":\"SE05\"}");
                return;
            }
            String raw = ex.getRequestURI().getRawQuery();
            Map<String, String> params = new LinkedHashMap<>();
            for (String pair : raw == null ? new String[0] : raw.split("&")) {
                int eq = pair.indexOf('=');
                params.put(URLDecoder.decode(pair.substring(0, eq), StandardCharsets.UTF_8),
                        URLDecoder.decode(pair.substring(eq + 1), StandardCharsets.UTF_8));
            }
            String id = ex.getRequestHeaders().getFirst("X-NCP-APIGW-API-KEY-ID");
            String key = ex.getRequestHeaders().getFirst("X-NCP-APIGW-API-KEY");
            requests.add(new Req(ex.getRequestMethod(), path, raw, params, id, key));
            String query = params.getOrDefault("query", "");
            if (!CLIENT_ID.equals(id) || !CLIENT_SECRET.equals(key)) {
                send(ex, 401, "{\"error\":{\"errorCode\":\"200\",\"message\":\"Authentication Failed\","
                        + "\"details\":\"Invalid authentication information. (id=" + id + ", key=" + key + ")\"}}");
            } else if (query.equals("한도초과")) {
                send(ex, 429, "{\"error\":{\"errorCode\":\"400\",\"message\":\"Quota Exceeded\"}}");
            } else if (query.equals("SE02")) {
                send(ex, 400, "{\"errorMessage\":\"Invalid display value (부적절한 display 값입니다.)\",\"errorCode\":\"SE02\"}");
            } else if (query.equals("서버오류")) {
                send(ex, 500, "{\"errorMessage\":\"System Error\",\"errorCode\":\"SE99\"}");
            } else {
                send(ex, 200, """
                        {"lastBuildDate":"Fri, 16 Oct 2026 10:00:00 +0900","total":1234,"start":%s,"display":4,"items":[
                          {"title":"<b>메타넷</b>, &quot;신제품&quot; 공개","originallink":"https://press.example.com/a/1",
                           "link":"https://n.news.naver.com/mnews/article/001/0001","description":"<b>메타넷</b> 요약 설명",
                           "pubDate":"Fri, 16 Oct 2026 09:30:00 +0900"},
                          {"title":"<b>메타넷</b> 채용 공고","originallink":"https://press.example.com/a/2",
                           "link":"https://n.news.naver.com/mnews/article/001/0002","description":"요약 설명",
                           "pubDate":"Fri, 16 Oct 2026 09:00:00 +0900"},
                          {"title":"다른 매체 보도","originallink":"https://www.other.example.org/news/3",
                           "link":"https://www.other.example.org/news/3","description":"요약 설명",
                           "pubDate":"Fri, 16 Oct 2026 08:00:00 +0900"},
                          {"title":"원문 링크 없는 기사","originallink":"",
                           "link":"https://n.news.naver.com/mnews/article/002/0004","description":"요약 설명",
                           "pubDate":"Fri, 16 Oct 2026 07:00:00 +0900"}]}
                        """.formatted(params.getOrDefault("start", "1")));
            }
        }

        private static void send(HttpExchange ex, int status, String body) throws IOException {
            byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
            ex.getResponseHeaders().set("Content-Type", "application/json;charset=utf-8");
            ex.sendResponseHeaders(status, bytes.length);
            try (OutputStream out = ex.getResponseBody()) {
                out.write(bytes);
            }
        }
    }
}
