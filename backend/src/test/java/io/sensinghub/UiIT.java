package io.sensinghub;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.web.client.TestRestTemplate;
import org.springframework.http.HttpEntity;
import org.springframework.http.HttpHeaders;
import org.springframework.http.HttpMethod;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;

import com.fasterxml.jackson.databind.JsonNode;

/** 최소 화면: 정적 파일이 서비스되고 /api/v1/me 가 X-User 헤더의 권한을 돌려준다. */
@EnabledIfEnvironmentVariable(named = "SENSING_IT_DB_URL", matches = "jdbc:postgresql:.+")
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT,
        properties = {"sensing.scheduler.enabled=false", "sensing.digest.enabled=false"})
class UiIT {

    @DynamicPropertySource
    static void db(DynamicPropertyRegistry r) {
        r.add("spring.datasource.url", () -> System.getenv("SENSING_IT_DB_URL"));
        r.add("spring.flyway.clean-disabled", () -> "false");
        r.add("sensing.secret-key", () -> "it-secret-key");
        // 테스트 환경에 SENSING_LLM_PROVIDER가 있어도 실제 LLM을 부르지 않는다
        r.add("sensing.llm.provider", () -> "none");
    }

    @Autowired TestRestTemplate http;

    @Test
    void rootServesIndexHtmlAndStaticScripts() {
        ResponseEntity<String> index = http.getForEntity("/", String.class);
        assertThat(index.getStatusCode()).isEqualTo(HttpStatus.OK);
        assertThat(index.getBody()).contains("id=\"app\"");

        ResponseEntity<String> app = http.getForEntity("/app.js", String.class);
        assertThat(app.getStatusCode()).isEqualTo(HttpStatus.OK);
        assertThat(app.getBody()).contains("X-User");

        for (String script : new String[] {"registrations.js", "documents.js", "issues.js", "companies.js", "watches.js",
                "naver.js", "digests.js", "chat.js", "chat-dock.js", "admin.js", "ops.js", "caidentia.js", "app.css", "chat.css"}) {
            assertThat(http.getForEntity("/" + script, String.class).getStatusCode())
                    .as(script).isEqualTo(HttpStatus.OK);
        }
        // 어느 화면에서나 뜨는 챗봇 버튼·대화창
        assertThat(index.getBody()).contains("chat-dock.js").contains("ChatDock.mount()").contains("href=\"chat.css\"");
        // 네이버 뉴스 탭: 키가 없어도 메뉴는 보인다 (화면이 안내를 보여 준다)
        assertThat(index.getBody()).contains("href=\"#/naver\"").contains("naver.js");
        // 관리자 메뉴: 주요 키워드 (네이버 탭·챗봇 칩)
        assertThat(index.getBody()).contains("href=\"#/admin/keywords\"");
        // 일간 다이제스트 메뉴의 이름은 "구독" (주소 #/digests와 API 경로는 그대로)
        assertThat(index.getBody()).contains("data-path=\"/digests\">구독</a>").doesNotContain(">다이제스트</a>");
    }

    /** 챗봇만 있는 새 창: 메뉴 없이 같은 app.js(X-User)와 챗봇 공통 부품을 쓴다. */
    @Test
    void chatPopupPageServesChatOnly() {
        ResponseEntity<String> popup = http.getForEntity("/chat.html", String.class);
        assertThat(popup.getStatusCode()).isEqualTo(HttpStatus.OK);
        assertThat(popup.getBody())
                .contains("app.js").contains("chat.js").contains("chat-dock.js").contains("ChatDock.mountPopup(")
                .contains("href=\"chat.css\"")
                .doesNotContain("id=\"menu\"");
    }

    @Test
    void meReturnsRoleOfHeaderUser() {
        assertThat(me("admin").get("role").asText()).isEqualTo("ADMIN");
        assertThat(me("admin").get("loginId").asText()).isEqualTo("admin");
        assertThat(me("viewer").get("role").asText()).isEqualTo("VIEWER");

        // 헤더가 없으면 기본 사용자(registrar)
        ResponseEntity<JsonNode> none = http.getForEntity("/api/v1/me", JsonNode.class);
        assertThat(none.getStatusCode()).isEqualTo(HttpStatus.OK);
        assertThat(none.getBody().get("role").asText()).isEqualTo("REGISTRAR");
    }

    /** LLM 설정이 없으면(기본) 챗봇은 오류 대신 "설정되지 않았습니다" 안내를 답으로 저장한다. */
    @Test
    void chatWithoutLlmAnswersThatItIsNotConfigured() {
        HttpHeaders h = new HttpHeaders();
        h.set("X-User", "viewer");
        h.setContentType(org.springframework.http.MediaType.APPLICATION_JSON);
        JsonNode status = http.exchange("/api/v1/chat/status", HttpMethod.GET, new HttpEntity<>(h), JsonNode.class).getBody();
        assertThat(status.get("configured").asBoolean()).isFalse();

        ResponseEntity<JsonNode> created = http.exchange("/api/v1/chat/sessions", HttpMethod.POST,
                new HttpEntity<>("{}", h), JsonNode.class);
        assertThat(created.getStatusCode()).isEqualTo(HttpStatus.CREATED);
        long id = created.getBody().get("id").asLong();
        ResponseEntity<JsonNode> sent = http.exchange("/api/v1/chat/sessions/" + id + "/messages", HttpMethod.POST,
                new HttpEntity<>("{\"content\":\"오늘 이슈 알려 줘\"}", h), JsonNode.class);
        assertThat(sent.getStatusCode()).isEqualTo(HttpStatus.OK);
        JsonNode answer = sent.getBody().get("assistant");
        assertThat(answer.get("error").asBoolean()).isTrue();
        assertThat(answer.get("content").asText()).contains("LLM이 설정되지 않았습니다");
        assertThat(answer.get("errorKind").asText()).isEqualTo("NOT_CONFIGURED");

        // 키워드만 보낸 메시지는 LLM 없이도 수집 기사에서 바로 찾는다
        ResponseEntity<JsonNode> keyword = http.exchange("/api/v1/chat/sessions/" + id + "/messages", HttpMethod.POST,
                new HttpEntity<>("{\"content\":\"반도체\"}", h), JsonNode.class);
        assertThat(keyword.getStatusCode()).isEqualTo(HttpStatus.OK);
        assertThat(keyword.getBody().get("assistant").get("mode").asText()).isEqualTo("KEYWORD");
        assertThat(keyword.getBody().get("assistant").get("error").asBoolean()).isFalse();
        assertThat(keyword.getBody().get("assistant").get("content").asText()).startsWith("‘반도체’ 관련");
    }

    private JsonNode me(String user) {
        HttpHeaders h = new HttpHeaders();
        h.set("X-User", user);
        ResponseEntity<JsonNode> res = http.exchange("/api/v1/me", HttpMethod.GET, new HttpEntity<>(h), JsonNode.class);
        assertThat(res.getStatusCode()).isEqualTo(HttpStatus.OK);
        return res.getBody();
    }
}
