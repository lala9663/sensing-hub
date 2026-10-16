package io.sensinghub.review;

import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;

import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Service;

import io.sensinghub.auth.CurrentUser;
import io.sensinghub.auth.Role;
import io.sensinghub.common.ApiException;

/**
 * 관리자 처리 대기 큐. 자동으로 판단할 수 없어 사람이 봐야 하는 항목을 한곳에 모은다.
 * <ul>
 *   <li>APPROVAL: VIEWER의 등록 요청 승인 대기 → approve | reject</li>
 *   <li>API_KEY: 공개 API 제공처(DART 등)인데 키가 없거나 틀려 멈춘 소스 → set_api_key | exclude</li>
 *   <li>REVIEW: 자동 탐색·품질 검사 실패로 등록이 멈춘 소스 → rediscover | exclude</li>
 *   <li>BROKEN: 주기 수집 중 구조 변경·급감으로 멈춘 소스 → recheck | rediscover | pause | exclude</li>
 *   <li>BLOCKED: 차단 신호로 멈춘 소스 → recheck | exclude</li>
 * </ul>
 * 우선순위는 위 순서(사용자가 기다리는 항목 먼저), 같은 종류 안에서는 오래 기다린 순이다.
 * 조치는 기존 API로 한다: /registrations/{id}/approve|reject, /api-credentials (set_api_key),
 * /sources/{id}/actions/{action}.
 */
@Service
public class ReviewQueueService {

    public enum Kind { APPROVAL, API_KEY, REVIEW, BROKEN, BLOCKED }

    /** REVIEW 소스 중 이 사유는 API_KEY 종류로 본다 (워커 codes.API_KEY_CODES와 같다). */
    static final Set<String> API_KEY_REASONS = Set.of("API_KEY_REQUIRED", "API_KEY_INVALID");

    private static final Map<Kind, List<String>> SOURCE_ACTIONS = Map.of(
            Kind.API_KEY, List.of("set_api_key", "exclude"),
            Kind.REVIEW, List.of("rediscover", "exclude"),
            Kind.BROKEN, List.of("recheck", "rediscover", "pause", "exclude"),
            Kind.BLOCKED, List.of("recheck", "exclude"));

    /**
     * @param reasonCode   실패 코드 (승인 대기는 null)
     * @param evidencePath 최근 실패의 증거 파일 (워커 SENSING_EVIDENCE_DIR 기준 상대 경로)
     * @param inProgress   이미 대기·실행 중인 작업이 있으면 true (재점검 등을 또 넣을 수 없다)
     * @param provider     API_KEY 항목의 제공자 id (site.risk_flags.api_provider), 그 밖은 null
     */
    public record ReviewItem(Kind kind, Long registrationId, Long sourceId, String domain, String inputUrl,
                             String requestedBy, String reasonCode, String message, String evidencePath,
                             OffsetDateTime waitingSince, boolean inProgress, List<String> actions, String provider) {
    }

    private final JdbcClient jdbc;

    public ReviewQueueService(JdbcClient jdbc) {
        this.jdbc = jdbc;
    }

    public List<ReviewItem> list(CurrentUser user, String kind) {
        user.require(Role.ADMIN);
        Kind filter = parseKind(kind);
        List<ReviewItem> items = new ArrayList<>();
        if (filter == null || filter == Kind.APPROVAL) {
            items.addAll(approvals());
        }
        if (filter != Kind.APPROVAL) {
            items.addAll(sources(filter));
        }
        items.sort(Comparator.comparing(ReviewItem::kind).thenComparing(ReviewItem::waitingSince));
        return items;
    }

    public Map<String, Long> counts(CurrentUser user) {
        user.require(Role.ADMIN);
        Map<String, Long> counts = new LinkedHashMap<>();
        for (Kind k : Kind.values()) {
            counts.put(k.name(), 0L);
        }
        counts.put(Kind.APPROVAL.name(), jdbc.sql(
                "SELECT count(*) FROM registration_request WHERE status = 'AWAITING_APPROVAL'")
                .query(Long.class).single());
        jdbc.sql("""
                SELECT CASE WHEN status = 'REVIEW' AND status_reason IN ('API_KEY_REQUIRED', 'API_KEY_INVALID')
                            THEN 'API_KEY' ELSE status END AS kind, count(*)
                FROM source WHERE status IN ('REVIEW', 'BROKEN', 'BLOCKED') GROUP BY 1
                """)
                .query(rs -> {
                    counts.put(rs.getString(1), rs.getLong(2));
                });
        return counts;
    }

    // ------------------------------------------------------------------

    private List<ReviewItem> approvals() {
        return jdbc.sql("""
                SELECT r.id, r.normalized_domain, r.input_url, u.name, r.result_message, r.created_at
                FROM registration_request r JOIN app_user u ON u.id = r.user_id
                WHERE r.status = 'AWAITING_APPROVAL'
                """)
                .query((rs, n) -> new ReviewItem(Kind.APPROVAL, rs.getLong(1), null, rs.getString(2),
                        rs.getString(3), rs.getString(4), null, rs.getString(5), null,
                        rs.getObject(6, OffsetDateTime.class), false, List.of("approve", "reject"), null))
                .list();
    }

    private List<ReviewItem> sources(Kind filter) {
        String statuses = filter == null ? "'REVIEW', 'BROKEN', 'BLOCKED'"
                : "'" + (filter == Kind.API_KEY ? "REVIEW" : filter.name()) + "'";
        // 최근 실패는 소스 상태를 만든 코드(status_reason)와 같은 것을 우선한다
        List<ReviewItem> items = jdbc.sql("""
                SELECT s.id, s.status, s.status_reason, s.updated_at, t.domain, u.name AS requested_by,
                       t.risk_flags->>'api_provider' AS provider,
                       r.id AS registration_id, r.input_url, e.message, e.evidence_path,
                       EXISTS (SELECT 1 FROM crawl_job j WHERE j.source_id = s.id
                               AND j.status IN ('QUEUED', 'RUNNING')) AS in_progress
                FROM source s
                JOIN site t ON t.id = s.site_id
                LEFT JOIN app_user u ON u.id = s.requested_by
                LEFT JOIN LATERAL (
                    SELECT id, input_url FROM registration_request
                    WHERE source_id = s.id AND status <> 'DUPLICATE' ORDER BY id DESC LIMIT 1) r ON true
                LEFT JOIN LATERAL (
                    SELECT e.message, e.evidence_path FROM crawl_error e JOIN crawl_job j ON j.id = e.job_id
                    WHERE j.source_id = s.id
                    ORDER BY (e.code = s.status_reason) DESC, e.id DESC LIMIT 1) e ON true
                WHERE s.status IN (%s)
                """.formatted(statuses))
                .query((rs, n) -> {
                    Kind kind = kindOf(rs.getString("status"), rs.getString("status_reason"));
                    return new ReviewItem(kind, rs.getObject("registration_id", Long.class),
                            rs.getLong("id"), rs.getString("domain"), rs.getString("input_url"),
                            rs.getString("requested_by"), rs.getString("status_reason"), rs.getString("message"),
                            rs.getString("evidence_path"), rs.getObject("updated_at", OffsetDateTime.class),
                            rs.getBoolean("in_progress"), SOURCE_ACTIONS.get(kind),
                            kind == Kind.API_KEY ? rs.getString("provider") : null);
                })
                .list();
        if (filter == null) {
            return items;
        }
        return items.stream().filter(i -> i.kind() == filter).toList();
    }

    static Kind kindOf(String status, String reason) {
        if ("REVIEW".equals(status) && reason != null && API_KEY_REASONS.contains(reason)) {
            return Kind.API_KEY;
        }
        return Kind.valueOf(status);
    }

    private static Kind parseKind(String kind) {
        if (kind == null || kind.isBlank()) {
            return null;
        }
        try {
            return Kind.valueOf(kind.strip().toUpperCase(Locale.ROOT));
        } catch (IllegalArgumentException e) {
            throw new ApiException(org.springframework.http.HttpStatus.BAD_REQUEST, "BAD_REQUEST",
                    "kind는 APPROVAL, API_KEY, REVIEW, BROKEN, BLOCKED 중 하나입니다.");
        }
    }
}
