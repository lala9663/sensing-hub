package io.sensinghub.naver;

import java.net.URI;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.regex.Pattern;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.dao.DataAccessException;
import org.springframework.http.HttpStatus;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Service;

import com.fasterxml.jackson.databind.ObjectMapper;

import io.sensinghub.audit.AuditLogger;
import io.sensinghub.auth.CurrentUser;
import io.sensinghub.auth.Role;
import io.sensinghub.common.ApiException;
import io.sensinghub.naver.NaverSearchClient.Kind;
import io.sensinghub.naver.NaverSearchClient.NaverSearchException;
import io.sensinghub.naver.NaverSearchClient.Sort;
import io.sensinghub.watch.WatchService;
import io.sensinghub.watch.WatchService.WatchView;

/**
 * 네이버 뉴스 표시 전용 조회 (기본 모드). 두 가지 화면이 쓴다.
 * <ul>
 *   <li>감시 화면 "실시간" 보기({@link #live}): 감시 키워드 하나로 네이버 최신 뉴스를 불러와 제목·언론사·시각·원문/네이버 링크만 돌려준다
 *       (요약 없음).</li>
 *   <li>"네이버 뉴스" 탭({@link #search}): 감시와 무관한 검색어로 검색해 위 항목에 요약 문장(description)까지 돌려준다. VIEWER 이상,
 *       사용자별 짧은 간격 제한({@code sensing.naver.search-per-second}, 기본 초당 2회)과 display 상한({@value #MAX_DISPLAY})을 둔다.
 *       검색어·정렬·쪽은 감사 로그(NAVER_SEARCH)에 남기고 결과는 남기지 않는다.</li>
 * </ul>
 * <ul>
 *   <li>2026-09-07 시행 NAVER API HUB 개정 약관(검색 결과의 저장·캐싱·AI 입력·제3자 제공 금지)에 맞춰 결과는 <b>어디에도 남기지 않는다</b>:
 *       document·document_watch·임베딩·다이제스트·챗봇·LLM 어느 쪽에도 넣지 않고, 메모리 캐시도 두지 않는다(약관 확인 전까지).
 *       응답에는 {@code Cache-Control: no-store}를 붙인다(컨트롤러). DB에 쓰는 것은 키 상태(api_credential.status)와 탭 검색의
 *       감사 로그뿐이다. 화면에는 출처 "NAVER 검색 결과"를 붙인다.</li>
 *   <li>키는 api_credential(provider naver_news, "Client ID:Client Secret")을 pgcrypto로 풀어 쓰고 응답·로그에 내지 않는다.</li>
 *   <li>모드가 off거나 키가 없으면(또는 INVALID면) 감시 화면은 이 보기를 숨기고 탭은 안내만 보여 준다({@link #status}).</li>
 * </ul>
 */
@Service
public class NaverNewsService {

    private static final Logger log = LoggerFactory.getLogger(NaverNewsService.class);
    public static final String PROVIDER = WatchService.PROVIDER;
    static final int DEFAULT_DISPLAY = 20;
    static final int MAX_DISPLAY = 50;
    static final int MAX_START = 1000;
    static final int MAX_QUERY_LENGTH = 100;
    private static final Pattern WS = Pattern.compile("\\s+");

    /**
     * @param liveAvailable 감시 화면 "네이버 뉴스 실시간"과 네이버 뉴스 탭 검색을 쓸 수 있는지 (모드가 off가 아니고 쓸 수 있는 키가 있다)
     * @param keyStatus     UNVERIFIED | VALID | INVALID, 키가 없으면 null
     */
    public record Status(String mode, String api, boolean keyRegistered, String keyStatus, boolean liveAvailable,
                         String sourceLabel, String notice) {
    }

    /** @param naverUrl 네이버 뉴스 주소 (네이버에 없는 기사면 null) */
    public record LiveItem(String title, String press, OffsetDateTime publishedAt, String originalUrl, String naverUrl) {
    }

    /**
     * @param excluded 감시 제외어가 제목에 있어 뺀 건수
     * @param nextStart 더 볼 수 있으면 다음 start, 아니면 null
     */
    public record LiveResult(long watchId, String watchName, String keyword, List<String> keywords, long total, int start,
                             int display, List<LiveItem> items, int excluded, Integer nextStart, OffsetDateTime fetchedAt,
                             String sourceLabel, String mode) {
    }

    /**
     * @param description 요약 문장 (태그 제거·엔티티 해제), 없으면 null
     * @param naverUrl    네이버 뉴스 주소 (네이버에 없는 기사면 null)
     */
    public record SearchItem(String title, String description, String press, OffsetDateTime publishedAt, String originalUrl,
                             String naverUrl) {
    }

    /**
     * @param sort      date(최신순) | sim(정확도순)
     * @param nextStart 더 볼 수 있으면 다음 start, 아니면 null (네이버는 start {@value #MAX_START}까지)
     */
    public record SearchResult(String query, String sort, long total, int start, int display, List<SearchItem> items,
                               Integer nextStart, int maxStart, OffsetDateTime fetchedAt, String sourceLabel, String mode) {
    }

    public record VerifyResult(String provider, String status, String message, OffsetDateTime lastVerifiedAt) {
    }

    private record Credential(String clientId, String clientSecret, String status, String lastError) {
    }

    private final JdbcClient jdbc;
    private final NaverSettings settings;
    private final NaverSearchClient client;
    private final WatchService watches;
    private final AuditLogger audit;
    private final ObjectMapper json;
    private final UserRateLimiter limiter;
    private final String secretKey;

    public NaverNewsService(JdbcClient jdbc, NaverSettings settings, NaverSearchClient client, WatchService watches,
                            AuditLogger audit, ObjectMapper json,
                            @Value("${sensing.naver.search-per-second:2}") int searchPerSecond,
                            @Value("${sensing.secret-key:}") String secretKey) {
        this.jdbc = jdbc;
        this.settings = settings;
        this.client = client;
        this.watches = watches;
        this.audit = audit;
        this.json = json;
        this.limiter = new UserRateLimiter(searchPerSecond, 1000);
        this.secretKey = secretKey == null ? "" : secretKey;
    }

    public Status status(CurrentUser user) {
        String keyStatus = jdbc.sql("SELECT status FROM api_credential WHERE provider = ?").param(PROVIDER)
                .query(String.class).optional().orElse(null);
        boolean live = settings.enabled() && keyStatus != null && !"INVALID".equals(keyStatus) && !secretKey.isBlank();
        String notice = switch (settings.mode()) {
            case OFF -> "네이버 뉴스 검색을 쓰지 않습니다 (SENSING_NAVER_MODE=off).";
            case STORE -> "네이버 검색 결과를 저장·분석합니다 (SENSING_NAVER_MODE=store, 법무 확인 뒤에만 켭니다).";
            case DISPLAY -> "네이버 검색 결과는 표시 전용입니다: 화면에 보여 주기만 하고 저장·요약·챗봇·다이제스트에 쓰지 않습니다.";
        };
        return new Status(settings.mode().name().toLowerCase(Locale.ROOT), settings.api(), keyStatus != null, keyStatus, live,
                NaverSettings.SOURCE_LABEL, notice);
    }

    /** 감시 키워드 하나로 네이버 최신 뉴스(날짜순) 한 쪽. 감시를 볼 수 있는 사람만(내 감시, ADMIN은 전체). */
    public LiveResult live(CurrentUser user, long watchId, String keyword, Integer display, Integer start) {
        WatchView watch = watches.get(user, watchId);
        if (!settings.enabled()) {
            throw new ApiException(HttpStatus.SERVICE_UNAVAILABLE, "NAVER_OFF", "네이버 뉴스 검색이 꺼져 있습니다 (SENSING_NAVER_MODE=off).");
        }
        String term = pickKeyword(watch.keywords(), keyword);
        int size = clamp(display, DEFAULT_DISPLAY, 1, MAX_DISPLAY);
        int from = clamp(start, 1, 1, MAX_START);
        Credential key = credential();

        NaverSearchClient.Page page = call(key, term, size, from, Sort.DATE);
        markValidIfNeeded();

        List<String> exclude = watch.exclude().stream().map(e -> e.toLowerCase(Locale.ROOT)).toList();
        Map<String, String> pressNames = pressNames(page.items());
        List<LiveItem> items = new ArrayList<>();
        int excluded = 0;
        for (NaverSearchClient.Item it : page.items()) {
            String original = it.originalLink() != null ? it.originalLink() : it.link();
            if (original == null) {
                continue;
            }
            String lowered = it.title() == null ? "" : it.title().toLowerCase(Locale.ROOT);
            if (exclude.stream().anyMatch(lowered::contains)) {
                excluded++;
                continue;
            }
            items.add(new LiveItem(it.title(), press(original, pressNames), it.publishedAt(), original, naverUrl(it)));
        }
        Integer next = nextStart(page, from, size);
        log.info("naver live search watch={} start={} display={} -> {} items (api {})", watchId, from, size, items.size(),
                settings.api());
        return new LiveResult(watch.id(), watch.name(), term, watch.keywords(), page.total(), from, size, items, excluded, next,
                OffsetDateTime.now(), NaverSettings.SOURCE_LABEL, settings.mode().name().toLowerCase(Locale.ROOT));
    }

    /**
     * 네이버 뉴스 탭: 감시와 무관한 검색어로 네이버 뉴스 한 쪽 (표시 전용). VIEWER 이상.
     * 결과는 돌려주기만 하고 남기지 않는다. 감사 로그에는 검색어·정렬·쪽만 남긴다 (결과·건수는 남기지 않는다).
     */
    public SearchResult search(CurrentUser user, String query, Integer display, Integer start, String sort) {
        user.require(Role.VIEWER);
        if (!settings.enabled()) {
            throw new ApiException(HttpStatus.SERVICE_UNAVAILABLE, "NAVER_OFF", "네이버 뉴스 검색이 꺼져 있습니다 (SENSING_NAVER_MODE=off).");
        }
        String term = query == null ? "" : WS.matcher(query).replaceAll(" ").strip();
        if (term.isEmpty()) {
            throw new ApiException(HttpStatus.BAD_REQUEST, "BAD_REQUEST", "검색어를 입력해 주세요.");
        }
        if (term.length() > MAX_QUERY_LENGTH) {
            throw new ApiException(HttpStatus.BAD_REQUEST, "BAD_REQUEST", "검색어는 " + MAX_QUERY_LENGTH + "자까지 입력할 수 있습니다.");
        }
        Sort order = parseSort(sort);
        int size = clamp(display, DEFAULT_DISPLAY, 1, MAX_DISPLAY);
        int from = clamp(start, 1, 1, MAX_START);
        if (!limiter.tryAcquire(user.id())) {
            throw new ApiException(HttpStatus.TOO_MANY_REQUESTS, "NAVER_TOO_FAST",
                    "검색을 너무 빠르게 요청했습니다 (사용자당 초당 " + limiter.limit() + "회까지). 잠시 후 다시 검색해 주세요.");
        }
        Credential key = credential();
        audit.log(user.id(), "NAVER_SEARCH", "naver_news", null, json.createObjectNode()
                .put("query", term).put("sort", order.param()).put("start", from).put("display", size).toString());

        NaverSearchClient.Page page = call(key, term, size, from, order);
        markValidIfNeeded();

        Map<String, String> pressNames = pressNames(page.items());
        List<SearchItem> items = new ArrayList<>();
        for (NaverSearchClient.Item it : page.items()) {
            String original = it.originalLink() != null ? it.originalLink() : it.link();
            if (original == null) {
                continue;
            }
            items.add(new SearchItem(it.title(), it.description(), press(original, pressNames), it.publishedAt(), original,
                    naverUrl(it)));
        }
        Integer next = nextStart(page, from, size);
        log.info("naver news search user={} sort={} start={} display={} -> {} items (api {})", user.id(), order.param(), from,
                size, items.size(), settings.api());
        return new SearchResult(term, order.param(), page.total(), from, size, items, next, MAX_START, OffsetDateTime.now(),
                NaverSettings.SOURCE_LABEL, settings.mode().name().toLowerCase(Locale.ROOT));
    }

    /** 관리자: 키 시험 호출 (검색어 "뉴스", 1건). 결과를 api_credential 상태에 남긴다. */
    public VerifyResult verify(CurrentUser admin) {
        admin.require(Role.ADMIN);
        if (!settings.enabled()) {
            throw new ApiException(HttpStatus.SERVICE_UNAVAILABLE, "NAVER_OFF", "네이버 뉴스 검색이 꺼져 있습니다 (SENSING_NAVER_MODE=off).");
        }
        Credential key = loadCredential();
        if (key == null) {
            throw ApiException.notFound("naver_news 자격 증명");
        }
        String message = tryVerify(key);
        return jdbc.sql("SELECT status, last_verified_at FROM api_credential WHERE provider = ?").param(PROVIDER)
                .query((rs, n) -> new VerifyResult(PROVIDER, rs.getString("status"), message,
                        rs.getObject("last_verified_at", OffsetDateTime.class)))
                .single();
    }

    /** 시험 호출 한 번. 성공·키 오류는 상태에 남기고, 한도·서버 오류는 상태를 바꾸지 않는다. 사람이 읽을 결과 문장을 돌려준다. */
    private String tryVerify(Credential key) {
        try {
            NaverSearchClient.Page page = client.search(key.clientId(), key.clientSecret(), "뉴스", 1, 1);
            mark("VALID", null);
            return "네이버 뉴스 검색 시험 호출 성공 (" + settings.api() + ", 검색 결과 " + page.total()
                    + "건). '네이버 뉴스' 탭과 키워드 감시 화면의 '네이버 실시간'에서 볼 수 있습니다.";
        } catch (NaverSearchException e) {
            if (e.kind() == Kind.KEY_INVALID) {
                mark("INVALID", e.getMessage());
            }
            return e.getMessage();
        }
    }

    // ------------------------------------------------------------------

    private NaverSearchClient.Page call(Credential key, String term, int size, int from, Sort sort) {
        try {
            return client.search(key.clientId(), key.clientSecret(), term, size, from, sort);
        } catch (NaverSearchException e) {
            if (e.kind() == Kind.KEY_INVALID) {
                mark("INVALID", e.getMessage());
                throw new ApiException(HttpStatus.BAD_GATEWAY, "NAVER_KEY_INVALID", e.getMessage());
            }
            if (e.kind() == Kind.RATE_LIMITED) {
                throw new ApiException(HttpStatus.TOO_MANY_REQUESTS, "NAVER_RATE_LIMITED", e.getMessage());
            }
            throw new ApiException(e.kind() == Kind.NETWORK ? HttpStatus.GATEWAY_TIMEOUT : HttpStatus.BAD_GATEWAY,
                    "NAVER_ERROR", e.getMessage());
        }
    }

    /** 실시간 보기·탭 검색에 쓸 키. 없거나 INVALID면 409 (화면은 status로 이 보기를 숨기거나 안내만 보여 준다). */
    private Credential credential() {
        Credential key = loadCredential();
        if (key == null) {
            throw new ApiException(HttpStatus.CONFLICT, "NAVER_KEY_REQUIRED",
                    "네이버 API 키가 없습니다. 관리자가 API 키 화면에서 naver_news(Client ID·Client Secret)를 등록하면 볼 수 있습니다.");
        }
        if ("INVALID".equals(key.status())) {
            throw new ApiException(HttpStatus.CONFLICT, "NAVER_KEY_INVALID", "네이버 API 키가 유효하지 않습니다"
                    + (key.lastError() != null ? ": " + key.lastError() : ".") + " 관리자가 키를 다시 등록하거나 시험 호출해 주세요.");
        }
        return key;
    }

    private Credential loadCredential() {
        if (secretKey.isBlank()) {
            throw new ApiException(HttpStatus.SERVICE_UNAVAILABLE, "NOT_CONFIGURED", "SENSING_SECRET_KEY가 설정되지 않았습니다.");
        }
        Map<String, String> row;
        try {
            row = jdbc.sql("""
                    SELECT pgp_sym_decrypt(secret_enc, ?) AS secret, status, last_error FROM api_credential WHERE provider = ?
                    """)
                    .params(secretKey, PROVIDER)
                    .query((rs, n) -> {
                        Map<String, String> m = new HashMap<>();
                        m.put("secret", rs.getString("secret"));
                        m.put("status", rs.getString("status"));
                        m.put("last_error", rs.getString("last_error"));
                        return m;
                    })
                    .optional().orElse(null);
        } catch (DataAccessException e) {
            // 대칭키가 저장할 때와 다르면 pgcrypto가 실패한다 (키 값은 메시지에 없다)
            log.warn("naver credential decrypt failed: {}", e.getClass().getSimpleName());
            throw new ApiException(HttpStatus.SERVICE_UNAVAILABLE, "NOT_CONFIGURED",
                    "저장된 네이버 키를 풀지 못했습니다. SENSING_SECRET_KEY가 키를 저장할 때와 같은지 확인하거나 키를 다시 등록해 주세요.");
        }
        if (row == null) {
            return null;
        }
        String secret = row.get("secret") == null ? "" : row.get("secret").strip();
        int colon = secret.indexOf(':');
        if (colon <= 0 || colon == secret.length() - 1) {
            mark("INVALID", "네이버 키는 Client ID:Client Secret 형식이어야 합니다.");
            throw new ApiException(HttpStatus.CONFLICT, "NAVER_KEY_INVALID",
                    "네이버 키는 Client ID:Client Secret 형식이어야 합니다. 관리자가 키를 다시 등록해 주세요.");
        }
        return new Credential(secret.substring(0, colon).strip(), secret.substring(colon + 1).strip(), row.get("status"),
                row.get("last_error"));
    }

    private void mark(String status, String error) {
        jdbc.sql("""
                UPDATE api_credential SET status = ?, last_error = ?, last_verified_at = now(), updated_at = now()
                WHERE provider = ?
                """)
                .params(status, error == null ? null : error.length() > 500 ? error.substring(0, 500) : error, PROVIDER)
                .update();
    }

    /** 실시간 조회가 성공했으면 키를 VALID로 (이미 VALID면 쓰지 않는다). */
    private void markValidIfNeeded() {
        jdbc.sql("""
                UPDATE api_credential SET status = 'VALID', last_error = NULL, last_verified_at = now(), updated_at = now()
                WHERE provider = ? AND status <> 'VALID'
                """).param(PROVIDER).update();
    }

    /**
     * 원문 주소의 호스트(와 한 단계 위 도메인, news.example.co.kr → example.co.kr) → 우리가 등록한 사이트 이름 (site.name).
     * 없으면 표에 없다 (호출한 쪽이 호스트를 쓴다).
     */
    private Map<String, String> pressNames(List<NaverSearchClient.Item> items) {
        Set<String> hosts = new LinkedHashSet<>();
        for (NaverSearchClient.Item it : items) {
            String host = host(it.originalLink() != null ? it.originalLink() : it.link());
            if (host != null) {
                hosts.add(host);
                String parent = parentDomain(host);
                if (parent != null) {
                    hosts.add(parent);
                }
            }
        }
        Map<String, String> names = new HashMap<>();
        if (hosts.isEmpty()) {
            return names;
        }
        jdbc.sql("SELECT domain, name FROM site WHERE domain IN (:hosts) AND name IS NOT NULL AND name <> ''")
                .param("hosts", List.copyOf(hosts))
                .query((rs, n) -> Map.entry(rs.getString("domain"), rs.getString("name")))
                .list()
                .forEach(e -> names.put(e.getKey(), e.getValue()));
        return names;
    }

    static String host(String url) {
        if (url == null) {
            return null;
        }
        try {
            String h = URI.create(url.strip()).getHost();
            if (h == null) {
                return null;
            }
            h = h.toLowerCase(Locale.ROOT);
            return h.startsWith("www.") ? h.substring(4) : h;
        } catch (IllegalArgumentException e) {
            return null;
        }
    }

    private static boolean isNaverHost(String host) {
        return host != null && (host.equals("naver.com") || host.endsWith(".naver.com"));
    }

    /** 언론사 표시: 네이버 주소면 "네이버 뉴스", 등록된 사이트(호스트 또는 한 단계 위 도메인)면 그 이름, 아니면 호스트. */
    private static String press(String originalUrl, Map<String, String> pressNames) {
        String host = host(originalUrl);
        if (isNaverHost(host)) {
            return "네이버 뉴스";
        }
        String name = host == null ? null : pressNames.get(host);
        String parent = parentDomain(host);
        if (name == null && parent != null) {
            name = pressNames.get(parent);
        }
        return name != null ? name : host;
    }

    /** news.example.co.kr → example.co.kr. 점이 하나뿐이면(example.com) null. */
    static String parentDomain(String host) {
        if (host == null) {
            return null;
        }
        int dot = host.indexOf('.');
        String parent = dot < 0 ? null : host.substring(dot + 1);
        return parent != null && parent.indexOf('.') > 0 ? parent : null;
    }

    /** 네이버 뉴스 주소 (link가 네이버 주소일 때만, 아니면 null). */
    private static String naverUrl(NaverSearchClient.Item it) {
        return it.link() != null && isNaverHost(host(it.link())) ? it.link() : null;
    }

    /** 다음 쪽 start. 네이버는 start {@value #MAX_START}까지라 그 너머나 결과 끝이면 null. */
    private static Integer nextStart(NaverSearchClient.Page page, int from, int size) {
        long reachable = Math.min(page.total(), MAX_START);
        return from + size <= reachable && !page.items().isEmpty() ? from + size : null;
    }

    /** 정렬 값: 비우면 date(최신순), date|sim 말고는 400. */
    static Sort parseSort(String value) {
        String v = value == null ? "" : value.strip().toLowerCase(Locale.ROOT);
        return switch (v) {
            case "", "date" -> Sort.DATE;
            case "sim" -> Sort.SIM;
            default -> throw new ApiException(HttpStatus.BAD_REQUEST, "BAD_REQUEST",
                    "정렬은 date(최신순) 또는 sim(정확도순)만 쓸 수 있습니다: " + value);
        };
    }

    /** 요청한 키워드가 감시 키워드인지 (대소문자 무시). 비우면 첫 키워드. */
    static String pickKeyword(List<String> keywords, String requested) {
        if (keywords == null || keywords.isEmpty()) {
            throw new ApiException(HttpStatus.BAD_REQUEST, "BAD_REQUEST", "감시에 키워드가 없습니다.");
        }
        if (requested == null || requested.isBlank()) {
            return keywords.get(0);
        }
        String r = requested.strip();
        return keywords.stream().filter(k -> k.equalsIgnoreCase(r)).findFirst()
                .orElseThrow(() -> new ApiException(HttpStatus.BAD_REQUEST, "BAD_REQUEST",
                        "이 감시의 키워드가 아닙니다: " + r));
    }

    private static int clamp(Integer value, int fallback, int min, int max) {
        int v = value == null ? fallback : value;
        return Math.max(min, Math.min(max, v));
    }
}
