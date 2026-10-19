package io.sensinghub;

import static org.assertj.core.api.Assertions.assertThat;

import java.util.ArrayList;
import java.util.List;

import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.AfterEach;
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

import io.sensinghub.llm.MockLlmServer;
import io.sensinghub.llm.MockLlmServer.Reply;

/**
 * 챗봇 1단계: 도구 호출 루프 → 기존 서비스(검색·감시·등록) → 저장(chat_message)·근거(chat_citation), 세션 권한, LLM 오류(429) 처리,
 * 도구 왕복 상한, 붙여 넣은 키 가리기. LLM은 가짜 OpenAI 호환 서버({@link MockLlmServer})로 흉내 내 실제 호출이 없다.
 * 실제 PostgreSQL이 필요하며 SENSING_IT_DB_URL이 있을 때만 실행된다.
 */
@EnabledIfEnvironmentVariable(named = "SENSING_IT_DB_URL", matches = "jdbc:postgresql:.+")
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT,
        properties = {"sensing.scheduler.enabled=false", "sensing.digest.enabled=false"})
class ChatIT {

    static final MockLlmServer LLM = MockLlmServer.start();
    static final ObjectMapper JSON = new ObjectMapper();

    @DynamicPropertySource
    static void props(DynamicPropertyRegistry r) {
        r.add("spring.datasource.url", () -> System.getenv("SENSING_IT_DB_URL"));
        r.add("spring.flyway.clean-disabled", () -> "false");
        r.add("sensing.secret-key", () -> "it-secret-key");
        r.add("sensing.llm.provider", () -> "openai");
        r.add("sensing.llm.base-url", () -> LLM.baseUrl() + "/v1");
        r.add("sensing.llm.model", () -> "mock-model");
        r.add("sensing.llm.api-key", () -> "test-key");
        r.add("sensing.llm.timeout-seconds", () -> "10");
        r.add("sensing.llm.retry-delay-ms", () -> "0");
        // 의미 검색: 주소를 따로 주지 않았으므로 LLM과 같은 가짜 서버·같은 키로 /v1/embeddings를 부른다
        r.add("sensing.embed.provider", () -> "openai");
        r.add("sensing.embed.model", () -> "mock-embed");
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
    }

    // ------------------------------------------------------------------ 도우미

    private ResponseEntity<JsonNode> call(String user, HttpMethod method, String path, Object body) {
        HttpHeaders h = new HttpHeaders();
        h.set("X-User", user);
        h.setContentType(MediaType.APPLICATION_JSON);
        return http.exchange(path, method, new HttpEntity<>(body, h), JsonNode.class);
    }

    private long session(String user) {
        ResponseEntity<JsonNode> res = call(user, HttpMethod.POST, "/api/v1/chat/sessions", java.util.Map.of());
        assertThat(res.getStatusCode()).isEqualTo(HttpStatus.CREATED);
        return res.getBody().get("id").asLong();
    }

    private JsonNode send(String user, long session, String content) {
        ResponseEntity<JsonNode> res = call(user, HttpMethod.POST, "/api/v1/chat/sessions/" + session + "/messages",
                java.util.Map.of("content", content));
        assertThat(res.getStatusCode()).as(String.valueOf(res.getBody())).isEqualTo(HttpStatus.OK);
        return res.getBody();
    }

    private static Reply toolCall(String id, String name, String args) {
        return Reply.ok(MockLlmServer.openAiToolCall(id, name, args, null));
    }

    private static Reply text(String text) {
        return Reply.ok(MockLlmServer.openAiText(text));
    }

    /** 요청의 messages 중 role인 것들. */
    private static List<JsonNode> messages(JsonNode request, String role) {
        List<JsonNode> out = new ArrayList<>();
        request.path("messages").forEach(m -> {
            if (role.equals(m.path("role").asText())) {
                out.add(m);
            }
        });
        return out;
    }

    private static JsonNode parse(String s) {
        try {
            return JSON.readTree(s);
        } catch (Exception e) {
            throw new AssertionError(e);
        }
    }

    private long siteSource(String domain, String siteType, String name, boolean aiRestricted) {
        long siteId = jdbc.sql("""
                INSERT INTO site (domain, base_url, site_type, name, ai_restricted) VALUES (?, ?, ?, ?, ?) RETURNING id
                """).params(domain, "https://" + domain, siteType, name, aiRestricted).query(Long.class).single();
        return jdbc.sql("INSERT INTO source (site_id, name, status) VALUES (?, ?, 'ACTIVE') RETURNING id")
                .params(siteId, domain).query(Long.class).single();
    }

    private long document(long sourceId, String url, String title, String publishedAt, String summary, String topics) {
        long id = jdbc.sql("""
                INSERT INTO document (source_id, url, url_hash, title, published_at)
                VALUES (?, ?, md5(?) || md5(?), ?, CAST(? AS timestamptz)) RETURNING id
                """).params(sourceId, url, url, url, title, publishedAt).query(Long.class).single();
        jdbc.sql("""
                INSERT INTO document_enrichment (document_id, document_version, summary, topics)
                VALUES (?, 1, ?, CAST(? AS jsonb))
                """).params(id, summary, topics).update();
        return id;
    }

    private List<String> roles(long session) {
        return jdbc.sql("SELECT role FROM chat_message WHERE session_id = ? ORDER BY seq").param(session)
                .query(String.class).list();
    }

    // ------------------------------------------------------------------ 시나리오

    @Test
    void toolLoopSearchesDocumentsAndStoresCitations() {
        long press = siteSource("news.example.com", "NEWS", "예시일보", false);
        long gov = siteSource("gov.example.go.kr", "PUBLIC", "예시부", false);
        long closed = siteSource("closed.example.go.kr", "PUBLIC", "AI차단청", true);
        long article = document(press, "https://news.example.com/1", "반도체 수출 사상 최대", "2026-10-16T09:00:00+09:00",
                null, "[\"산업동향\"]");
        document(gov, "https://gov.example.go.kr/1", "반도체 장비 국산화 지원사업 공고", "2026-10-15T09:00:00+09:00",
                "반도체 장비 국산화 과제를 10월 31일까지 모집한다.", "[\"지원사업\"]");
        document(closed, "https://closed.example.go.kr/1", "반도체 인력 양성 계획", "2026-10-14T09:00:00+09:00",
                "AI에 보내면 안 되는 요약", "[\"정책·규제\"]");

        LLM.then(Reply.ok(MockLlmServer.openAiToolCall("call-1", "search_documents", "{\"query\":\"반도체\"}", "sig-1")))
                .then(text("반도체 소식입니다.\n- 수출 사상 최대 [1]\n- 장비 국산화 지원사업 모집 [2]"));
        long s = session("registrar");
        JsonNode turn = send("registrar", s, "반도체 관련 소식 알려 줘");

        JsonNode answer = turn.get("assistant");
        assertThat(answer.get("error").asBoolean()).isFalse();
        assertThat(answer.get("content").asText()).contains("[1]").contains("[2]");
        assertThat(answer.get("citations")).hasSize(2);
        JsonNode first = answer.get("citations").get(0);
        assertThat(first.get("refNo").asInt()).isEqualTo(1);
        assertThat(first.get("kind").asText()).isEqualTo("DOCUMENT");
        assertThat(first.get("documentId").asLong()).isEqualTo(article);
        assertThat(first.get("title").asText()).isEqualTo("반도체 수출 사상 최대");
        assertThat(first.get("url").asText()).isEqualTo("https://news.example.com/1");
        assertThat(first.get("sourceLabel").asText()).isEqualTo("예시일보");
        assertThat(answer.get("citations").get(1).get("sourceLabel").asText()).isEqualTo("예시부");
        assertThat(answer.get("tools").get(0).get("name").asText()).isEqualTo("search_documents");
        assertThat(answer.get("tools").get(0).get("ok").asBoolean()).isTrue();
        assertThat(turn.get("session").get("title").asText()).isEqualTo("반도체 관련 소식 알려 줘");

        // LLM에 보낸 것: 시스템 원칙, 도구 정의, 도구 결과(언론·AI 차단 사이트는 요약 없이), Gemini 서명 되돌려 주기
        List<MockLlmServer.Recorded> requests = LLM.requests();
        assertThat(requests).hasSize(2);
        assertThat(requests.get(0).header("Authorization")).isEqualTo("Bearer test-key");
        JsonNode firstReq = requests.get(0).body();
        assertThat(messages(firstReq, "system").get(0).path("content").asText())
                .contains("도구 결과에 없는").contains("제목·링크만").contains("API 키는 채팅으로 받지 않는다");
        List<String> toolNames = new ArrayList<>();
        firstReq.path("tools").forEach(t -> toolNames.add(t.path("function").path("name").asText()));
        assertThat(toolNames).containsExactly("search_documents", "list_issues", "list_watches", "create_watch",
                "register_source", "get_source_status");
        JsonNode secondReq = requests.get(1).body();
        JsonNode echoed = messages(secondReq, "assistant").get(0).path("tool_calls").path(0);
        assertThat(echoed.path("extra_content").path("google").path("thought_signature").asText()).isEqualTo("sig-1");
        JsonNode result = parse(messages(secondReq, "tool").get(0).path("content").asText());
        assertThat(result.path("count").asInt()).isEqualTo(3);
        JsonNode docs = result.path("documents");
        assertThat(docs.get(0).path("ref").asInt()).isEqualTo(1);
        assertThat(docs.get(0).path("type").asText()).isEqualTo("언론");
        assertThat(docs.get(0).has("summary")).isFalse();
        assertThat(docs.get(0).has("url")).isFalse();
        assertThat(docs.get(1).path("type").asText()).isEqualTo("공공기관");
        assertThat(docs.get(1).path("summary").asText()).contains("10월 31일");
        assertThat(docs.get(2).has("summary")).isFalse();
        assertThat(secondReq.toString()).doesNotContain("AI에 보내면 안 되는 요약");

        // 저장: 모든 단계와 근거
        assertThat(roles(s)).containsExactly("USER", "ASSISTANT", "TOOL", "ASSISTANT");
        assertThat(jdbc.sql("SELECT tool_calls->0->>'name' FROM chat_message WHERE session_id = ? AND seq = 2").param(s)
                .query(String.class).single()).isEqualTo("search_documents");
        assertThat(jdbc.sql("SELECT meta->>'toolCallId' FROM chat_message WHERE session_id = ? AND role = 'TOOL'").param(s)
                .query(String.class).single()).isEqualTo("call-1");
        assertThat(jdbc.sql("SELECT count(*) FROM chat_citation").query(Long.class).single()).isEqualTo(2);

        // 대화 불러오기: 질문과 답만, 답에 근거·도구
        JsonNode detail = call("registrar", HttpMethod.GET, "/api/v1/chat/sessions/" + s, null).getBody();
        assertThat(detail.get("messages")).hasSize(2);
        assertThat(detail.get("messages").get(1).get("citations")).hasSize(2);
        assertThat(detail.get("messages").get(1).get("tools").get(0).get("label").asText()).isEqualTo("문서 검색");

        // 다음 턴은 지난 턴(도구 호출·결과 포함)을 이어서 본다
        LLM.then(body -> {
            assertThat(messages(body, "user")).hasSize(2);
            assertThat(messages(body, "tool")).hasSize(1);
            assertThat(messages(body, "assistant")).hasSize(2);
            return text("첫 번째 기사는 예시일보 보도입니다 [1]");
        });
        JsonNode next = send("registrar", s, "첫 번째는 어디 기사야?");
        assertThat(next.get("assistant").get("error").asBoolean()).isFalse();
        // 도구 없이 답한 후속 답의 [1]은 앞 답의 근거 1번이다
        assertThat(next.get("assistant").get("citations")).hasSize(1);
        assertThat(next.get("assistant").get("citations").get(0).get("url").asText()).isEqualTo("https://news.example.com/1");

        // 도구를 불렀지만 문서가 없는 답(감시 목록)에 붙은 엉뚱한 [1]은 옛 기사로 이어지지 않는다
        LLM.then(toolCall("call-2", "list_watches", "{}")).then(text("감시가 없습니다 [1]"));
        assertThat(send("registrar", s, "내 감시는?").get("assistant").get("citations")).isEmpty();
        assertThat(LLM.pending()).isZero();
    }

    /** 워커가 임베딩한 제목 조각을 흉내 낸다 (모델 mock-embed, 1024차원). */
    private void embeddedTitle(long documentId, String title, float[] vector) {
        StringBuilder literal = new StringBuilder("[");
        for (int i = 0; i < vector.length; i++) {
            literal.append(i == 0 ? "" : ",").append(vector[i]);
        }
        jdbc.sql("""
                INSERT INTO chunk (document_id, seq, text, embedding, index_version, embed_model, embedded_at)
                VALUES (?, 0, ?, CAST(? AS vector), 'fts-kiwi-v1', 'mock-embed/1024', now())
                """).params(documentId, title, literal.append(']').toString()).update();
    }

    @Test
    void searchToolAddsArticlesThatShareMeaningButNotWords() {
        long press = siteSource("news.example.com", "NEWS", "예시일보", false);
        long words = document(press, "https://news.example.com/1", "배드민턴협회 감사 결과 발표", "2026-10-16T09:00:00+09:00",
                null, "[\"사건·사고\"]");
        embeddedTitle(words, "배드민턴협회 감사 결과 발표", MockLlmServer.vector(5, 1));
        long meaning = document(press, "https://news.example.com/2", "안세영, 덴마크오픈 우승…시즌 9번째 트로피",
                "2026-10-15T09:00:00+09:00", null, "[\"기타\"]");
        embeddedTitle(meaning, "안세영, 덴마크오픈 우승…시즌 9번째 트로피", MockLlmServer.vector(0, 0.9f, 2, 0.43589f));
        long unrelated = document(press, "https://news.example.com/3", "가을 축제 개막", "2026-10-17T09:00:00+09:00",
                null, "[\"기타\"]");
        embeddedTitle(unrelated, "가을 축제 개막", MockLlmServer.vector(0, 0.3f, 3, 0.95394f));

        LLM.then(toolCall("call-s", "search_documents", "{\"query\":\"배드민턴\"}"))
                .then(body -> {
                    assertThat(body.path("input").get(0).asText()).isEqualTo("배드민턴");
                    assertThat(body.path("model").asText()).isEqualTo("mock-embed");
                    return Reply.ok(MockLlmServer.openAiEmbedding(MockLlmServer.vector(0, 1)));
                })
                .then(body -> {
                    JsonNode r = parse(messages(body, "tool").get(0).path("content").asText());
                    assertThat(r.path("order").asText()).startsWith("관련도순");
                    JsonNode docs = r.path("documents");
                    assertThat(docs).hasSize(2);                 // 유사도 하한(0.58) 아래인 '가을 축제'는 빠진다
                    assertThat(docs.get(0).path("title").asText()).isEqualTo("배드민턴협회 감사 결과 발표");
                    assertThat(docs.get(0).path("match").asText()).isEqualTo("낱말");
                    assertThat(docs.get(1).path("title").asText()).startsWith("안세영");
                    assertThat(docs.get(1).path("match").asText()).isEqualTo("의미");
                    assertThat(docs.get(1).has("url")).isFalse();
                    assertThat(docs.get(1).has("summary")).isFalse();
                    assertThat(r.path("note").asText()).contains("뜻만 가까운");
                    return text("배드민턴 소식: 협회 감사 결과 [1], 안세영 덴마크오픈 우승 [2]");
                });
        long s = session("viewer");
        JsonNode answer = send("viewer", s, "배드민턴 소식 있어?").get("assistant");

        assertThat(answer.get("error").asBoolean()).isFalse();
        assertThat(answer.get("citations")).hasSize(2);
        assertThat(answer.get("citations").get(1).get("documentId").asLong()).isEqualTo(meaning);
        assertThat(answer.get("citations").get(1).get("url").asText()).isEqualTo("https://news.example.com/2");
        List<MockLlmServer.Recorded> requests = LLM.requests();
        assertThat(requests).extracting(MockLlmServer.Recorded::path)
                .containsExactly("/v1/chat/completions", "/v1/embeddings", "/v1/chat/completions");
        assertThat(requests.get(1).header("Authorization")).isEqualTo("Bearer test-key");   // LLM 키를 함께 쓴다
        assertThat(LLM.pending()).isZero();
    }

    /** 기사 종류(document.section)가 있는 문서. */
    private long document(long sourceId, String url, String title, String publishedAt, String section) {
        long id = document(sourceId, url, title, publishedAt, null, "[]");
        jdbc.sql("UPDATE document SET section = ? WHERE id = ?").params(section, id).update();
        return id;
    }

    private static JsonNode lastTool(JsonNode request) {
        List<JsonNode> tools = messages(request, "tool");
        return parse(tools.get(tools.size() - 1).path("content").asText());
    }

    @Test
    void searchToolFiltersBySiteAndArticleKindAndTellsTheNamesItCanUse() {
        long korea = siteSource("korea.kr", "PUBLIC", null, false);       // 운영처럼 사이트 이름이 비어 있다
        long donga = siteSource("donga.com", "NEWS", "동아일보", false);
        long s1 = document(korea, "https://korea.kr/s1", "제44기 대통령기 이북도민 체육대회 축사", "2026-10-18T10:00:00+09:00", "연설문");
        long s2 = document(korea, "https://korea.kr/s2", "제107회 전국체육대회 기념사", "2026-10-16T10:00:00+09:00", "연설문");
        long s3 = document(korea, "https://korea.kr/s3", "국군의 날 기념사", "2026-10-01T10:00:00+09:00", "연설문");
        document(korea, "https://korea.kr/s4", "현충일 추념사", "2026-06-06T10:00:00+09:00", "연설문");
        document(korea, "https://korea.kr/p1", "체육대회 안전관리 강화", "2026-10-18T12:00:00+09:00", "보도자료");
        document(korea, "https://korea.kr/n1", "생활체육 지원 확대", "2026-10-17T12:00:00+09:00", "정책뉴스");
        document(donga, "https://donga.com/1", "전국체전 개막", "2026-10-18T13:00:00+09:00", "스포츠");

        // 1) 사이트·종류 이름을 검색어에 넣으면 낱말이 맞지 않는다 → 종류 이름이라고 알려 준다
        LLM.then(toolCall("k1", "search_documents", "{\"query\":\"정책브리핑 연설문\"}"))
                .then(body -> {
                    JsonNode r = lastTool(body);
                    assertThat(r.path("count").asInt()).isZero();
                    assertThat(r.path("note").asText()).contains("'연설문'은(는) 기사 종류 이름입니다").contains("section");
                    // 2) 사이트 이름을 모른다 (korea.kr은 이름이 없다) → 고를 수 있는 사이트를 준다
                    return toolCall("k2", "search_documents", "{\"site\":\"정책브리핑\",\"section\":\"연설문\"}");
                })
                .then(body -> {
                    JsonNode r = lastTool(body);
                    assertThat(r.path("ok").asBoolean()).isTrue();
                    assertThat(r.path("documents")).isEmpty();
                    assertThat(r.path("note").asText()).contains("맞는 수집 사이트가 없습니다");
                    assertThat(r.path("sites").toString()).contains("korea.kr").contains("동아일보(donga.com)");
                    // 3) 도메인 + 종류 이름 일부, 최근 3개
                    return toolCall("k3", "search_documents", "{\"site\":\"korea.kr\",\"section\":\"연설\",\"limit\":3}");
                })
                .then(body -> {
                    JsonNode r = lastTool(body);
                    assertThat(r.path("sites").get(0).asText()).isEqualTo("korea.kr");
                    assertThat(r.path("sections").get(0).asText()).isEqualTo("연설문");
                    assertThat(r.path("note").asText()).contains("'연설'을(를) '연설문'(으)로 찾았습니다");
                    assertThat(r.path("order").asText()).isEqualTo("최신순");
                    JsonNode docs = r.path("documents");
                    assertThat(docs).hasSize(3);
                    assertThat(docs.get(0).path("title").asText()).isEqualTo("제44기 대통령기 이북도민 체육대회 축사");
                    assertThat(docs.get(0).path("section").asText()).isEqualTo("연설문");
                    assertThat(docs.get(0).path("source").asText()).isEqualTo("korea.kr");
                    assertThat(docs.get(2).path("title").asText()).isEqualTo("국군의 날 기념사");
                    assertThat(r.path("more").asText()).contains("더 있을 수 있습니다");
                    List<String> kinds = new ArrayList<>();
                    r.path("site_sections").forEach(k -> kinds.add(k.asText()));
                    assertThat(kinds).containsExactly("연설문", "보도자료", "정책뉴스");
                    return text("정책브리핑 최근 연설문입니다.\n- 이북도민 체육대회 축사 [1]\n- 전국체육대회 기념사 [2]\n- 국군의 날 기념사 [3]");
                });
        long s = session("viewer");
        JsonNode answer = send("viewer", s, "정책브리핑에서 최근 올라온 연설문 3개만 알려 줘").get("assistant");

        assertThat(answer.get("error").asBoolean()).isFalse();
        assertThat(answer.get("tools")).hasSize(3);
        List<Long> cited = new ArrayList<>();
        answer.get("citations").forEach(c -> cited.add(c.get("documentId").asLong()));
        assertThat(cited).containsExactly(s1, s2, s3);
        assertThat(answer.get("citations").get(0).get("url").asText()).isEqualTo("https://korea.kr/s1");
        assertThat(answer.get("citations").get(0).get("sourceLabel").asText()).isEqualTo("korea.kr");
        assertThat(LLM.pending()).isZero();

        // 시스템 프롬프트와 도구 정의가 사이트·종류 조건을 알려 준다
        JsonNode first = LLM.requests().get(0).body();
        assertThat(messages(first, "system").get(0).path("content").asText()).contains("site·section으로 거르고");
        JsonNode searchTool = first.path("tools").get(0).path("function");
        assertThat(searchTool.path("parameters").path("properties").has("site")).isTrue();
        assertThat(searchTool.path("parameters").path("properties").has("section")).isTrue();
        assertThat(searchTool.path("description").asText()).contains("연설문·보도자료·정책뉴스").contains("query는 비운다");

        // 화면 API도 같은 조건: 종류는 여러 번 줄 수 있고(쉼표는 나누지 않는다), 사이트는 도메인·이름
        JsonNode both = call("viewer", HttpMethod.GET, "/api/v1/documents?site=korea.kr&section=연설문&section=보도자료",
                null).getBody();
        assertThat(both).hasSize(5);
        assertThat(call("viewer", HttpMethod.GET, "/api/v1/documents?site=동아일보", null).getBody()).hasSize(1);
        assertThat(call("viewer", HttpMethod.GET, "/api/v1/documents?section=연설문,보도자료", null).getBody()).isEmpty();
        JsonNode counts = call("viewer", HttpMethod.GET, "/api/v1/documents/sections?site=korea.kr", null).getBody();
        assertThat(counts.path("연설문").asInt()).isEqualTo(4);
        assertThat(counts.has("스포츠")).isFalse();

        // 사이트만 주면(실측: '브리핑 자료'를 묻고 종류를 빼먹었다) 종류를 거르지 않았다는 것과 다시 찾는 방법을 알려 준다
        LLM.then(toolCall("k4", "search_documents", "{\"site\":\"korea.kr\",\"limit\":2}"))
                .then(body -> {
                    JsonNode r = lastTool(body);
                    assertThat(r.path("documents")).hasSize(2);
                    assertThat(r.path("note").asText()).contains("종류를 거르지 않은 최신 문서").contains("section");
                    return text("최근 문서 2건입니다 [1][2]");
                });
        send("viewer", s, "정책브리핑 최근 문서 2개");
        assertThat(LLM.pending()).isZero();
    }

    @Test
    void sessionsAreVisibleOnlyToTheirOwner() {
        long s = session("registrar");
        assertThat(call("viewer", HttpMethod.GET, "/api/v1/chat/sessions/" + s, null).getStatusCode())
                .isEqualTo(HttpStatus.NOT_FOUND);
        assertThat(call("admin", HttpMethod.GET, "/api/v1/chat/sessions/" + s, null).getStatusCode())
                .isEqualTo(HttpStatus.NOT_FOUND);
        assertThat(call("viewer", HttpMethod.POST, "/api/v1/chat/sessions/" + s + "/messages",
                java.util.Map.of("content", "남의 대화")).getStatusCode()).isEqualTo(HttpStatus.NOT_FOUND);
        assertThat(call("viewer", HttpMethod.GET, "/api/v1/chat/sessions/" + s + "/messages", null).getStatusCode())
                .isEqualTo(HttpStatus.NOT_FOUND);
        assertThat(call("viewer", HttpMethod.GET, "/api/v1/chat/sessions", null).getBody()).isEmpty();
        assertThat(call("registrar", HttpMethod.GET, "/api/v1/chat/sessions", null).getBody()).hasSize(1);
        assertThat(LLM.requests()).isEmpty();

        assertThat(call("viewer", HttpMethod.DELETE, "/api/v1/chat/sessions/" + s, null).getStatusCode())
                .isEqualTo(HttpStatus.NOT_FOUND);
        assertThat(call("registrar", HttpMethod.DELETE, "/api/v1/chat/sessions/" + s, null).getStatusCode())
                .isEqualTo(HttpStatus.NO_CONTENT);
        assertThat(call("registrar", HttpMethod.GET, "/api/v1/chat/sessions", null).getBody()).isEmpty();
        assertThat(call("registrar", HttpMethod.GET, "/api/v1/chat/sessions/" + s, null).getStatusCode())
                .isEqualTo(HttpStatus.NOT_FOUND);

        // 빈 메시지는 400
        long other = session("viewer");
        assertThat(call("viewer", HttpMethod.POST, "/api/v1/chat/sessions/" + other + "/messages",
                java.util.Map.of("content", "  ")).getStatusCode()).isEqualTo(HttpStatus.BAD_REQUEST);
        JsonNode status = call("viewer", HttpMethod.GET, "/api/v1/chat/status", null).getBody();
        assertThat(status.get("configured").asBoolean()).isTrue();
        assertThat(status.get("provider").asText()).isEqualTo("openai mock-model");
        assertThat(status.get("search").asText()).isEqualTo("낱말+의미 (mock-embed/1024)");
    }

    @Test
    void chatCreatesWatchesAndRegistersSitesThroughTheSameServicesAsTheScreen() {
        long s = session("registrar");
        LLM.then(toolCall("c1", "create_watch", "{\"name\":\"자사 보도\",\"keywords\":[\"메타넷\",\"메타넷글로벌\"]}"))
                .then(text("'자사 보도' 감시를 만들었습니다. 수집 기사에 키워드를 대조합니다."));
        JsonNode created = send("registrar", s, "메타넷 보도 감시 만들어 줘. 이름은 자사 보도");
        assertThat(created.get("assistant").get("tools").get(0).get("link").asText()).isEqualTo("#/watches");
        assertThat(jdbc.sql("SELECT status FROM source WHERE kind = 'WATCH' AND watch_name = '자사 보도'")
                .query(String.class).single()).isEqualTo("ACTIVE");
        assertThat(jdbc.sql("SELECT channel || ':' || status FROM registration_request WHERE input_url = 'watch:자사 보도'")
                .query(String.class).single()).isEqualTo("CHAT:COMPLETED");
        JsonNode watchResult = parse(jdbc.sql("SELECT content FROM chat_message WHERE session_id = ? AND role = 'TOOL'")
                .param(s).query(String.class).single());
        assertThat(watchResult.path("ok").asBoolean()).isTrue();
        assertThat(watchResult.path("watch").path("mode").asText()).isEqualTo("수집 기사 대조");

        // 같은 이름은 화면과 같은 규칙으로 거절되고, 오류는 도구 결과로 LLM에 간다
        LLM.then(toolCall("c2", "create_watch", "{\"name\":\"자사 보도\",\"keywords\":[\"메타넷\"]}"))
                .then(body -> {
                    JsonNode r = parse(messages(body, "tool").get(messages(body, "tool").size() - 1).path("content").asText());
                    assertThat(r.path("ok").asBoolean()).isFalse();
                    assertThat(r.path("error").asText()).contains("같은 이름의 감시가 이미 있습니다");
                    return text("같은 이름의 감시가 이미 있습니다.");
                });
        assertThat(send("registrar", s, "같은 걸 하나 더").get("assistant").get("tools").get(0).get("ok").asBoolean()).isFalse();

        // 사이트 등록: Channel.CHAT → 등록 요청 채널 CHAT, 작업 trigger CHAT
        LLM.then(toolCall("c3", "register_source", "{\"url\":\"https://news.example.org/article/1\"}"))
                .then(text("등록 요청을 접수했어요. 점검 중입니다."));
        send("registrar", s, "news.example.org 수집해 줘");
        assertThat(jdbc.sql("SELECT channel || ':' || status FROM registration_request WHERE normalized_domain = 'news.example.org'")
                .query(String.class).single()).isEqualTo("CHAT:PROCESSING");
        assertThat(jdbc.sql("""
                SELECT j.trigger FROM crawl_job j JOIN source s ON s.id = j.source_id JOIN site t ON t.id = s.site_id
                WHERE t.domain = 'news.example.org'
                """).query(String.class).single()).isEqualTo("CHAT");

        // 진행 상태: 워커가 API 키 대기로 멈췄다 → 키 안내와 관리자 화면 링크 (키는 받지 않는다)
        jdbc.sql("UPDATE source SET status = 'REVIEW', status_reason = 'API_KEY_REQUIRED' WHERE name = 'news.example.org'").update();
        jdbc.sql("UPDATE registration_request SET status = 'REVIEW', result_message = '공시 API 키가 필요합니다. 발급: https://opendart.fss.or.kr' "
                + "WHERE normalized_domain = 'news.example.org'").update();
        LLM.then(toolCall("c4", "get_source_status", "{\"url\":\"news.example.org\"}"))
                .then(body -> {
                    JsonNode r = parse(messages(body, "tool").get(messages(body, "tool").size() - 1).path("content").asText());
                    JsonNode req = r.path("requests").path(0);
                    assertThat(req.path("status").asText()).isEqualTo("확인 필요(관리자 검토)");
                    assertThat(req.path("message").asText()).contains("opendart");
                    assertThat(req.path("source").path("api_key").path("guide").asText()).contains("채팅으로 받지 않습니다");
                    return text("API 키가 필요합니다. 관리자가 'API 키' 화면에서 등록하면 이어서 진행됩니다.");
                });
        JsonNode status = send("registrar", s, "아까 등록한 거 어떻게 됐어?");
        assertThat(status.get("assistant").get("tools").get(0).get("link").asText()).isEqualTo("#/admin/credentials");

        // 일반 사용자의 감시 요청은 화면과 같이 관리자 승인 대기
        long v = session("viewer");
        LLM.then(toolCall("c5", "create_watch", "{\"name\":\"경쟁사\",\"keywords\":\"A사, B사\"}"))
                .then(text("요청이 접수되어 관리자 승인을 기다립니다."));
        send("viewer", v, "A사, B사 감시 만들어 줘. 이름은 경쟁사");
        assertThat(jdbc.sql("SELECT channel || ':' || status FROM registration_request WHERE input_url = 'watch:경쟁사'")
                .query(String.class).single()).isEqualTo("CHAT:AWAITING_APPROVAL");

        // 내 감시 목록은 자기 것만
        LLM.then(toolCall("c6", "list_watches", "{}"))
                .then(body -> {
                    JsonNode r = parse(messages(body, "tool").get(0).path("content").asText());
                    assertThat(r.path("count").asInt()).isEqualTo(1);
                    assertThat(r.path("watches").path(0).path("name").asText()).isEqualTo("경쟁사");
                    return text("감시 1개: 경쟁사");
                });
        send("viewer", session("viewer"), "내 감시 보여 줘");
        assertThat(LLM.pending()).isZero();
    }

    @Test
    void rateLimitIsExplainedAndTheFailedTurnIsLeftOutOfHistory() {
        long s = session("viewer");
        LLM.then(Reply.status(429, MockLlmServer.GEMINI_DAILY_429));
        JsonNode failed = send("viewer", s, "주요 이슈 알려 줘");
        assertThat(failed.get("assistant").get("error").asBoolean()).isTrue();
        assertThat(failed.get("assistant").get("content").asText()).contains("하루 사용 한도");
        assertThat(jdbc.sql("SELECT meta->>'error' FROM chat_message WHERE session_id = ? AND role = 'ASSISTANT'").param(s)
                .query(String.class).single()).isEqualTo("RATE_LIMITED");

        // 도구를 한 번 부른 뒤 한도에 걸려도 그 턴은 저장되고 다음 턴에 다시 보내지 않는다
        LLM.then(toolCall("c1", "list_issues", "{}")).then(Reply.status(429, MockLlmServer.GEMINI_MINUTE_429));
        JsonNode midway = send("viewer", s, "그럼 어제 이슈는?");
        assertThat(midway.get("assistant").get("content").asText()).contains("약 42초 뒤");
        assertThat(midway.get("assistant").get("citations")).isEmpty();
        assertThat(roles(s)).containsExactly("USER", "ASSISTANT", "USER", "ASSISTANT", "TOOL", "ASSISTANT");

        LLM.then(body -> {
            List<JsonNode> users = messages(body, "user");
            assertThat(users).hasSize(1);
            assertThat(users.get(0).path("content").asText()).isEqualTo("다시 해 볼게");
            assertThat(messages(body, "tool")).isEmpty();
            return text("네, 다시 물어봐 주세요.");
        });
        JsonNode ok = send("viewer", s, "다시 해 볼게");
        assertThat(ok.get("assistant").get("error").asBoolean()).isFalse();

        JsonNode detail = call("viewer", HttpMethod.GET, "/api/v1/chat/sessions/" + s, null).getBody();
        assertThat(detail.get("messages")).hasSize(6);
        assertThat(detail.get("messages").get(1).get("error").asBoolean()).isTrue();
        assertThat(detail.get("messages").get(3).get("tools").get(0).get("name").asText()).isEqualTo("list_issues");
        assertThat(LLM.pending()).isZero();
    }

    @Test
    void toolRoundsAreCappedAndTheLastCallAsksForAnAnswerWithoutTools() {
        long s = session("registrar");
        for (int i = 1; i <= 4; i++) {
            LLM.then(toolCall("loop-" + i, "list_watches", "{}"));
        }
        LLM.then(body -> {
            assertThat(body.path("tool_choice").asText()).isEqualTo("none");
            assertThat(body.path("tools")).isNotEmpty();
            return text("감시가 없습니다.");
        });
        JsonNode turn = send("registrar", s, "감시 목록 계속 확인해 줘");
        assertThat(turn.get("assistant").get("content").asText()).isEqualTo("감시가 없습니다.");
        assertThat(LLM.requests()).hasSize(5);
        assertThat(LLM.requests().get(3).body().path("tool_choice").asText()).isEqualTo("auto");
        assertThat(jdbc.sql("SELECT count(*) FROM chat_message WHERE session_id = ? AND role = 'TOOL'").param(s)
                .query(Long.class).single()).isEqualTo(4);
        assertThat(jdbc.sql("SELECT (meta->>'toolRounds')::int FROM chat_message WHERE session_id = ? ORDER BY seq DESC LIMIT 1")
                .param(s).query(Integer.class).single()).isEqualTo(4);

        // 모르는 도구·빠진 인자는 도구 오류 결과로 돌아간다
        LLM.then(toolCall("x1", "delete_everything", "{}")).then(toolCall("x2", "register_source", "{}"))
                .then(body -> {
                    List<JsonNode> tools = messages(body, "tool");
                    assertThat(tools.get(tools.size() - 2).path("content").asText()).contains("알 수 없는 도구");
                    assertThat(tools.get(tools.size() - 1).path("content").asText()).contains("주소(url)가 필요합니다");
                    return text("어떤 주소를 등록할까요?");
                });
        assertThat(send("registrar", s, "등록해 줘").get("assistant").get("content").asText()).contains("주소");
    }

    @Test
    void pastedApiKeyIsMaskedBeforeSavingAndSending() {
        long s = session("admin");
        String key = "0123456789abcdef0123456789abcdef01234567";
        LLM.then(body -> {
            String user = messages(body, "user").get(0).path("content").asText();
            assertThat(user).doesNotContain(key).contains("저장하지 않음");
            return text("키는 채팅으로 받지 않습니다. 관리자 메뉴의 'API 키' 화면에서 등록해 주세요.");
        });
        JsonNode turn = send("admin", s, "DART 키 " + key + " 등록해 줘");
        assertThat(turn.get("notice").asText()).contains("저장하지 않고 지웠습니다");
        assertThat(turn.get("user").get("content").asText()).doesNotContain(key);
        assertThat(jdbc.sql("SELECT count(*) FROM chat_message WHERE content LIKE ?").param("%" + key + "%")
                .query(Long.class).single()).isZero();
        assertThat(jdbc.sql("SELECT title FROM chat_session WHERE id = ?").param(s).query(String.class).single())
                .doesNotContain(key);
        assertThat(jdbc.sql("SELECT count(*) FROM api_credential").query(Long.class).single()).isZero();
    }
}
