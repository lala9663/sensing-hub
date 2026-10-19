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

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.dao.DataAccessException;
import org.springframework.http.HttpStatus;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Service;

import io.sensinghub.auth.CurrentUser;
import io.sensinghub.auth.Role;
import io.sensinghub.common.ApiException;
import io.sensinghub.naver.NaverSearchClient.Kind;
import io.sensinghub.naver.NaverSearchClient.NaverSearchException;
import io.sensinghub.watch.WatchService;
import io.sensinghub.watch.WatchService.WatchView;

/**
 * 네이버 뉴스 "실시간" 보기 (표시 전용, 기본 모드). 감시 화면에서 감시 키워드 하나로 네이버 최신 뉴스를 불러와 제목·언론사·시각·
 * 원문/네이버 링크만 돌려준다.
 * <ul>
 *   <li>2026-09-07 시행 NAVER API HUB 개정 약관(검색 결과의 저장·캐싱·AI 입력·제3자 제공 금지)에 맞춰 결과는 <b>어디에도 남기지 않는다</b>:
 *       document·document_watch·임베딩·다이제스트·챗봇·LLM 어느 쪽에도 넣지 않고, 메모리 캐시도 두지 않는다(약관 확인 전까지).
 *       응답에는 {@code Cache-Control: no-store}를 붙인다(컨트롤러). DB에 쓰는 것은 키 상태(api_credential.status)뿐이다.</li>
 *   <li>요약(description)은 돌려주지 않는다. 화면에는 출처 "NAVER 검색 결과"를 붙인다.</li>
 *   <li>키는 api_credential(provider naver_news, "Client ID:Client Secret")을 pgcrypto로 풀어 쓰고 응답·로그에 내지 않는다.</li>
 *   <li>모드가 off거나 키가 없으면(또는 INVALID면) 화면이 이 보기를 숨긴다({@link #status}).</li>
 * </ul>
 */
@Service
public class NaverNewsService {

    private static final Logger log = LoggerFactory.getLogger(NaverNewsService.class);
    public static final String PROVIDER = WatchService.PROVIDER;
    static final int DEFAULT_DISPLAY = 20;
    static final int MAX_DISPLAY = 50;
    static final int MAX_START = 1000;

    /**
     * @param liveAvailable 감시 화면에 "네이버 뉴스 실시간"을 보여 줄지 (모드가 off가 아니고 쓸 수 있는 키가 있다)
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

    public record VerifyResult(String provider, String status, String message, OffsetDateTime lastVerifiedAt) {
    }

    private record Credential(String clientId, String clientSecret, String status, String lastError) {
    }

    private final JdbcClient jdbc;
    private final NaverSettings settings;
    private final NaverSearchClient client;
    private final WatchService watches;
    private final String secretKey;

    public NaverNewsService(JdbcClient jdbc, NaverSettings settings, NaverSearchClient client, WatchService watches,
                            @Value("${sensing.secret-key:}") String secretKey) {
        this.jdbc = jdbc;
        this.settings = settings;
        this.client = client;
        this.watches = watches;
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

        NaverSearchClient.Page page = call(key, term, size, from);
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
            String host = host(original);
            String naverUrl = it.link() != null && isNaverHost(host(it.link())) ? it.link() : null;
            String press = isNaverHost(host) ? "네이버 뉴스" : pressNames.getOrDefault(host, host);
            items.add(new LiveItem(it.title(), press, it.publishedAt(), original, naverUrl));
        }
        long reachable = Math.min(page.total(), MAX_START);
        Integer next = from + size <= reachable && !page.items().isEmpty() ? from + size : null;
        log.info("naver live search watch={} start={} display={} -> {} items (api {})", watchId, from, size, items.size(),
                settings.api());
        return new LiveResult(watch.id(), watch.name(), term, watch.keywords(), page.total(), from, size, items, excluded, next,
                OffsetDateTime.now(), NaverSettings.SOURCE_LABEL, settings.mode().name().toLowerCase(Locale.ROOT));
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
                    + "건). 키워드 감시 화면에서 '네이버 뉴스 실시간'을 볼 수 있습니다.";
        } catch (NaverSearchException e) {
            if (e.kind() == Kind.KEY_INVALID) {
                mark("INVALID", e.getMessage());
            }
            return e.getMessage();
        }
    }

    // ------------------------------------------------------------------

    private NaverSearchClient.Page call(Credential key, String term, int size, int from) {
        try {
            return client.search(key.clientId(), key.clientSecret(), term, size, from);
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

    /** 실시간 보기에 쓸 키. 없거나 INVALID면 409 (화면은 status로 이 보기를 숨긴다). */
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

    /** 원문 주소의 호스트 → 우리가 등록한 사이트 이름 (site.name). 없으면 표에 없다 (호출한 쪽이 호스트를 쓴다). */
    private Map<String, String> pressNames(List<NaverSearchClient.Item> items) {
        Set<String> hosts = new LinkedHashSet<>();
        for (NaverSearchClient.Item it : items) {
            String host = host(it.originalLink() != null ? it.originalLink() : it.link());
            if (host != null) {
                hosts.add(host);
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
