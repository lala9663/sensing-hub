package io.sensinghub;

import static org.assertj.core.api.Assertions.assertThat;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.ArrayList;
import java.util.Iterator;
import java.util.List;
import java.util.Map;
import java.util.stream.Stream;

import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.web.client.TestRestTemplate;
import org.springframework.boot.test.web.server.LocalServerPort;
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

import io.sensinghub.llm.MockLlmServer;
import io.sensinghub.llm.MockLlmServer.Reply;

/**
 * 챗봇 대화형 개선: 답 스트리밍(POST /messages/stream, SSE — 단계 → 글 조각 → 저장된 최종 답), 스트림이 끊기면 한 번에 받기로 바꾸기,
 * 429 처리와 사용량 장부, 키워드 바로 검색은 바로 답, 추천 후속 질문 칩(meta.followUps)의 저장, 앞 답의 근거 안에서 다시 거르기
 * (search_documents within_previous·previous_refs, 0건 포함). LLM은 가짜 OpenAI 호환 서버라 실제 호출이 없다.
 * 실제 PostgreSQL이 필요하며 SENSING_IT_DB_URL이 있을 때만 실행된다.
 */
@EnabledIfEnvironmentVariable(named = "SENSING_IT_DB_URL", matches = "jdbc:postgresql:.+")
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT,
        properties = {"sensing.scheduler.enabled=false", "sensing.digest.enabled=false"})
class ChatStreamIT {

    static final MockLlmServer LLM = MockLlmServer.start();
    static final ObjectMapper JSON = new ObjectMapper();
    static final HttpClient CLIENT = HttpClient.newBuilder().version(HttpClient.Version.HTTP_1_1)
            .connectTimeout(Duration.ofSeconds(5)).build();

    @DynamicPropertySource
    static void props(DynamicPropertyRegistry r) {
        r.add("spring.datasource.url", () -> System.getenv("SENSING_IT_DB_URL"));
        r.add("spring.flyway.clean-disabled", () -> "false");
        r.add("sensing.secret-key", () -> "it-secret-key");
        r.add("sensing.llm.provider", () -> "openai");
        r.add("sensing.llm.base-url", () -> LLM.baseUrl() + "/v1");
        r.add("sensing.llm.model", () -> "stream-model");
        r.add("sensing.llm.api-key", () -> "test-key");
        r.add("sensing.llm.timeout-seconds", () -> "10");
        r.add("sensing.llm.retry-delay-ms", () -> "0");
        r.add("sensing.embed.provider", () -> "none");
    }

    @AfterAll
    static void stopLlm() {
        LLM.close();
    }

    @AfterEach
    void scriptedRepliesPassed() {
        assertThat(LLM.failures()).isEmpty();
    }

    @Autowired TestRestTemplate http;
    @Autowired JdbcClient jdbc;
    @LocalServerPort int port;

    @BeforeEach
    void clean() {
        LLM.reset();
        jdbc.sql("""
                TRUNCATE chat_feedback, chat_citation, chat_message, chat_session, chunk, attachment, issue_document, issue,
                         document_version, document_watch, document_enrichment, document_company, company_signal, company,
                         document, crawl_error, crawl_job, registration_request, audit_log, api_credential
                RESTART IDENTITY CASCADE
                """).update();
        jdbc.sql("UPDATE source SET active_config_version_id = NULL").update();
        jdbc.sql("TRUNCATE source_config_version, source, site RESTART IDENTITY CASCADE").update();
        jdbc.sql("TRUNCATE llm_usage, llm_cooldown").update();
    }

    // ------------------------------------------------------------------ 도우미

    record Event(String name, JsonNode data, long atNanos) {
    }

    /** POST /messages/stream을 끝까지 읽어 이벤트(이름·JSON·도착 시각)로. */
    private List<Event> stream(String user, long session, String content) throws Exception {
        HttpRequest request = HttpRequest.newBuilder(
                        URI.create("http://127.0.0.1:" + port + "/api/v1/chat/sessions/" + session + "/messages/stream"))
                .timeout(Duration.ofSeconds(30))
                .header("X-User", user).header("Content-Type", "application/json").header("Accept", "text/event-stream")
                .POST(HttpRequest.BodyPublishers.ofString(JSON.writeValueAsString(Map.of("content", content))))
                .build();
        HttpResponse<Stream<String>> res = CLIENT.send(request, HttpResponse.BodyHandlers.ofLines());
        assertThat(res.statusCode()).isEqualTo(200);
        assertThat(res.headers().firstValue("content-type").orElse("")).startsWith("text/event-stream");
        List<Event> events = new ArrayList<>();
        String name = null;
        StringBuilder data = null;
        Iterator<String> lines = res.body().iterator();
        while (lines.hasNext()) {
            String line = lines.next();
            if (line.isEmpty()) {
                if (data != null) {
                    events.add(new Event(name == null ? "message" : name, JSON.readTree(data.toString()), System.nanoTime()));
                }
                name = null;
                data = null;
            } else if (line.startsWith("event:")) {
                name = line.substring(6).strip();
            } else if (line.startsWith("data:")) {
                String v = line.substring(5);
                v = v.startsWith(" ") ? v.substring(1) : v;
                data = data == null ? new StringBuilder(v) : data.append('\n').append(v);
            }
        }
        return events;
    }

    private static List<String> names(List<Event> events) {
        return events.stream().map(e -> e.name().equals("stage") ? "stage:" + e.data().path("stage").asText() : e.name())
                .toList();
    }

    private static String streamedText(List<Event> events) {
        StringBuilder sb = new StringBuilder();
        for (Event e : events) {
            if (e.name().equals("delta")) {
                sb.append(e.data().path("text").asText());
            } else if (e.name().equals("reset")) {
                sb.setLength(0);
            }
        }
        return sb.toString();
    }

    private static JsonNode done(List<Event> events) {
        Event last = events.get(events.size() - 1);
        assertThat(last.name()).isEqualTo("done");
        return last.data();
    }

    private ResponseEntity<JsonNode> call(String user, HttpMethod method, String path, Object body) {
        HttpHeaders h = new HttpHeaders();
        h.set("X-User", user);
        h.setContentType(MediaType.APPLICATION_JSON);
        return http.exchange(path, method, new HttpEntity<>(body, h), JsonNode.class);
    }

    private long session(String user) {
        ResponseEntity<JsonNode> res = call(user, HttpMethod.POST, "/api/v1/chat/sessions", Map.of());
        assertThat(res.getStatusCode()).isEqualTo(HttpStatus.CREATED);
        return res.getBody().get("id").asLong();
    }

    private JsonNode send(String user, long session, String content) {
        ResponseEntity<JsonNode> res = call(user, HttpMethod.POST, "/api/v1/chat/sessions/" + session + "/messages",
                Map.of("content", content));
        assertThat(res.getStatusCode()).as(String.valueOf(res.getBody())).isEqualTo(HttpStatus.OK);
        return res.getBody();
    }

    private static Reply toolCall(String id, String name, String args) {
        return Reply.ok(MockLlmServer.openAiToolCall(id, name, args, null));
    }

    private static Reply text(String text) {
        return Reply.ok(MockLlmServer.openAiText(text));
    }

    private static List<JsonNode> messages(JsonNode request, String role) {
        List<JsonNode> out = new ArrayList<>();
        request.path("messages").forEach(m -> {
            if (role.equals(m.path("role").asText())) {
                out.add(m);
            }
        });
        return out;
    }

    /** 요청의 마지막 도구 결과 JSON. */
    private static JsonNode lastToolResult(JsonNode request) {
        List<JsonNode> tools = messages(request, "tool");
        try {
            return JSON.readTree(tools.get(tools.size() - 1).path("content").asText());
        } catch (Exception e) {
            throw new AssertionError(e);
        }
    }

    private static List<String> titles(JsonNode toolResult) {
        List<String> out = new ArrayList<>();
        toolResult.path("documents").forEach(d -> out.add(d.path("title").asText()));
        return out;
    }

    private static List<String> chips(JsonNode message) {
        List<String> out = new ArrayList<>();
        message.path("followUps").forEach(f -> out.add(f.path("text").asText()));
        return out;
    }

    private long siteSource(String domain, String name) {
        long siteId = jdbc.sql("INSERT INTO site (domain, base_url, site_type, name) VALUES (?, ?, 'NEWS', ?) RETURNING id")
                .params(domain, "https://" + domain, name).query(Long.class).single();
        return jdbc.sql("INSERT INTO source (site_id, name, status) VALUES (?, ?, 'ACTIVE') RETURNING id")
                .params(siteId, domain).query(Long.class).single();
    }

    /** hoursAgo: 발행 시각 (결과 순서를 정한다 — 검색은 최신순, 이슈 기사는 발행 순). */
    private long document(long sourceId, String url, String title, String tags, int hoursAgo) {
        long id = jdbc.sql("""
                INSERT INTO document (source_id, url, url_hash, title, published_at)
                VALUES (?, ?, md5(?) || md5(?), ?, now() - make_interval(hours => ?)) RETURNING id
                """).params(sourceId, url, url, url, title, hoursAgo).query(Long.class).single();
        jdbc.sql("""
                INSERT INTO document_enrichment (document_id, document_version, summary, topics, tags)
                VALUES (?, 1, NULL, '["산업동향"]'::jsonb, CAST(? AS jsonb))
                """).params(id, tags).update();
        return id;
    }

    private long company(String name) {
        return jdbc.sql("INSERT INTO company (name) VALUES (?) RETURNING id").param(name).query(Long.class).single();
    }

    private void link(long documentId, long companyId) {
        jdbc.sql("INSERT INTO document_company (document_id, company_id, matched_by) VALUES (?, ?, 'TAG')")
                .params(documentId, companyId).update();
    }

    private long usage(String column) {
        return jdbc.sql("SELECT COALESCE(sum(" + column + "), 0) FROM llm_usage WHERE model = 'stream-model' AND purpose = 'chat'")
                .query(Long.class).single();
    }

    // ------------------------------------------------------------------ 스트리밍

    @Test
    void streamSendsStagesThenTextThenTheSavedAnswer() throws Exception {
        long press = siteSource("news.example.com", "예시일보");
        long other = siteSource("edaily.example.com", "이데일리");
        long first = document(press, "https://news.example.com/1", "반도체 수출 사상 최대", "[\"수출\"]", 1);
        long second = document(other, "https://edaily.example.com/2", "반도체 장비 국산화 지원", "[\"장비\"]", 2);
        link(first, company("삼성전자"));

        LLM.then(Reply.sse(MockLlmServer.openAiStreamToolCall("call-1", "search_documents", "sig-1", true,
                        "{\"query\":\"반도", "체\",\"days\":7}")))
                .then(body -> {
                    assertThat(body.path("stream").asBoolean()).isTrue();
                    JsonNode echoed = messages(body, "assistant").get(0).path("tool_calls").path(0);
                    assertThat(echoed.path("extra_content").path("google").path("thought_signature").asText()).isEqualTo("sig-1");
                    assertThat(lastToolResult(body).path("count").asInt()).isEqualTo(2);
                    return Reply.sse(300, MockLlmServer.openAiStreamText("반도체 소식입니다.\n", "- 수출 사상 최대 [1]\n",
                            "- 장비 국산화 지원 [2]"));
                });
        long s = session("viewer");
        List<Event> events = stream("viewer", s, "반도체 소식 알려 줘");

        assertThat(names(events)).containsExactly("stage:READING", "stage:TOOL", "stage:COMPOSING",
                "delta", "delta", "delta", "done");
        assertThat(events.get(0).data().path("label").asText()).isEqualTo("질문을 살펴보는 중…");
        assertThat(events.get(1).data().path("tool").asText()).isEqualTo("search_documents");
        assertThat(events.get(1).data().path("label").asText()).isEqualTo("수집 기사 검색 중…");
        assertThat(events.get(2).data().path("label").asText()).isEqualTo("근거를 정리하는 중…");
        // 글은 조각마다 바로 온다 (서버가 모아 두었다 보내지 않는다): 첫 조각이 끝보다 한참 먼저 도착한다
        Event firstDelta = events.get(3);
        assertThat((events.get(6).atNanos() - firstDelta.atNanos()) / 1_000_000).isGreaterThan(600);

        JsonNode turn = done(events);
        JsonNode answer = turn.get("assistant");
        assertThat(answer.get("error").asBoolean()).isFalse();
        assertThat(answer.get("content").asText()).isEqualTo(streamedText(events).strip());
        assertThat(answer.get("citations")).hasSize(2);
        assertThat(answer.get("citations").get(0).get("documentId").asLong()).isEqualTo(first);
        assertThat(answer.get("citations").get(1).get("documentId").asLong()).isEqualTo(second);
        assertThat(answer.get("tools").get(0).get("name").asText()).isEqualTo("search_documents");
        assertThat(turn.get("session").get("title").asText()).isEqualTo("반도체 소식 알려 줘");
        // 추천 칩: 근거 두 건 중 한 건에만 있는 회사, 기간 넓히기
        assertThat(chips(answer)).contains("이 중 삼성전자 관련만", "‘반도체’ 최근 30일로 넓혀 보기");

        // 저장은 한 번에 받기와 같다: 단계별 메시지, 근거 스냅숏, 칩은 meta에
        assertThat(jdbc.sql("SELECT role FROM chat_message WHERE session_id = ? ORDER BY seq").param(s).query(String.class).list())
                .containsExactly("USER", "ASSISTANT", "TOOL", "ASSISTANT");
        long answerId = answer.get("id").asLong();
        assertThat(jdbc.sql("SELECT document_id FROM chat_citation WHERE message_id = ? ORDER BY ref_no").param(answerId)
                .query(Long.class).list()).containsExactly(first, second);
        assertThat(jdbc.sql("SELECT meta->>'streamed' FROM chat_message WHERE id = ?").param(answerId).query(String.class)
                .single()).isEqualTo("true");
        assertThat(usage("calls")).isEqualTo(2);
        assertThat(usage("input_tokens")).isEqualTo(220);   // 스트림 마지막 조각의 사용량도 장부에 적는다

        JsonNode reloaded = call("viewer", HttpMethod.GET, "/api/v1/chat/sessions/" + s, null).getBody().get("messages").get(1);
        assertThat(chips(reloaded)).isEqualTo(chips(answer));
        assertThat(reloaded.get("citations")).hasSize(2);
        assertThat(LLM.pending()).isZero();
    }

    /** 스트림이 중간에 끊기면 보낸 조각을 버리라고(reset) 알리고 같은 요청을 한 번에 받기로 다시 보낸다. 장부에는 실패 1·호출 2. */
    @Test
    void brokenStreamFallsBackToOneShot() throws Exception {
        List<String> broken = new ArrayList<>(MockLlmServer.openAiStreamText("앞부분만 ", "오다가"));
        broken.subList(2, broken.size()).clear();
        LLM.then(Reply.sse(broken)).then(body -> {
            assertThat(body.has("stream")).isFalse();
            return text("다시 받은 답입니다.");
        });
        long s = session("viewer");
        List<Event> events = stream("viewer", s, "무엇을 할 수 있어?");

        assertThat(names(events)).containsExactly("stage:READING", "delta", "delta", "reset", "done");
        assertThat(streamedText(events)).isEmpty();
        assertThat(done(events).get("assistant").get("content").asText()).isEqualTo("다시 받은 답입니다.");
        assertThat(done(events).get("assistant").get("error").asBoolean()).isFalse();
        assertThat(usage("calls")).isEqualTo(2);
        assertThat(usage("errors")).isEqualTo(1);
    }

    /** 429는 다시 보내지 않고 안내 답으로 끝난다 (조각 없음). 쉬는 동안은 LLM을 부르지 않고 바로 안내한다. */
    @Test
    void rateLimitOnTheStreamIsExplainedAtOnce() throws Exception {
        LLM.then(Reply.status(429, MockLlmServer.GEMINI_DAILY_429));
        long s = session("viewer");
        List<Event> events = stream("viewer", s, "주요 이슈 알려 줘");
        assertThat(names(events)).containsExactly("stage:READING", "done");
        JsonNode answer = done(events).get("assistant");
        assertThat(answer.get("error").asBoolean()).isTrue();
        assertThat(answer.get("errorKind").asText()).isEqualTo("RATE_LIMITED");
        assertThat(answer.get("followUps")).isEmpty();
        assertThat(LLM.requests()).hasSize(1);
        assertThat(usage("rate_limited")).isEqualTo(1);
        assertThat(jdbc.sql("SELECT reason FROM llm_cooldown WHERE model = 'stream-model'").query(String.class).single())
                .isEqualTo("DAILY");

        long started = System.nanoTime();
        List<Event> paused = stream("viewer", s, "다시 알려 줘");
        assertThat((System.nanoTime() - started) / 1_000_000).isLessThan(3000);
        assertThat(names(paused)).doesNotContain("delta");
        assertThat(done(paused).get("assistant").get("content").asText()).contains("한국 시간").contains("풀리니");
        assertThat(LLM.requests()).hasSize(1);
    }

    /** 키워드 바로 검색은 단계·조각 없이 바로 답한다 (칩 포함). 검사 오류는 스트림을 열기 전에 보통 JSON 오류로 온다. */
    @Test
    void keywordShortcutAnswersAtOnceOnTheStream() throws Exception {
        long press = siteSource("news.example.com", "예시일보");
        long other = siteSource("edaily.example.com", "이데일리");
        long a = document(press, "https://news.example.com/1", "반도체 수출 사상 최대", "[]", 1);
        document(other, "https://edaily.example.com/2", "반도체 장비 국산화", "[]", 2);
        link(a, company("삼성전자"));
        long s = session("viewer");

        List<Event> events = stream("viewer", s, "반도체");
        assertThat(names(events)).containsExactly("done");
        JsonNode answer = done(events).get("assistant");
        assertThat(answer.get("mode").asText()).isEqualTo("KEYWORD");
        assertThat(answer.get("citations")).hasSize(2);
        assertThat(chips(answer)).containsExactly("이 중 삼성전자 관련만", "‘반도체’ 최근 30일로 넓혀 보기", "이 중 예시일보 기사만");
        assertThat(LLM.requests()).isEmpty();

        ResponseEntity<JsonNode> naver = call("viewer", HttpMethod.POST, "/api/v1/chat/sessions/" + s + "/messages/stream",
                Map.of("content", "네이버 - 반도체"));
        assertThat(naver.getStatusCode()).isEqualTo(HttpStatus.BAD_REQUEST);
        assertThat(naver.getBody().get("error").asText()).isEqualTo("NAVER_DISPLAY_ONLY");
        assertThat(call("admin", HttpMethod.POST, "/api/v1/chat/sessions/" + s + "/messages/stream",
                Map.of("content", "반도체")).getStatusCode()).isEqualTo(HttpStatus.NOT_FOUND);
    }

    // ------------------------------------------------------------------ 앞 결과 안에서 다시 거르기

    /**
     * 키워드 바로 검색 답 → "이 중 삼성전자 관련만"(칩): 새로 찾지 않고 앞 근거 3건 안에서 거른다 (제목에 없어도 연결된 회사로 맞춘다,
     * 앞 목록에 없던 삼성전자 기사는 나오지 않는다) → "그중 이데일리 것만": 0건 → 새로 찾기 칩 → "2번 기사": previous_refs.
     */
    @Test
    void withinPreviousFiltersThePreviousCitations() {
        long press = siteSource("news.example.com", "예시일보");
        long other = siteSource("edaily.example.com", "이데일리");
        long samsung = company("삼성전자");
        long d1 = document(press, "https://news.example.com/1", "삼성전자 반도체 HBM 공급 확대", "[\"HBM\"]", 1);
        long d2 = document(other, "https://edaily.example.com/2", "SK하이닉스 반도체 증산", "[\"HBM\"]", 2);
        long d3 = document(press, "https://news.example.com/3", "메모리 반도체 가격 반등", "[\"파운드리\"]", 3);
        long old = document(press, "https://news.example.com/old", "삼성전자 반도체 신공장", "[]", 4);
        jdbc.sql("UPDATE document SET collected_at = now() - interval '40 days' WHERE id = ?").param(old).update();
        link(d1, samsung);
        link(d3, samsung);
        link(old, samsung);

        long s = session("viewer");
        JsonNode keyword = send("viewer", s, "반도체").get("assistant");
        assertThat(keyword.get("citations")).extracting(c -> c.get("documentId").asLong()).containsExactly(d1, d2, d3);
        assertThat(chips(keyword)).contains("이 중 삼성전자 관련만");

        LLM.then(body -> {
            assertThat(messages(body, "user").get(messages(body, "user").size() - 1).path("content").asText())
                    .isEqualTo("이 중 삼성전자 관련만");   // 키워드 바로 검색으로 가지 않았다
            List<String> props = new ArrayList<>();
            body.path("tools").get(0).path("function").path("parameters").path("properties").fieldNames().forEachRemaining(props::add);
            assertThat(props).contains("within_previous", "previous_refs");
            assertThat(messages(body, "system").get(0).path("content").asText()).contains("within_previous");
            return toolCall("w1", "search_documents", "{\"within_previous\":true,\"query\":\"삼성전자\"}");
        }).then(body -> {
            JsonNode result = lastToolResult(body);
            assertThat(result.path("within_previous").asText()).contains("3건");
            assertThat(titles(result)).containsExactly("삼성전자 반도체 HBM 공급 확대", "메모리 반도체 가격 반등");
            assertThat(result.path("documents").get(1).path("companies").get(0).asText()).isEqualTo("삼성전자");
            // 앞 답의 번호 그대로 (실측: Gemini는 거른 답에도 앞 목록의 번호를 쓴다)
            assertThat(result.path("documents").get(0).path("ref").asInt()).isEqualTo(1);
            assertThat(result.path("documents").get(1).path("ref").asInt()).isEqualTo(3);
            return text("앞 목록 중 삼성전자 관련은 2건입니다.\n- [1]\n- [3]");
        });
        JsonNode narrowed = send("viewer", s, "이 중 삼성전자 관련만").get("assistant");
        assertThat(narrowed.get("citations")).extracting(c -> c.get("documentId").asLong()).containsExactly(d1, d3);
        assertThat(narrowed.get("citations")).extracting(c -> c.get("refNo").asInt()).containsExactly(1, 3);
        assertThat(narrowed.get("tools").get(0).get("name").asText()).isEqualTo("search_documents");
        assertThat(chips(narrowed)).containsExactly("이 중 HBM 관련만", "1번 기사 원문 보기");
        assertThat(narrowed.get("followUps").get(1).get("url").asText()).isEqualTo("https://news.example.com/1");

        // 0건: 앞 근거(삼성전자 2건, 모두 예시일보) 안에 이데일리 기사는 없다
        LLM.then(toolCall("w2", "search_documents", "{\"within_previous\":true,\"site\":\"이데일리\"}")).then(body -> {
            JsonNode result = lastToolResult(body);
            assertThat(result.path("count").asInt()).isZero();
            assertThat(result.path("note").asText()).contains("앞 답의 근거 2건 중").contains("새로 찾아볼지");
            return text("앞 목록에는 이데일리 기사가 없습니다. 새로 찾아볼까요?");
        });
        JsonNode none = send("viewer", s, "그중 이데일리 것만").get("assistant");
        assertThat(none.get("citations")).isEmpty();
        assertThat(chips(none)).containsExactly("‘이데일리’ 기사 새로 찾아 줘");

        // 앞 답의 번호: 근거가 있는 가장 최근 답(삼성전자 2건 — [1]·[3])의 3번. 없는 번호면 고를 수 있는 번호를 알려 준다
        LLM.then(toolCall("w3", "search_documents", "{\"previous_refs\":[2]}")).then(body -> {
            assertThat(lastToolResult(body).path("note").asText()).contains("앞 답의 근거 번호는 1, 3입니다");
            return toolCall("w4", "search_documents", "{\"previous_refs\":[3]}");
        }).then(body -> {
            JsonNode result = lastToolResult(body);
            assertThat(titles(result)).containsExactly("메모리 반도체 가격 반등");
            assertThat(result.path("documents").get(0).path("ref").asInt()).isEqualTo(3);
            return text("[3] 메모리 반도체 가격 반등 기사입니다.");
        });
        JsonNode picked = send("viewer", s, "3번 기사만 다시 보여 줘").get("assistant");
        assertThat(picked.get("citations")).extracting(c -> c.get("documentId").asLong()).containsExactly(d3);
        assertThat(picked.get("citations").get(0).get("refNo").asInt()).isEqualTo(3);
        assertThat(LLM.pending()).isZero();

        // 근거가 없는 대화에서는 거를 수 없다고 알려 준다
        LLM.then(toolCall("w4", "search_documents", "{\"within_previous\":true,\"query\":\"삼성전자\"}")).then(body -> {
            assertThat(lastToolResult(body).path("note").asText()).contains("앞 답에 근거 목록이 없어");
            return text("앞 목록이 없습니다.");
        });
        send("viewer", session("viewer"), "그중 삼성전자만");
        assertThat(LLM.pending()).isZero();
    }

    /** 이슈 목록 답 → "1번 이슈 언론사 목록"(칩): previous_refs의 이슈 번호는 그 이슈에 묶인 기사들이다. */
    @Test
    void issueReferenceExpandsToTheIssueArticles() {
        long press = siteSource("news.example.com", "예시일보");
        long other = siteSource("edaily.example.com", "이데일리");
        long d1 = document(press, "https://news.example.com/1", "반도체 수출 사상 최대", "[]", 3);
        long d2 = document(other, "https://edaily.example.com/2", "반도체 수출 최대 기록", "[]", 2);
        long issue = jdbc.sql("""
                INSERT INTO issue (title, first_seen_at, last_seen_at, doc_count, source_count, top_terms)
                VALUES ('반도체 수출 사상 최대', now() - interval '2 hours', now() - interval '1 hour', 2, 2, '["반도체","수출"]')
                RETURNING id
                """).query(Long.class).single();
        jdbc.sql("INSERT INTO issue_document (document_id, issue_id, similarity) VALUES (?, ?, 1), (?, ?, 0.8)")
                .params(d1, issue, d2, issue).update();

        LLM.then(toolCall("i1", "list_issues", "{}")).then(text("오늘의 주요 이슈입니다.\n1. 반도체 수출 사상 최대 [1]"));
        long s = session("viewer");
        JsonNode issues = send("viewer", s, "오늘 주요 이슈 알려 줘").get("assistant");
        assertThat(issues.get("citations").get(0).get("kind").asText()).isEqualTo("ISSUE");
        assertThat(chips(issues)).contains("1번 이슈 언론사 목록", "‘반도체’ 관련 기사 찾아 줘");

        LLM.then(toolCall("i2", "search_documents", "{\"within_previous\":true,\"previous_refs\":[1]}")).then(body -> {
            JsonNode result = lastToolResult(body);
            assertThat(titles(result)).containsExactly("반도체 수출 사상 최대", "반도체 수출 최대 기록");
            List<String> sources = new ArrayList<>();
            result.path("documents").forEach(d -> sources.add(d.path("source").asText()));
            assertThat(sources).containsExactly("예시일보", "이데일리");
            return text("1번 이슈는 예시일보 [1]·이데일리 [2]가 보도했습니다.");
        });
        JsonNode outlets = send("viewer", s, "1번 이슈 언론사 목록").get("assistant");
        assertThat(outlets.get("citations")).extracting(c -> c.get("documentId").asLong()).containsExactly(d1, d2);
        assertThat(LLM.pending()).isZero();
    }
}
