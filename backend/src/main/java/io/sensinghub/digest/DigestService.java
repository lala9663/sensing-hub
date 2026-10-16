package io.sensinghub.digest;

import java.sql.ResultSet;
import java.sql.SQLException;
import java.time.Duration;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.regex.Pattern;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpStatus;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;

import io.sensinghub.audit.AuditLogger;
import io.sensinghub.auth.CurrentUser;
import io.sensinghub.auth.Role;
import io.sensinghub.common.ApiException;
import io.sensinghub.common.UrlNormalizer;
import io.sensinghub.digest.DigestComposer.Digest;
import io.sensinghub.digest.DigestComposer.Document;
import io.sensinghub.digest.DigestComposer.WatchHit;

/**
 * 일간 다이제스트 구독 (M2-4). 사용자가 "어떤 감시·토픽·소스를 몇 시에 어디로" 받을지 등록하면 스케줄러가 매일 그 시각(KST)에
 * 지난 다이제스트 이후(last_sent_at, 없으면 24시간 전) 새로 수집된(collected_at) 문서를 묶어 보낸다.
 * <ul>
 *   <li>필터 {@code {"watch_ids":[...],"source_ids":[...],"topics":[...]}}: 세 목록 중 하나라도 맞으면 포함(합집합), 모두 비면 전체.
 *       watch_ids는 {@code document_watch.source_id}(어느 감시에 걸렸나), source_ids는 {@code document.source_id}, topics는
 *       보강 토픽 포함 여부다. 상한 200건(발행일 내림차순).</li>
 *   <li>문서가 0건이면 보내지 않고 EMPTY로 기록한다. 그래도 last_sent_at은 갱신해 빈 날이 기간을 늘리지 않게 한다.
 *       FAILED는 last_sent_at을 두어 다음 틱(같은 시간대 안)에 다시 시도한다.</li>
 *   <li>보낸 본문은 digest_run.body_text에 남는다 (화면·감사에서 다시 본다).</li>
 * </ul>
 */
@Service
public class DigestService {

    private static final Logger log = LoggerFactory.getLogger(DigestService.class);
    static final int MAX_DOCUMENTS = 200;
    static final Duration FIRST_PERIOD = Duration.ofHours(24);
    static final Set<String> CHANNELS = Set.of("EMAIL", "WEBHOOK");
    private static final Pattern EMAIL = Pattern.compile("^[^\\s@]+@[^\\s@]+\\.[^\\s@]+$");
    private static final TypeReference<List<String>> STRINGS = new TypeReference<>() { };
    private static final TypeReference<List<Map<String, Object>>> OBJECTS = new TypeReference<>() { };

    @JsonIgnoreProperties(ignoreUnknown = true)
    public record Filters(List<Long> watchIds, List<Long> sourceIds, List<String> topics) {
        public Filters {
            watchIds = distinct(watchIds);
            sourceIds = distinct(sourceIds);
            topics = distinct(topics);
        }

        public static Filters empty() {
            return new Filters(null, null, null);
        }

        private static <T> List<T> distinct(List<T> values) {
            return values == null ? List.of() : values.stream().filter(java.util.Objects::nonNull).distinct().toList();
        }
    }

    public record SubscriptionView(long id, long userId, String owner, String name, String channel, String target,
                                   int hourKst, Filters filters, boolean enabled, OffsetDateTime lastSentAt,
                                   OffsetDateTime createdAt, OffsetDateTime updatedAt) {
    }

    public record RunView(long id, long subscriptionId, OffsetDateTime periodFrom, OffsetDateTime periodTo,
                          int documentCount, String status, String error, String bodyText, OffsetDateTime sentAt) {
    }

    public record Preview(OffsetDateTime periodFrom, OffsetDateTime periodTo, int documentCount, boolean truncated,
                          String subject, String text, String html) {
    }

    private final JdbcClient jdbc;
    private final ObjectMapper json;
    private final AuditLogger audit;
    private final DigestComposer composer;
    private final DigestSender sender;

    public DigestService(JdbcClient jdbc, ObjectMapper json, AuditLogger audit, DigestComposer composer,
                         DigestSender sender) {
        this.jdbc = jdbc;
        this.json = json;
        this.audit = audit;
        this.composer = composer;
        this.sender = sender;
    }

    // ------------------------------------------------------------------ 구독 CRUD

    @Transactional
    public SubscriptionView create(CurrentUser user, String name, String channel, String target, Integer hourKst,
                                   Filters filters) {
        user.require(Role.VIEWER);
        String cleanName = cleanName(name);
        String cleanChannel = cleanChannel(channel);
        String cleanTarget = cleanTarget(cleanChannel, target);
        int hour = cleanHour(hourKst == null ? 8 : hourKst);
        Filters cleanFilters = cleanFilters(filters == null ? Filters.empty() : filters);
        long id = jdbc.sql("""
                INSERT INTO digest_subscription (user_id, name, channel, target, hour_kst, filters)
                VALUES (?, ?, ?, ?, ?, CAST(? AS jsonb)) RETURNING id
                """)
                .params(user.id(), cleanName, cleanChannel, cleanTarget, hour, toJson(cleanFilters))
                .query(Long.class).single();
        audit.log(user.id(), "DIGEST_SUBSCRIBED", "digest_subscription", id,
                "{\"name\":" + quote(cleanName) + ",\"channel\":\"" + cleanChannel + "\",\"hourKst\":" + hour + "}");
        return get(user, id);
    }

    /** 생략한(null) 필드는 그대로 둔다. */
    @Transactional
    public SubscriptionView update(CurrentUser user, long id, String name, String channel, String target,
                                   Integer hourKst, Filters filters, Boolean enabled) {
        SubscriptionView current = get(user, id);
        String newName = name == null ? current.name() : cleanName(name);
        String newChannel = channel == null ? current.channel() : cleanChannel(channel);
        String newTarget = target == null && newChannel.equals(current.channel()) ? current.target()
                : cleanTarget(newChannel, target == null ? current.target() : target);
        int newHour = hourKst == null ? current.hourKst() : cleanHour(hourKst);
        Filters newFilters = filters == null ? current.filters() : cleanFilters(filters);
        boolean newEnabled = enabled == null ? current.enabled() : enabled;
        jdbc.sql("""
                UPDATE digest_subscription
                SET name = ?, channel = ?, target = ?, hour_kst = ?, filters = CAST(? AS jsonb), enabled = ?, updated_at = now()
                WHERE id = ?
                """)
                .params(newName, newChannel, newTarget, newHour, toJson(newFilters), newEnabled, id).update();
        audit.log(user.id(), "DIGEST_UPDATED", "digest_subscription", id,
                "{\"name\":" + quote(newName) + ",\"channel\":\"" + newChannel + "\",\"hourKst\":" + newHour
                        + ",\"enabled\":" + newEnabled + "}");
        return get(user, id);
    }

    @Transactional
    public void delete(CurrentUser user, long id) {
        get(user, id);
        jdbc.sql("DELETE FROM digest_subscription WHERE id = ?").param(id).update();
        audit.log(user.id(), "DIGEST_UNSUBSCRIBED", "digest_subscription", id, null);
    }

    /** 내 구독. ADMIN은 전체. */
    public List<SubscriptionView> list(CurrentUser user) {
        user.require(Role.VIEWER);
        if (user.role().atLeast(Role.ADMIN)) {
            return jdbc.sql(SELECT + " ORDER BY s.id").query(this::mapSubscription).list();
        }
        return jdbc.sql(SELECT + " WHERE s.user_id = ? ORDER BY s.id").param(user.id()).query(this::mapSubscription).list();
    }

    /** 남의 구독은 404 (있다는 것도 알리지 않는다). ADMIN은 전부. */
    public SubscriptionView get(CurrentUser user, long id) {
        user.require(Role.VIEWER);
        SubscriptionView s = jdbc.sql(SELECT + " WHERE s.id = ?").param(id).query(this::mapSubscription).optional()
                .orElseThrow(() -> ApiException.notFound("다이제스트 구독"));
        if (!user.role().atLeast(Role.ADMIN) && s.userId() != user.id()) {
            throw ApiException.notFound("다이제스트 구독");
        }
        return s;
    }

    /** 스케줄러용: 권한 검사 없이 구독을 읽는다. 없으면 null. */
    public SubscriptionView load(long id) {
        return jdbc.sql(SELECT + " WHERE s.id = ?").param(id).query(this::mapSubscription).optional().orElse(null);
    }

    /** 최근 기록(보낸 본문 포함), 최신순. */
    public List<RunView> runs(CurrentUser user, long id, int limit) {
        get(user, id);
        return jdbc.sql("""
                SELECT id, subscription_id, period_from, period_to, document_count, status, error, body_text, sent_at
                FROM digest_run WHERE subscription_id = ? ORDER BY id DESC LIMIT ?
                """).params(id, Math.max(1, Math.min(100, limit))).query(this::mapRun).list();
    }

    // ------------------------------------------------------------------ 본문·전송

    /** 지금 기준으로 본문을 만들어 돌려준다 (보내지 않고 기록도 남기지 않는다). */
    public Preview preview(CurrentUser user, long id) {
        SubscriptionView s = get(user, id);
        Digest d = compose(s, OffsetDateTime.now());
        return new Preview(d.periodFrom(), d.periodTo(), d.documentCount(), d.truncated(), d.subject(), d.text(), d.html());
    }

    /** 즉시 한 번 보낸다 (테스트용). 기록·last_sent_at 갱신은 스케줄 전송과 같다. */
    public RunView sendNow(CurrentUser user, long id) {
        SubscriptionView s = get(user, id);
        return deliver(s, OffsetDateTime.now(), user.id());
    }

    /**
     * 구독 하나를 처리한다: 본문 → 전송 → digest_run 기록 → last_sent_at 갱신.
     * 0건이면 EMPTY(보내지 않음, last_sent_at 갱신), 전송 실패는 FAILED(last_sent_at 그대로). 예외는 밖으로 내지 않는다.
     */
    public RunView deliver(SubscriptionView s, OffsetDateTime now, Long actorId) {
        Digest digest;
        try {
            digest = compose(s, now);
        } catch (RuntimeException e) {
            log.error("digest {} compose failed", s.id(), e);
            return record(s, actorId, from(s, now), now, 0, "FAILED", "본문 생성 실패: " + brief(e), null, false);
        }
        if (digest.documentCount() == 0) {
            return record(s, actorId, digest.periodFrom(), digest.periodTo(), 0, "EMPTY", null, null, true);
        }
        try {
            sender.send(s.channel(), s.target(), digest);
        } catch (DigestSender.NotSentException e) {
            return record(s, actorId, digest.periodFrom(), digest.periodTo(), digest.documentCount(), "FAILED",
                    e.getMessage(), digest.text(), false);
        } catch (RuntimeException e) {
            log.error("digest {} send failed", s.id(), e);
            return record(s, actorId, digest.periodFrom(), digest.periodTo(), digest.documentCount(), "FAILED",
                    "전송 실패: " + brief(e), digest.text(), false);
        }
        return record(s, actorId, digest.periodFrom(), digest.periodTo(), digest.documentCount(), "SENT", null,
                digest.text(), true);
    }

    /** digest_run 한 행 + (advance면) last_sent_at 갱신 + 감사 로그. 전송 뒤에 부르므로 트랜잭션으로 묶지 않는다. */
    private RunView record(SubscriptionView s, Long actorId, OffsetDateTime from, OffsetDateTime to, int count,
                           String status, String error, String bodyText, boolean advance) {
        String err = error != null && error.length() > 500 ? error.substring(0, 500) : error;
        long runId = jdbc.sql("""
                INSERT INTO digest_run (subscription_id, period_from, period_to, document_count, status, error, body_text)
                VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id
                """).params(s.id(), from, to, count, status, err, bodyText).query(Long.class).single();
        if (advance) {
            jdbc.sql("UPDATE digest_subscription SET last_sent_at = ?, updated_at = now() WHERE id = ?")
                    .params(to, s.id()).update();
        }
        if (status.equals("SENT")) {
            audit.log(actorId, "DIGEST_SENT", "digest_subscription", s.id(),
                    "{\"runId\":" + runId + ",\"channel\":\"" + s.channel() + "\",\"documents\":" + count + "}");
        } else if (status.equals("FAILED")) {
            audit.log(actorId, "DIGEST_FAILED", "digest_subscription", s.id(),
                    "{\"runId\":" + runId + ",\"channel\":\"" + s.channel() + "\",\"error\":" + quote(err) + "}");
        }
        return jdbc.sql("""
                SELECT id, subscription_id, period_from, period_to, document_count, status, error, body_text, sent_at
                FROM digest_run WHERE id = ?
                """).param(runId).query(this::mapRun).single();
    }

    Digest compose(SubscriptionView s, OffsetDateTime now) {
        OffsetDateTime from = from(s, now);
        List<Document> docs = loadDocuments(s.filters(), from, now, MAX_DOCUMENTS + 1);
        boolean truncated = docs.size() > MAX_DOCUMENTS;
        if (truncated) {
            docs = docs.subList(0, MAX_DOCUMENTS);
        }
        return composer.compose(s.name(), from, now, docs, new LinkedHashSet<>(s.filters().watchIds()), truncated);
    }

    private static OffsetDateTime from(SubscriptionView s, OffsetDateTime now) {
        return s.lastSentAt() == null ? now.minus(FIRST_PERIOD) : s.lastSentAt();
    }

    /**
     * 기간(collected_at, from 초과 to 이하)의 문서를 필터(합집합)로 걸러 발행일 내림차순으로 읽는다.
     * DocumentService의 목록 SQL과 같은 조인에 감시 이름을 더한 것이다.
     */
    List<Document> loadDocuments(Filters f, OffsetDateTime from, OffsetDateTime to, int limit) {
        StringBuilder sql = new StringBuilder("""
                SELECT d.id, d.url, d.title, d.published_at, t.domain, e.summary,
                       COALESCE(e.topics::text, '[]') AS topics,
                       (SELECT COALESCE(json_agg(json_build_object('watchId', w.source_id, 'watch', ws.watch_name,
                                                                   'keyword', w.keyword)
                                                 ORDER BY w.source_id, w.matched_at), '[]'::json)::text
                        FROM document_watch w JOIN source ws ON ws.id = w.source_id
                        WHERE w.document_id = d.id) AS hits
                FROM document d
                JOIN source s ON s.id = d.source_id
                JOIN site t ON t.id = s.site_id
                LEFT JOIN document_enrichment e ON e.document_id = d.id
                WHERE d.collected_at > ? AND d.collected_at <= ?
                """);
        List<Object> params = new ArrayList<>();
        params.add(from);
        params.add(to);
        List<String> any = new ArrayList<>();
        if (!f.watchIds().isEmpty()) {
            any.add("EXISTS (SELECT 1 FROM document_watch w WHERE w.document_id = d.id AND w.source_id IN ("
                    + placeholders(f.watchIds().size()) + "))");
            params.addAll(f.watchIds());
        }
        if (!f.sourceIds().isEmpty()) {
            any.add("d.source_id IN (" + placeholders(f.sourceIds().size()) + ")");
            params.addAll(f.sourceIds());
        }
        for (String topic : f.topics()) {
            any.add("e.topics @> CAST(? AS jsonb)");
            params.add(toJson(List.of(topic)));
        }
        if (!any.isEmpty()) {
            sql.append(" AND (").append(String.join(" OR ", any)).append(")");
        }
        sql.append(" ORDER BY d.published_at DESC NULLS LAST, d.id DESC LIMIT ?");
        params.add(limit);
        return jdbc.sql(sql.toString()).params(params).query(this::mapDocument).list();
    }

    // ------------------------------------------------------------------ 검증

    static String cleanName(String name) {
        String cleaned = name == null ? "" : name.strip().replaceAll("\\s+", " ");
        if (cleaned.isEmpty()) {
            throw bad("구독 이름을 넣어 주세요.");
        }
        if (cleaned.length() > 200) {
            throw bad("구독 이름은 200자 이하여야 합니다.");
        }
        return cleaned;
    }

    static String cleanChannel(String channel) {
        String c = channel == null ? "" : channel.strip().toUpperCase();
        if (!CHANNELS.contains(c)) {
            throw bad("channel은 EMAIL 또는 WEBHOOK이어야 합니다.");
        }
        return c;
    }

    /** EMAIL이면 메일 주소 형식, WEBHOOK이면 http(s) 절대 URL이고 내부망 주소는 거부(UrlNormalizer). */
    static String cleanTarget(String channel, String target) {
        String t = target == null ? "" : target.strip();
        if (t.isEmpty()) {
            throw bad(channel.equals("EMAIL") ? "받을 메일 주소를 넣어 주세요." : "웹훅 URL을 넣어 주세요.");
        }
        if (t.length() > 500) {
            throw bad("target은 500자 이하여야 합니다.");
        }
        if (channel.equals("EMAIL")) {
            if (!EMAIL.matcher(t).matches()) {
                throw bad("메일 주소 형식이 올바르지 않습니다.");
            }
            return t;
        }
        if (!t.contains("://")) {
            throw bad("웹훅 URL은 http:// 또는 https://로 시작하는 절대 주소여야 합니다.");
        }
        UrlNormalizer.normalize(t);   // 형식·http(s)·내부망 검사. 아니면 400 INVALID_URL
        return t;
    }

    static int cleanHour(int hour) {
        if (hour < 0 || hour > 23) {
            throw bad("hourKst는 0~23이어야 합니다.");
        }
        return hour;
    }

    Filters cleanFilters(Filters f) {
        for (String topic : f.topics()) {
            if (!DigestComposer.TOPIC_ORDER.contains(topic)) {
                throw bad("알 수 없는 토픽입니다: " + topic + " (" + String.join(", ", DigestComposer.TOPIC_ORDER) + ")");
            }
        }
        if (!f.watchIds().isEmpty()) {
            long found = jdbc.sql("SELECT count(*) FROM source WHERE kind = 'WATCH' AND id IN ("
                    + placeholders(f.watchIds().size()) + ")").params(new ArrayList<Object>(f.watchIds()))
                    .query(Long.class).single();
            if (found != f.watchIds().size()) {
                throw bad("없는 키워드 감시 id가 있습니다: " + f.watchIds());
            }
        }
        if (!f.sourceIds().isEmpty()) {
            long found = jdbc.sql("SELECT count(*) FROM source WHERE id IN (" + placeholders(f.sourceIds().size()) + ")")
                    .params(new ArrayList<Object>(f.sourceIds())).query(Long.class).single();
            if (found != f.sourceIds().size()) {
                throw bad("없는 소스 id가 있습니다: " + f.sourceIds());
            }
        }
        return f;
    }

    private static ApiException bad(String message) {
        return new ApiException(HttpStatus.BAD_REQUEST, "BAD_REQUEST", message);
    }

    private static String placeholders(int n) {
        return String.join(", ", java.util.Collections.nCopies(n, "?"));
    }

    private static String brief(Exception e) {
        String m = e.getMessage() == null ? e.getClass().getSimpleName() : e.getMessage();
        return m.length() > 300 ? m.substring(0, 300) : m;
    }

    private static String quote(String text) {
        return text == null ? "null" : "\"" + text.replace("\\", "\\\\").replace("\"", "\\\"")
                .replace("\n", "\\n").replace("\r", "") + "\"";
    }

    // ------------------------------------------------------------------ 매핑

    private static final String SELECT = """
            SELECT s.id, s.user_id, u.name AS owner, s.name, s.channel, s.target, s.hour_kst, s.filters::text AS filters,
                   s.enabled, s.last_sent_at, s.created_at, s.updated_at
            FROM digest_subscription s
            JOIN app_user u ON u.id = s.user_id
            """;

    private SubscriptionView mapSubscription(ResultSet rs, int n) throws SQLException {
        return new SubscriptionView(rs.getLong("id"), rs.getLong("user_id"), rs.getString("owner"), rs.getString("name"),
                rs.getString("channel"), rs.getString("target"), rs.getInt("hour_kst"), filters(rs.getString("filters")),
                rs.getBoolean("enabled"), rs.getObject("last_sent_at", OffsetDateTime.class),
                rs.getObject("created_at", OffsetDateTime.class), rs.getObject("updated_at", OffsetDateTime.class));
    }

    private RunView mapRun(ResultSet rs, int n) throws SQLException {
        return new RunView(rs.getLong("id"), rs.getLong("subscription_id"),
                rs.getObject("period_from", OffsetDateTime.class), rs.getObject("period_to", OffsetDateTime.class),
                rs.getInt("document_count"), rs.getString("status"), rs.getString("error"), rs.getString("body_text"),
                rs.getObject("sent_at", OffsetDateTime.class));
    }

    private Document mapDocument(ResultSet rs, int n) throws SQLException {
        List<WatchHit> hits = new ArrayList<>();
        for (Map<String, Object> h : objects(rs.getString("hits"))) {
            Object id = h.get("watchId");
            if (id instanceof Number num) {
                hits.add(new WatchHit(num.longValue(), (String) h.get("watch"), (String) h.get("keyword")));
            }
        }
        return new Document(rs.getLong("id"), rs.getString("url"), rs.getString("title"),
                rs.getObject("published_at", OffsetDateTime.class), rs.getString("domain"), rs.getString("summary"),
                strings(rs.getString("topics")), hits);
    }

    /** 저장된 filters jsonb ({"watch_ids":[...],"source_ids":[...],"topics":[...]}) → Filters. */
    Filters filters(String jsonText) {
        if (jsonText == null || jsonText.isBlank()) {
            return Filters.empty();
        }
        try {
            Map<String, Object> m = json.readValue(jsonText, new TypeReference<Map<String, Object>>() { });
            return new Filters(longs(m.get("watch_ids")), longs(m.get("source_ids")), stringList(m.get("topics")));
        } catch (JsonProcessingException e) {
            return Filters.empty();
        }
    }

    private static List<Long> longs(Object value) {
        List<Long> out = new ArrayList<>();
        if (value instanceof List<?> list) {
            for (Object o : list) {
                if (o instanceof Number num) {
                    out.add(num.longValue());
                }
            }
        }
        return out;
    }

    private static List<String> stringList(Object value) {
        List<String> out = new ArrayList<>();
        if (value instanceof List<?> list) {
            for (Object o : list) {
                if (o instanceof String s) {
                    out.add(s);
                }
            }
        }
        return out;
    }

    private String toJson(Filters f) {
        Map<String, Object> m = new java.util.LinkedHashMap<>();
        m.put("watch_ids", f.watchIds());
        m.put("source_ids", f.sourceIds());
        m.put("topics", f.topics());
        return toJson(m);
    }

    private String toJson(Object value) {
        try {
            return json.writeValueAsString(value);
        } catch (JsonProcessingException e) {
            throw new IllegalStateException(e);
        }
    }

    private List<String> strings(String jsonArray) {
        if (jsonArray == null || jsonArray.isBlank()) {
            return List.of();
        }
        try {
            return json.readValue(jsonArray, STRINGS);
        } catch (JsonProcessingException e) {
            return List.of();
        }
    }

    private List<Map<String, Object>> objects(String jsonArray) {
        if (jsonArray == null || jsonArray.isBlank()) {
            return List.of();
        }
        try {
            return json.readValue(jsonArray, OBJECTS);
        } catch (JsonProcessingException e) {
            return List.of();
        }
    }
}
