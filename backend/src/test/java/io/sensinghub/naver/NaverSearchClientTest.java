package io.sensinghub.naver;

import static org.assertj.core.api.Assertions.assertThat;

import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.Map;

import org.junit.jupiter.api.Test;

import com.fasterxml.jackson.databind.ObjectMapper;

import io.sensinghub.naver.NaverSearchClient.Kind;
import io.sensinghub.naver.NaverSearchClient.NaverSearchException;

/** 네이버 검색 응답 해석 (DB·네트워크 없이). 요청 형식·저장하지 않음은 NaverNewsIT가 가짜 HUB 서버로 본다. */
class NaverSearchClientTest {

    private final ObjectMapper json = new ObjectMapper();

    @Test
    void settingsDefaultToDisplayAndHubAndUnknownValuesFallBackSafely() {
        NaverSettings defaults = new NaverSettings("display", "hub", "", 10);
        assertThat(defaults.mode()).isEqualTo(NaverSettings.Mode.DISPLAY);
        assertThat(defaults.storeEnabled()).isFalse();
        assertThat(defaults.enabled()).isTrue();
        assertThat(defaults.url()).isEqualTo("https://naverapihub.apigw.ntruss.com/search/v1/news");
        assertThat(defaults.authHeaders("id", "secret"))
                .isEqualTo(Map.of("X-NCP-APIGW-API-KEY-ID", "id", "X-NCP-APIGW-API-KEY", "secret"));
        assertThat(defaults.extraParams()).containsExactly(Map.entry("format", "json"));

        NaverSettings legacy = new NaverSettings(" STORE ", "developers", "", 10);
        assertThat(legacy.storeEnabled()).isTrue();
        assertThat(legacy.url()).isEqualTo("https://openapi.naver.com/v1/search/news.json");
        assertThat(legacy.authHeaders("id", "secret"))
                .isEqualTo(Map.of("X-Naver-Client-Id", "id", "X-Naver-Client-Secret", "secret"));
        assertThat(legacy.extraParams()).isEmpty();

        // 모르는 모드는 저장으로 켜지지 않게 표시 전용, 모르는 api는 hub. 주소만 바꾸면 헤더는 hub 그대로
        NaverSettings odd = new NaverSettings("save-everything", "nope", "https://proxy.example.com/naver", 10);
        assertThat(odd.mode()).isEqualTo(NaverSettings.Mode.DISPLAY);
        assertThat(odd.api()).isEqualTo("hub");
        assertThat(odd.url()).isEqualTo("https://proxy.example.com/naver");
        assertThat(new NaverSettings("off", "hub", "", 10).enabled()).isFalse();
    }

    @Test
    void readsBothErrorBodyShapesAndExplainsCodes() throws Exception {
        assertThat(NaverSearchClient.errorOf(json.readTree("{\"errorCode\":\"SE03\",\"errorMessage\":\"Invalid start value\"}")))
                .containsExactly("SE03", "Invalid start value");
        assertThat(NaverSearchClient.errorOf(json.readTree(
                "{\"error\":{\"errorCode\":\"200\",\"message\":\"Authentication Failed\",\"details\":\"missing\"}}")))
                .containsExactly("200", "Authentication Failed: missing");
        assertThat(NaverSearchClient.errorOf(json.readTree("{\"items\":[]}"))).containsExactly("", "");
        assertThat(NaverSearchClient.errorOf(null)).containsExactly("", "");

        assertThat(NaverSearchClient.describe(200, "", "")).isNull();
        NaverSearchException auth = NaverSearchClient.describe(401, "200", "Authentication Failed");
        assertThat(auth.kind()).isEqualTo(Kind.KEY_INVALID);
        assertThat(auth.getMessage()).contains("Client ID").contains("'검색'").contains("200: Authentication Failed");
        assertThat(NaverSearchClient.describe(403, "", "").kind()).isEqualTo(Kind.KEY_INVALID);
        NaverSearchException limit = NaverSearchClient.describe(429, "", "");
        assertThat(limit.kind()).isEqualTo(Kind.RATE_LIMITED);
        assertThat(limit.getMessage()).contains("하루 허용량").contains("HTTP 429");
        assertThat(NaverSearchClient.describe(400, "SE01", "x").getMessage()).contains("query");
        assertThat(NaverSearchClient.describe(400, "SE02", "x").getMessage()).contains("display 값이 허용 범위");
        assertThat(NaverSearchClient.describe(400, "SE03", "x").getMessage()).contains("start 값");
        assertThat(NaverSearchClient.describe(400, "SE04", "x").getMessage()).contains("sort");
        assertThat(NaverSearchClient.describe(400, "SE06", "x").getMessage()).contains("UTF-8");
        NaverSearchException missing = NaverSearchClient.describe(404, "SE05", "Invalid search api");
        assertThat(missing.kind()).isEqualTo(Kind.BAD_REQUEST);
        assertThat(missing.getMessage()).contains("SENSING_NAVER_API");
        assertThat(NaverSearchClient.describe(300, "300", "API 없음").kind()).isEqualTo(Kind.BAD_REQUEST);
        NaverSearchException server = NaverSearchClient.describe(500, "SE99", "System Error");
        assertThat(server.kind()).isEqualTo(Kind.SERVER);
        assertThat(server.getMessage()).contains("네이버 검색 서버 내부 오류");
    }

    @Test
    void cleansTitlesAndParsesPubDates() {
        assertThat(NaverSearchClient.cleanTitle("  <b>삼성</b>&amp;<b>LG</b> &lt;협력&gt; &quot;신제품&quot;  "))
                .isEqualTo("삼성&LG <협력> \"신제품\"");
        assertThat(NaverSearchClient.cleanTitle("")).isNull();
        assertThat(NaverSearchClient.cleanTitle(null)).isNull();
        OffsetDateTime expected = OffsetDateTime.of(2026, 10, 16, 0, 30, 0, 0, ZoneOffset.UTC);
        assertThat(NaverSearchClient.parsePubDate("Fri, 16 Oct 2026 09:30:00 +0900")).isEqualTo(expected);
        assertThat(NaverSearchClient.parsePubDate("Thu, 16 Oct 2026 09:30:00 +0900")).isEqualTo(expected);   // 요일이 틀려도
        assertThat(NaverSearchClient.parsePubDate("not a date")).isNull();
        assertThat(NaverSearchClient.parsePubDate(null)).isNull();
    }

    @Test
    void liveViewHelpersPickWatchKeywordsAndPressHosts() {
        assertThat(NaverNewsService.pickKeyword(java.util.List.of("메타넷", "Metanet"), null)).isEqualTo("메타넷");
        assertThat(NaverNewsService.pickKeyword(java.util.List.of("메타넷", "Metanet"), " metanet ")).isEqualTo("Metanet");
        assertThat(NaverNewsService.host("https://WWW.Example.co.kr/a?b=1")).isEqualTo("example.co.kr");
        assertThat(NaverNewsService.host("not a url")).isNull();
    }
}
