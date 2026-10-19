package io.sensinghub.naver;

import java.io.IOException;
import java.net.ProxySelector;
import java.net.URI;
import java.net.URLEncoder;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.net.http.HttpTimeoutException;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.time.format.DateTimeFormatter;
import java.time.format.DateTimeParseException;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.regex.Pattern;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;
import org.springframework.web.util.HtmlUtils;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;

/**
 * 네이버 뉴스 검색 API 한 번 호출 (NAVER API HUB 기본, {@link NaverSettings}). 결과는 호출한 쪽에 돌려줄 뿐 저장·캐싱하지 않는다.
 * <ul>
 *   <li>GET {url}?query=..&display=..&start=..&sort=date|sim(&format=json). 키는 헤더로만 보낸다 (HUB: X-NCP-APIGW-API-KEY-ID/KEY).</li>
 *   <li>제목·요약(description)은 &lt;b&gt; 태그를 지우고 HTML 엔티티를 풀어 돌려준다. 요약은 표시 전용 검색 화면에서만 쓴다.</li>
 *   <li>오류 본문은 두 형식이다: 검색 API {@code {"errorCode":"SE02","errorMessage":".."}},
 *       API Gateway {@code {"error":{"errorCode":"200","message":"Authentication Failed","details":".."}}}.</li>
 *   <li>401·403 → 키 문제({@link Kind#KEY_INVALID}), 429 → 하루 허용량 초과, 400 SE01~SE04·SE06 / 404 SE05 / 300 → 요청·주소 오류,
 *       5xx(SE99) → 서버 오류. 문장은 워커 apis/naver_news.describe_error와 같은 뜻이다.</li>
 *   <li>Client ID·Secret은 예외 메시지·로그에 남기지 않는다 (응답 본문에 섞여 와도 가린다).</li>
 * </ul>
 */
@Component
public class NaverSearchClient {

    private static final Logger log = LoggerFactory.getLogger(NaverSearchClient.class);
    private static final Pattern TAG = Pattern.compile("<[^>]+>");
    private static final Pattern WS = Pattern.compile("\\s+");
    private static final DateTimeFormatter RFC_1123 = DateTimeFormatter.RFC_1123_DATE_TIME;
    private static final DateTimeFormatter NO_WEEKDAY = DateTimeFormatter.ofPattern("d MMM yyyy HH:mm[:ss] xx", Locale.ENGLISH);

    /** 검색 API 오류 코드 (NAVER API HUB 뉴스 검색 문서, 2026-10-19 확인). */
    static final Map<String, String> SE_MESSAGES = Map.of(
            "SE01", "검색 요청 형식이 잘못됐습니다 (주소의 https·필수 변수 query 확인)",
            "SE02", "display 값이 허용 범위(1~100)를 벗어났습니다",
            "SE03", "start 값이 허용 범위(1~1000)를 벗어났습니다",
            "SE04", "sort 값이 잘못됐습니다 (sim 또는 date)",
            "SE05", "존재하지 않는 검색 API 주소입니다 (SENSING_NAVER_API·SENSING_NAVER_SEARCH_URL 확인)",
            "SE06", "검색어 인코딩이 잘못됐습니다 (UTF-8)",
            "SE99", "네이버 검색 서버 내부 오류입니다");

    /** 요약 문장 최대 글자 수 (네이버 요약은 보통 150자 안팎이다). */
    static final int DESCRIPTION_MAX = 300;

    /**
     * @param description 요약 문장 (태그 제거·엔티티 해제, {@value #DESCRIPTION_MAX}자까지). 표시 전용 검색 화면만 쓴다
     * @param link        네이버 뉴스 주소 (네이버에 없는 기사는 원문 주소와 같다)
     */
    public record Item(String title, String description, String originalLink, String link, OffsetDateTime publishedAt) {
    }

    /** 정렬: date 최신순, sim 정확도순. */
    public enum Sort {
        DATE, SIM;

        public String param() {
            return name().toLowerCase(Locale.ROOT);
        }
    }

    public record Page(long total, int start, int display, List<Item> items) {
    }

    public enum Kind { KEY_INVALID, RATE_LIMITED, BAD_REQUEST, SERVER, NETWORK }

    public static final class NaverSearchException extends RuntimeException {
        private final Kind kind;
        private final int httpStatus;

        NaverSearchException(Kind kind, int httpStatus, String message) {
            super(message);
            this.kind = kind;
            this.httpStatus = httpStatus;
        }

        public Kind kind() {
            return kind;
        }

        public int httpStatus() {
            return httpStatus;
        }
    }

    private final NaverSettings settings;
    private final ObjectMapper json;
    private final HttpClient client;

    public NaverSearchClient(NaverSettings settings, ObjectMapper json) {
        this.settings = settings;
        this.json = json;
        this.client = HttpClient.newBuilder()
                .version(HttpClient.Version.HTTP_1_1)
                .connectTimeout(Duration.ofSeconds(settings.timeoutSeconds()))
                .followRedirects(HttpClient.Redirect.NEVER)
                .proxy(ProxySelector.getDefault())
                .build();
    }

    /** 날짜순(sort=date) 검색 한 쪽. 키가 틀리거나 한도를 넘으면 {@link NaverSearchException}. */
    public Page search(String clientId, String clientSecret, String query, int display, int start) {
        return search(clientId, clientSecret, query, display, start, Sort.DATE);
    }

    /** 검색 한 쪽 (정렬 지정). 키가 틀리거나 한도를 넘으면 {@link NaverSearchException}. */
    public Page search(String clientId, String clientSecret, String query, int display, int start, Sort sort) {
        Map<String, String> params = new LinkedHashMap<>();
        params.put("query", query);
        params.put("display", String.valueOf(display));
        params.put("start", String.valueOf(start));
        params.put("sort", sort.param());
        settings.extraParams().forEach(e -> params.put(e.getKey(), e.getValue()));
        StringBuilder url = new StringBuilder(settings.url()).append('?');
        params.forEach((k, v) -> url.append(k).append('=').append(URLEncoder.encode(v, StandardCharsets.UTF_8)).append('&'));
        url.setLength(url.length() - 1);

        HttpRequest.Builder request = HttpRequest.newBuilder(URI.create(url.toString()))
                .timeout(Duration.ofSeconds(settings.timeoutSeconds()))
                .header("Accept", "application/json")
                .GET();
        settings.authHeaders(clientId, clientSecret).forEach(request::header);
        HttpResponse<String> response;
        try {
            response = client.send(request.build(), HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
        } catch (HttpTimeoutException e) {
            throw new NaverSearchException(Kind.NETWORK, 0,
                    "네이버 검색 API가 " + settings.timeoutSeconds() + "초 안에 응답하지 않았습니다. 잠시 후 다시 시도해 주세요.");
        } catch (IOException e) {
            log.warn("naver search ({}) connection failed: {}", settings.api(), e.toString());
            throw new NaverSearchException(Kind.NETWORK, 0, "네이버 검색 API에 연결하지 못했습니다. 잠시 후 다시 시도해 주세요.");
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new NaverSearchException(Kind.NETWORK, 0, "네이버 검색 API 호출이 중단되었습니다.");
        }

        JsonNode body = parse(response.body());
        String[] error = errorOf(body);
        NaverSearchException problem = describe(response.statusCode(), error[0], mask(error[1], clientId, clientSecret));
        if (problem != null) {
            log.warn("naver search ({}) failed: HTTP {} {}", settings.api(), response.statusCode(), error[0]);
            throw problem;
        }
        if (body == null || !body.has("items")) {
            throw new NaverSearchException(Kind.SERVER, response.statusCode(), "네이버 검색 API 응답이 예상한 JSON이 아닙니다.");
        }
        List<Item> items = new ArrayList<>();
        for (JsonNode row : body.path("items")) {
            items.add(new Item(cleanTitle(row.path("title").asText(null)), cleanDescription(row.path("description").asText(null)),
                    blankToNull(row.path("originallink").asText(null)), blankToNull(row.path("link").asText(null)),
                    parsePubDate(row.path("pubDate").asText(null))));
        }
        return new Page(body.path("total").asLong(0), body.path("start").asInt(start), body.path("display").asInt(items.size()),
                items);
    }

    // ------------------------------------------------------------------

    private JsonNode parse(String text) {
        if (text == null || text.isBlank()) {
            return null;
        }
        try {
            return json.readTree(text);
        } catch (IOException e) {
            return null;
        }
    }

    /** 오류 본문 → {코드, 메시지}. 검색 API 형식과 API Gateway 형식을 모두 읽는다. */
    static String[] errorOf(JsonNode body) {
        if (body == null || !body.isObject()) {
            return new String[] {"", ""};
        }
        if (body.hasNonNull("errorCode") || body.hasNonNull("errorMessage")) {
            return new String[] {body.path("errorCode").asText("").strip(), body.path("errorMessage").asText("").strip()};
        }
        JsonNode gateway = body.path("error");
        if (gateway.isObject()) {
            String message = gateway.path("message").asText("").strip();
            String details = gateway.path("details").asText("").strip();
            String text = !message.isEmpty() && !details.isEmpty() ? message + ": " + details : message.isEmpty() ? details : message;
            return new String[] {gateway.path("errorCode").asText("").strip(), text};
        }
        return new String[] {"", ""};
    }

    /** HTTP 상태·오류 코드 → 알기 쉬운 예외. 정상(200, 코드 없음)이면 null. */
    static NaverSearchException describe(int status, String code, String message) {
        String detail = !code.isEmpty() && !message.isEmpty() ? code + ": " + message
                : !code.isEmpty() ? code : !message.isEmpty() ? message : "HTTP " + status;
        String friendly = SE_MESSAGES.get(code);
        if (status == 401) {
            return new NaverSearchException(Kind.KEY_INVALID, status, "네이버 검색 인증 실패 — Client ID·Client Secret이 맞는지, "
                    + "API HUB Application에 '검색'이 선택돼 있는지 확인하세요. (" + detail + ")");
        }
        if (status == 403) {
            return new NaverSearchException(Kind.KEY_INVALID, status, "네이버 검색 호출이 허용되지 않았습니다 — Application의 검색 API "
                    + "권한과 https 주소를 확인하세요. (" + detail + ")");
        }
        if (status == 429) {
            return new NaverSearchException(Kind.RATE_LIMITED, status,
                    "네이버 검색 API 호출 한도(하루 허용량)를 넘었습니다. 내일 다시 시도해 주세요. (" + detail + ")");
        }
        if (status == 300 || status == 404) {
            return new NaverSearchException(Kind.BAD_REQUEST, status, "네이버 검색 API 주소가 잘못됐습니다 — "
                    + (friendly != null ? friendly : "API 없음") + " (" + detail + ")");
        }
        if (status >= 500) {
            return new NaverSearchException(Kind.SERVER, status, "네이버 검색 API 서버 오류 — "
                    + (friendly != null ? friendly : "잠시 후 다시 시도해 주세요") + " (" + detail + ")");
        }
        if (status != 200) {
            return new NaverSearchException(Kind.BAD_REQUEST, status, "네이버 검색 API 요청 오류 — "
                    + (friendly != null ? friendly : "잘못된 요청") + " (" + detail + ")");
        }
        if (!code.isEmpty()) {
            return new NaverSearchException(Kind.SERVER, status, "네이버 검색 API 오류 — "
                    + (friendly != null ? friendly : message.isEmpty() ? code : message) + " (" + detail + ")");
        }
        return null;
    }

    /** &lt;b&gt;태그 제거, &amp;quot; 같은 엔티티 복원, 공백 정리. */
    static String cleanTitle(String raw) {
        if (raw == null || raw.isBlank()) {
            return null;
        }
        String text = WS.matcher(HtmlUtils.htmlUnescape(TAG.matcher(raw).replaceAll(""))).replaceAll(" ").strip();
        return text.isEmpty() ? null : text;
    }

    /** 요약 문장: 제목과 같게 정리하고 {@value #DESCRIPTION_MAX}자를 넘으면 자르고 "…"를 붙인다. */
    static String cleanDescription(String raw) {
        String text = cleanTitle(raw);
        if (text == null || text.length() <= DESCRIPTION_MAX) {
            return text;
        }
        int end = Character.isHighSurrogate(text.charAt(DESCRIPTION_MAX - 1)) ? DESCRIPTION_MAX - 1 : DESCRIPTION_MAX;
        return text.substring(0, end).stripTrailing() + "…";
    }

    /**
     * RFC 1123 ("Fri, 16 Oct 2026 09:30:00 +0900") → UTC. 읽지 못하면 null. 요일이 날짜와 맞지 않아도 버리지 않는다
     * (Java RFC 1123 파서는 요일까지 검사하지만, 워커의 email.utils처럼 요일은 보지 않는다).
     */
    static OffsetDateTime parsePubDate(String text) {
        if (text == null || text.isBlank()) {
            return null;
        }
        String t = text.strip();
        try {
            return OffsetDateTime.parse(t, RFC_1123).withOffsetSameInstant(ZoneOffset.UTC);
        } catch (DateTimeParseException e) {
            int comma = t.indexOf(',');
            try {
                return OffsetDateTime.parse(comma >= 0 ? t.substring(comma + 1).strip() : t, NO_WEEKDAY)
                        .withOffsetSameInstant(ZoneOffset.UTC);
            } catch (DateTimeParseException e2) {
                return null;
            }
        }
    }

    private static String blankToNull(String value) {
        return value == null || value.isBlank() ? null : value.strip();
    }

    private static String mask(String text, String clientId, String clientSecret) {
        String out = text == null ? "" : text;
        if (clientSecret != null && !clientSecret.isEmpty()) {
            out = out.replace(clientSecret, "***");
        }
        if (clientId != null && !clientId.isEmpty()) {
            out = out.replace(clientId, "***");
        }
        return out;
    }
}
