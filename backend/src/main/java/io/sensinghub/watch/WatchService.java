package io.sensinghub.watch;

import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpStatus;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;

import io.sensinghub.audit.AuditLogger;
import io.sensinghub.auth.CurrentUser;
import io.sensinghub.auth.Role;
import io.sensinghub.common.ApiException;

/**
 * 키워드 감시 (M2). 현업은 사이트가 아니라 키워드(자사·경쟁사·이슈) 단위로 보도를 본다.
 * <ul>
 *   <li>감시 하나는 {@code source} 한 행({@code kind = WATCH}, {@code watch_name})이고, 키워드는 설정 버전의
 *       config({@code {"method":"API","provider":"naver_news","keywords":[...],"exclude":[...]}})에 둔다.
 *       site는 제공자 도메인 한 행(news.naver.com)을 여러 감시가 공유한다.</li>
 *   <li>등록 규칙은 사이트 등록과 같다: REGISTRAR 이상은 바로 ONBOARD 작업이 들어가고, VIEWER는 승인 대기
 *       ({@code registration_request} AWAITING_APPROVAL, 승인은 /registrations/{id}/approve).</li>
 *   <li>진행 상태는 registration_request 한 행({@code input_url = "watch:<이름>"})으로 /registrations/mine에서 본다.
 *       워커는 키가 없으면 REVIEW/API_KEY_REQUIRED로 멈추고, 관리자가 네이버 키를 넣으면 자동으로 다시 진행한다.</li>
 *   <li>키워드 수정은 설정 새 버전 활성화다 (이전 버전은 RETIRED로 남아 롤백할 수 있다).</li>
 * </ul>
 */
@Service
public class WatchService {

    public static final String PROVIDER = "naver_news";
    public static final String DOMAIN = "news.naver.com";
    public static final String BASE_URL = "https://news.naver.com";
    static final int DEFAULT_INTERVAL_MINUTES = 60;
    static final int MAX_KEYWORDS = 50;
    /** 회사(거래처) 감시의 이름 접두어 (M3). */
    public static final String COMPANY_PREFIX = "회사:";

    public record WatchView(long id, String name, List<String> keywords, List<String> exclude, String status,
                            String statusReason, Integer configVersion, int intervalMinutes,
                            OffsetDateTime lastSuccessAt, OffsetDateTime nextRunAt, long documentCount,
                            String requestedBy, Long registrationId, String registrationStatus,
                            String registrationMessage, OffsetDateTime createdAt) {
    }

    private final JdbcClient jdbc;
    private final AuditLogger audit;
    private final ObjectMapper json;
    private final boolean viewerNeedsApproval;

    public WatchService(JdbcClient jdbc, AuditLogger audit, ObjectMapper json,
                        @Value("${sensing.registration.viewer-needs-approval}") boolean viewerNeedsApproval) {
        this.jdbc = jdbc;
        this.audit = audit;
        this.json = json;
        this.viewerNeedsApproval = viewerNeedsApproval;
    }

    @Transactional
    public WatchView create(CurrentUser user, String name, List<String> keywords, List<String> exclude) {
        return create(user, name, keywords, exclude, DEFAULT_INTERVAL_MINUTES, "키워드 감시 생성");
    }

    /**
     * 회사(거래처) 마스터의 감시 (M3): 이름 "회사:&lt;회사명&gt;", 키워드는 회사 이름·별칭, 주기는 호출자가 정한다(360분).
     * 등록 규칙(REGISTRAR 이상 즉시 진행, 키 없으면 API_KEY_REQUIRED 큐)은 일반 감시와 같다.
     */
    @Transactional
    public WatchView createForCompany(CurrentUser user, String companyName, List<String> keywords, int intervalMinutes) {
        return create(user, COMPANY_PREFIX + companyName, keywords, List.of(), intervalMinutes, "회사 감시 생성");
    }

    /** 회사 이름·별칭이 바뀌면 그 감시의 이름과 키워드를 새 설정 버전으로 갱신한다 (소유자 검사 없음: 회사 API가 권한을 검사한다). */
    @Transactional
    public void updateForCompany(CurrentUser user, long sourceId, String companyName, List<String> keywords) {
        List<String> terms = cleanTerms(keywords, "keywords");
        if (terms.isEmpty()) {
            throw new ApiException(HttpStatus.BAD_REQUEST, "BAD_REQUEST", "키워드를 하나 이상 넣어 주세요.");
        }
        String watchName = cleanName(COMPANY_PREFIX + companyName);
        jdbc.sql("UPDATE source SET watch_name = ?, name = ?, updated_at = now() WHERE id = ?")
                .params(watchName, "watch:" + watchName, sourceId).update();
        int version = activateVersion(sourceId, user, terms, List.of(), "회사 이름·별칭 수정");
        jdbc.sql("UPDATE source SET next_run_at = now(), updated_at = now() WHERE id = ? AND status = 'ACTIVE'")
                .param(sourceId).update();
        audit.log(user.id(), "WATCH_UPDATED", "source", sourceId,
                "{\"version\":" + version + ",\"keywords\":" + terms.size() + ",\"company\":true}");
    }

    /** 회사를 보관(ARCHIVED)하면 그 감시는 제외(EXCLUDED)한다. 같은 이름으로 다시 만들 수 있게 열린 감시에서 빠진다. */
    @Transactional
    public void excludeForCompany(CurrentUser user, long sourceId, String reason) {
        jdbc.sql("UPDATE source SET status = 'EXCLUDED', status_reason = ?, updated_at = now() WHERE id = ?")
                .params(reason, sourceId).update();
        jdbc.sql("""
                UPDATE registration_request SET status = 'REJECTED', step = 'REJECTED', result_message = ?, updated_at = now()
                WHERE source_id = ? AND status IN ('RECEIVED', 'PROCESSING', 'REVIEW', 'AWAITING_APPROVAL')
                """).params(reason, sourceId).update();
        audit.log(user.id(), "WATCH_EXCLUDED", "source", sourceId, "{\"reason\":" + quote(reason) + "}");
    }

    private WatchView create(CurrentUser user, String name, List<String> keywords, List<String> exclude,
                             int intervalMinutes, String note) {
        String watchName = cleanName(name);
        List<String> terms = cleanTerms(keywords, "keywords");
        List<String> excluded = cleanTerms(exclude, "exclude");
        if (terms.isEmpty()) {
            throw new ApiException(HttpStatus.BAD_REQUEST, "BAD_REQUEST", "키워드를 하나 이상 넣어 주세요.");
        }
        if (findOpenWatch(watchName).isPresent()) {
            throw ApiException.conflict("같은 이름의 감시가 이미 있습니다: " + watchName);
        }

        long siteId = jdbc.sql("""
                INSERT INTO site (domain, base_url, risk_flags)
                VALUES (?, ?, CAST(? AS jsonb))
                ON CONFLICT (domain) DO UPDATE SET domain = EXCLUDED.domain
                RETURNING id
                """)
                .params(DOMAIN, BASE_URL, "{\"api_provider\":\"" + PROVIDER + "\"}")
                .query(Long.class).single();
        long sourceId = jdbc.sql("""
                INSERT INTO source (site_id, name, kind, watch_name, method, status, interval_minutes, requested_by)
                VALUES (?, ?, 'WATCH', ?, 'API', 'PENDING', ?, ?) RETURNING id
                """)
                .params(siteId, "watch:" + watchName, watchName, intervalMinutes, user.id())
                .query(Long.class).single();
        activateVersion(sourceId, user, terms, excluded, note);

        boolean needsApproval = viewerNeedsApproval && !user.role().atLeast(Role.REGISTRAR);
        long requestId = jdbc.sql("""
                INSERT INTO registration_request (user_id, input_url, normalized_domain, channel, status, step,
                                                  result_message, source_id)
                VALUES (?, ?, ?, 'WEB', ?, ?, ?, ?) RETURNING id
                """)
                .params(user.id(), "watch:" + watchName, DOMAIN,
                        needsApproval ? "AWAITING_APPROVAL" : "PROCESSING",
                        needsApproval ? null : "QUEUED",
                        needsApproval ? "키워드 감시 요청이 접수되었습니다. 관리자 승인 후 자동으로 진행됩니다."
                                : "키워드 감시를 접수했어요. 네이버 뉴스 검색 API 키를 확인한 뒤 첫 수집을 시작합니다.",
                        sourceId)
                .query(Long.class).single();
        if (!needsApproval) {
            jdbc.sql("INSERT INTO crawl_job (source_id, job_type, trigger) VALUES (?, 'ONBOARD', 'REGISTRATION')")
                    .param(sourceId).update();
        }
        audit.log(user.id(), needsApproval ? "WATCH_REQUESTED" : "WATCH_CREATED", "source", sourceId,
                "{\"name\":" + quote(watchName) + ",\"keywords\":" + terms.size()
                        + ",\"registrationId\":" + requestId + "}");
        return get(user, sourceId);
    }

    /** 키워드 수정 = 설정 새 버전 활성화. 감시 중(ACTIVE)이면 다음 틱에 새 키워드로 바로 수집하게 next_run_at을 당긴다. */
    @Transactional
    public WatchView update(CurrentUser user, long id, String name, List<String> keywords, List<String> exclude) {
        WatchView current = get(user, id);
        List<String> terms = keywords == null ? current.keywords() : cleanTerms(keywords, "keywords");
        List<String> excluded = exclude == null ? current.exclude() : cleanTerms(exclude, "exclude");
        if (terms.isEmpty()) {
            throw new ApiException(HttpStatus.BAD_REQUEST, "BAD_REQUEST", "키워드를 하나 이상 넣어 주세요.");
        }
        if (name != null && !name.isBlank()) {
            String watchName = cleanName(name);
            Optional<Long> other = findOpenWatch(watchName);
            if (other.isPresent() && other.get() != id) {
                throw ApiException.conflict("같은 이름의 감시가 이미 있습니다: " + watchName);
            }
            jdbc.sql("UPDATE source SET watch_name = ?, name = ?, updated_at = now() WHERE id = ?")
                    .params(watchName, "watch:" + watchName, id).update();
        }
        int version = activateVersion(id, user, terms, excluded, "키워드 수정");
        jdbc.sql("UPDATE source SET next_run_at = now(), updated_at = now() WHERE id = ? AND status = 'ACTIVE'")
                .param(id).update();
        audit.log(user.id(), "WATCH_UPDATED", "source", id,
                "{\"version\":" + version + ",\"keywords\":" + terms.size() + "}");
        return get(user, id);
    }

    /** 내 감시. ADMIN은 전체. */
    public List<WatchView> list(CurrentUser user) {
        if (user.role().atLeast(Role.ADMIN)) {
            return jdbc.sql(SELECT + " ORDER BY s.id").query(this::map).list();
        }
        return jdbc.sql(SELECT + " WHERE s.requested_by = ? ORDER BY s.id").param(user.id()).query(this::map).list();
    }

    public WatchView get(CurrentUser user, long id) {
        WatchView w = jdbc.sql(SELECT + " WHERE s.id = ?").param(id).query(this::map).optional()
                .orElseThrow(() -> ApiException.notFound("키워드 감시"));
        if (!user.role().atLeast(Role.ADMIN) && !isOwner(user, id)) {
            throw ApiException.notFound("키워드 감시");
        }
        return w;
    }

    // ------------------------------------------------------------------

    private boolean isOwner(CurrentUser user, long sourceId) {
        return jdbc.sql("SELECT count(*) FROM source WHERE id = ? AND requested_by = ?")
                .params(sourceId, user.id()).query(Long.class).single() > 0;
    }

    private Optional<Long> findOpenWatch(String watchName) {
        return jdbc.sql("""
                SELECT id FROM source WHERE kind = 'WATCH' AND lower(watch_name) = lower(?) AND status <> 'EXCLUDED'
                ORDER BY id LIMIT 1
                """)
                .param(watchName).query(Long.class).optional();
    }

    /** 새 설정 버전을 ACTIVE로 만들고 소스가 가리키게 한다 (이전 ACTIVE는 RETIRED). 버전 번호를 돌려준다. */
    private int activateVersion(long sourceId, CurrentUser user, List<String> keywords, List<String> exclude,
                                String note) {
        Map<String, Object> config = new LinkedHashMap<>();
        config.put("method", "API");
        config.put("provider", PROVIDER);
        config.put("keywords", keywords);
        config.put("exclude", exclude);
        int version = jdbc.sql("SELECT COALESCE(MAX(version), 0) + 1 FROM source_config_version WHERE source_id = ?")
                .param(sourceId).query(Integer.class).single();
        jdbc.sql("UPDATE source_config_version SET status = 'RETIRED' WHERE source_id = ? AND status = 'ACTIVE'")
                .param(sourceId).update();
        long configId = jdbc.sql("""
                INSERT INTO source_config_version (source_id, version, config, status, change_note, created_by)
                VALUES (?, ?, CAST(? AS jsonb), 'ACTIVE', ?, ?) RETURNING id
                """)
                .params(sourceId, version, toJson(config), note, user.id())
                .query(Long.class).single();
        jdbc.sql("UPDATE source SET active_config_version_id = ?, method = 'API', updated_at = now() WHERE id = ?")
                .params(configId, sourceId).update();
        return version;
    }

    static String cleanName(String name) {
        String cleaned = name == null ? "" : name.strip().replaceAll("\\s+", " ");
        if (cleaned.isEmpty()) {
            throw new ApiException(HttpStatus.BAD_REQUEST, "BAD_REQUEST", "감시 이름을 넣어 주세요.");
        }
        if (cleaned.length() > 200) {
            throw new ApiException(HttpStatus.BAD_REQUEST, "BAD_REQUEST", "감시 이름은 200자 이하여야 합니다.");
        }
        return cleaned;
    }

    static List<String> cleanTerms(List<String> values, String field) {
        List<String> out = new ArrayList<>();
        if (values == null) {
            return out;
        }
        for (String v : values) {
            String term = v == null ? "" : v.strip().replaceAll("\\s+", " ");
            if (term.isEmpty() || out.stream().anyMatch(t -> t.equalsIgnoreCase(term))) {
                continue;
            }
            if (term.length() > 200) {
                throw new ApiException(HttpStatus.BAD_REQUEST, "BAD_REQUEST", field + "의 항목은 200자 이하여야 합니다.");
            }
            out.add(term);
        }
        if (out.size() > MAX_KEYWORDS) {
            throw new ApiException(HttpStatus.BAD_REQUEST, "BAD_REQUEST",
                    field + "은(는) " + MAX_KEYWORDS + "개 이하여야 합니다.");
        }
        return out;
    }

    private String toJson(Object value) {
        try {
            return json.writeValueAsString(value);
        } catch (JsonProcessingException e) {
            throw new IllegalStateException(e);
        }
    }

    private List<String> terms(String configJson, String field) {
        if (configJson == null) {
            return List.of();
        }
        try {
            Map<String, Object> config = json.readValue(configJson, new TypeReference<Map<String, Object>>() { });
            Object value = config.get(field);
            if (value instanceof List<?> list) {
                return list.stream().filter(String.class::isInstance).map(String.class::cast).toList();
            }
            return List.of();
        } catch (JsonProcessingException e) {
            return List.of();
        }
    }

    private static String quote(String text) {
        return "\"" + text.replace("\\", "\\\\").replace("\"", "\\\"") + "\"";
    }

    private static final String SELECT = """
            SELECT s.id, s.watch_name, s.status, s.status_reason, s.interval_minutes, s.last_success_at, s.next_run_at,
                   s.created_at, v.version AS config_version, v.config::text AS config, u.name AS requested_by,
                   (SELECT count(*) FROM document_watch w WHERE w.source_id = s.id) AS document_count,
                   r.id AS registration_id, r.status AS registration_status, r.result_message AS registration_message
            FROM source s
            LEFT JOIN source_config_version v ON v.id = s.active_config_version_id
            LEFT JOIN app_user u ON u.id = s.requested_by
            LEFT JOIN LATERAL (
                SELECT id, status, result_message FROM registration_request
                WHERE source_id = s.id ORDER BY id DESC LIMIT 1) r ON true
            """;

    private WatchView map(java.sql.ResultSet rs, int n) throws java.sql.SQLException {
        String config = rs.getString("config");
        return new WatchView(rs.getLong("id"), rs.getString("watch_name"), terms(config, "keywords"),
                terms(config, "exclude"), rs.getString("status"), rs.getString("status_reason"),
                (Integer) rs.getObject("config_version"), rs.getInt("interval_minutes"),
                rs.getObject("last_success_at", OffsetDateTime.class), rs.getObject("next_run_at", OffsetDateTime.class),
                rs.getLong("document_count"), rs.getString("requested_by"),
                rs.getObject("registration_id", Long.class), rs.getString("registration_status"),
                rs.getString("registration_message"), rs.getObject("created_at", OffsetDateTime.class));
    }
}
