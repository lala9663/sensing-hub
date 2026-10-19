package io.sensinghub.naver;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.Map;

import org.junit.jupiter.api.Test;

import com.fasterxml.jackson.databind.ObjectMapper;

import io.sensinghub.common.ApiException;
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
    void cleansDescriptionsAndCutsLongOnes() {
        assertThat(NaverSearchClient.cleanDescription("<b>메타넷</b>이 &quot;신제품&quot;을\n  공개 &amp; &lt;단독&gt;&#39;"))
                .isEqualTo("메타넷이 \"신제품\"을 공개 & <단독>'");
        assertThat(NaverSearchClient.cleanDescription("  ")).isNull();
        assertThat(NaverSearchClient.cleanDescription(null)).isNull();
        String exact = "가".repeat(NaverSearchClient.DESCRIPTION_MAX);
        assertThat(NaverSearchClient.cleanDescription(exact)).isEqualTo(exact);
        String cut = NaverSearchClient.cleanDescription("<b>" + "나".repeat(400) + "</b>");
        assertThat(cut).hasSize(NaverSearchClient.DESCRIPTION_MAX + 1).endsWith("…").doesNotContain("<b>");
        assertThat(NaverSearchClient.Sort.DATE.param()).isEqualTo("date");
        assertThat(NaverSearchClient.Sort.SIM.param()).isEqualTo("sim");
    }

    @Test
    void searchHelpersParseSortAndFindParentDomains() {
        assertThat(NaverNewsService.parseSort(null)).isEqualTo(NaverSearchClient.Sort.DATE);
        assertThat(NaverNewsService.parseSort(" ")).isEqualTo(NaverSearchClient.Sort.DATE);
        assertThat(NaverNewsService.parseSort("Sim")).isEqualTo(NaverSearchClient.Sort.SIM);
        assertThatThrownBy(() -> NaverNewsService.parseSort("popular")).isInstanceOf(ApiException.class)
                .hasMessageContaining("sim");
        assertThat(NaverNewsService.parentDomain("news.example.co.kr")).isEqualTo("example.co.kr");
        assertThat(NaverNewsService.parentDomain("example.com")).isNull();
        assertThat(NaverNewsService.parentDomain(null)).isNull();
    }

    @Test
    void userRateLimiterAllowsTwoPerSecondPerUser() {
        long[] now = {0};
        UserRateLimiter limiter = new UserRateLimiter(2, 1000, () -> now[0]);
        assertThat(limiter.tryAcquire(1)).isTrue();
        assertThat(limiter.tryAcquire(1)).isTrue();
        assertThat(limiter.tryAcquire(1)).isFalse();                 // 1초 안에 세 번째
        assertThat(limiter.tryAcquire(2)).isTrue();                  // 다른 사용자는 따로 센다
        now[0] = 999_000_000L;
        assertThat(limiter.tryAcquire(1)).isFalse();                 // 거절한 호출은 세지 않지만 아직 1초가 안 됐다
        now[0] = 1_000_000_000L;
        assertThat(limiter.tryAcquire(1)).isTrue();                  // 첫 두 호출이 창 밖으로 나갔다
        assertThat(limiter.tryAcquire(1)).isTrue();
        assertThat(limiter.tryAcquire(1)).isFalse();
        assertThat(limiter.limit()).isEqualTo(2);
    }

    @Test
    void liveViewHelpersPickWatchKeywordsAndPressHosts() {
        assertThat(NaverNewsService.pickKeyword(java.util.List.of("메타넷", "Metanet"), null)).isEqualTo("메타넷");
        assertThat(NaverNewsService.pickKeyword(java.util.List.of("메타넷", "Metanet"), " metanet ")).isEqualTo("Metanet");
        assertThat(NaverNewsService.host("https://WWW.Example.co.kr/a?b=1")).isEqualTo("example.co.kr");
        assertThat(NaverNewsService.host("not a url")).isNull();
    }
}
