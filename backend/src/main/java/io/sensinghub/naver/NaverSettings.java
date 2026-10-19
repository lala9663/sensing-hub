package io.sensinghub.naver;

import java.util.List;
import java.util.Locale;
import java.util.Map;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

/**
 * 네이버 뉴스 검색 사용 방식과 주소 ({@code sensing.naver.*}). 워커 Settings.naver_mode·naver_api·naver_search_url과 같은 환경 변수를 읽는다.
 * <ul>
 *   <li>mode (SENSING_NAVER_MODE): {@code display}(기본, 표시 전용) | {@code store}(저장·분석, 법무 확인 뒤) | {@code off}.
 *       2026-09-07 시행 개정 약관이 검색 결과의 저장·캐싱·AI 입력·제3자 제공을 금지해, 기본은 결과를 DB에 넣지 않고 감시 화면에서
 *       실시간으로 불러와 보여 주기만 한다({@link NaverNewsService}). store일 때만 키워드 감시에 저장 수집을 붙인다.</li>
 *   <li>api (SENSING_NAVER_API): {@code hub}(기본, NAVER API HUB) | {@code developers}(예전 개발자센터, 2027-06-30 지원 종료).
 *       주소와 인증 헤더 이름이 달라진다.</li>
 *   <li>search-url (SENSING_NAVER_SEARCH_URL): 주소만 바꾼다 (테스트·프록시). 헤더는 api를 따른다.</li>
 * </ul>
 */
@Component
public class NaverSettings {

    private static final Logger log = LoggerFactory.getLogger(NaverSettings.class);

    public enum Mode { DISPLAY, STORE, OFF }

    public static final String HUB_URL = "https://naverapihub.apigw.ntruss.com/search/v1/news";
    public static final String DEVELOPERS_URL = "https://openapi.naver.com/v1/search/news.json";
    /** 화면에 붙이는 출처 표기. */
    public static final String SOURCE_LABEL = "NAVER 검색 결과";

    private final Mode mode;
    private final String api;
    private final String url;
    private final int timeoutSeconds;

    public NaverSettings(@Value("${sensing.naver.mode:display}") String mode,
                         @Value("${sensing.naver.api:hub}") String api,
                         @Value("${sensing.naver.search-url:}") String searchUrl,
                         @Value("${sensing.naver.timeout-seconds:10}") int timeoutSeconds) {
        this.mode = parseMode(mode);
        String a = api == null ? "" : api.strip().toLowerCase(Locale.ROOT);
        if (!a.equals("hub") && !a.equals("developers")) {
            log.warn("sensing.naver.api={} is not hub|developers; using hub", api);
            a = "hub";
        }
        this.api = a;
        String override = searchUrl == null ? "" : searchUrl.strip();
        this.url = !override.isEmpty() ? override : a.equals("hub") ? HUB_URL : DEVELOPERS_URL;
        this.timeoutSeconds = Math.max(1, timeoutSeconds);
    }

    /** 모르는 값은 가장 안전한 표시 전용으로 본다 (저장으로 잘못 켜지지 않게). */
    static Mode parseMode(String value) {
        String v = value == null ? "" : value.strip().toLowerCase(Locale.ROOT);
        return switch (v) {
            case "store" -> Mode.STORE;
            case "off" -> Mode.OFF;
            case "display", "" -> Mode.DISPLAY;
            default -> {
                log.warn("sensing.naver.mode={} is not display|store|off; using display", value);
                yield Mode.DISPLAY;
            }
        };
    }

    public Mode mode() {
        return mode;
    }

    /** 키워드 감시에 네이버 저장 수집을 붙여도 되는가 (store). */
    public boolean storeEnabled() {
        return mode == Mode.STORE;
    }

    /** 네이버를 부르는가 (display·store). */
    public boolean enabled() {
        return mode != Mode.OFF;
    }

    public String api() {
        return api;
    }

    public String url() {
        return url;
    }

    public int timeoutSeconds() {
        return timeoutSeconds;
    }

    /** 인증 헤더 (hub: X-NCP-APIGW-API-KEY-ID/KEY, developers: X-Naver-Client-Id/Secret). */
    public Map<String, String> authHeaders(String clientId, String clientSecret) {
        return api.equals("hub")
                ? Map.of("X-NCP-APIGW-API-KEY-ID", clientId, "X-NCP-APIGW-API-KEY", clientSecret)
                : Map.of("X-Naver-Client-Id", clientId, "X-Naver-Client-Secret", clientSecret);
    }

    /** api별 추가 파라미터 (hub는 format=json, developers는 주소의 .json이 형식이다). */
    public List<Map.Entry<String, String>> extraParams() {
        return api.equals("hub") ? List.of(Map.entry("format", "json")) : List.of();
    }
}
