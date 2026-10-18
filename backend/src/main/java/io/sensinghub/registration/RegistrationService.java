package io.sensinghub.registration;

import java.time.OffsetDateTime;
import java.util.List;
import java.util.Optional;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import io.sensinghub.audit.AuditLogger;
import io.sensinghub.auth.CurrentUser;
import io.sensinghub.auth.Role;
import io.sensinghub.common.ApiException;
import io.sensinghub.common.UrlNormalizer;

/**
 * 사이트 등록. 사용자는 URL만 넣고, 이후 단계(사전 점검·자동 탐색·품질 검사·첫 수집)는
 * 워커가 ONBOARD 작업으로 자동 처리한다. 화면과 챗봇이 같은 메서드를 쓴다.
 */
@Service
public class RegistrationService {

    private final JdbcClient jdbc;
    private final AuditLogger audit;
    private final boolean viewerNeedsApproval;

    public RegistrationService(JdbcClient jdbc, AuditLogger audit,
                               @Value("${sensing.registration.viewer-needs-approval}") boolean viewerNeedsApproval) {
        this.jdbc = jdbc;
        this.audit = audit;
        this.viewerNeedsApproval = viewerNeedsApproval;
    }

    public enum Channel { WEB, CHAT }

    /**
     * @param batchId 이 요청이 속한 묶음(일괄 등록 {@code registration_request.batch_id}). 링크 모음(포털) 페이지 요청은 묶음에 속하지
     *                않지만 워커가 대신 등록한 언론사들의 묶음 {@code portal:<소스 id>}를 결과 메시지에 적으므로 그 값을 돌려준다
     *                — 화면이 {@code GET /registrations/batches/{batchId}}로 언론사들의 진행을 연다
     */
    public record Registration(long id, String inputUrl, String domain, String channel, String status,
                               String step, String resultMessage, Long sourceId, OffsetDateTime createdAt, String batchId) {
    }

    /** 결과 메시지에 적힌 언론사 묶음 id ("… [묶음 portal:12]"). 워커 jobs.portal_message와 같은 형식 */
    private static final Pattern PORTAL_BATCH = Pattern.compile("portal:\\d+");

    static String batchIdOf(String column, String resultMessage) {
        if (column != null) {
            return column;
        }
        if (resultMessage == null) {
            return null;
        }
        Matcher m = PORTAL_BATCH.matcher(resultMessage);
        return m.find() ? m.group() : null;
    }

    @Transactional
    public Registration register(CurrentUser user, String inputUrl, Channel channel) {
        UrlNormalizer.Normalized url = UrlNormalizer.normalize(inputUrl);

        Optional<ExistingSource> existing = findOpenSource(url.domain());
        if (existing.isPresent()) {
            long id = insertRequest(user, inputUrl, url.domain(), channel, "DUPLICATE",
                    existing.get().status().equals("ACTIVE")
                            ? "이미 수집 중인 사이트입니다."
                            : "이미 등록된 사이트입니다 (상태: " + existing.get().status() + ").",
                    existing.get().id());
            return get(id);
        }

        if (viewerNeedsApproval && !user.role().atLeast(Role.REGISTRAR)) {
            long id = insertRequest(user, inputUrl, url.domain(), channel, "AWAITING_APPROVAL",
                    "등록 요청이 접수되었습니다. 관리자 승인 후 자동으로 진행됩니다.", null);
            audit.log(user.id(), "REGISTRATION_REQUESTED", "registration_request", id, null);
            return get(id);
        }

        long id = insertRequest(user, inputUrl, url.domain(), channel, "RECEIVED", null, null);
        startOnboarding(id, user, url, channel);
        return get(id);
    }

    @Transactional
    public Registration approve(CurrentUser admin, long requestId) {
        admin.require(Role.ADMIN);
        Registration reg = get(requestId);
        if (!reg.status().equals("AWAITING_APPROVAL")) {
            throw ApiException.conflict("승인 대기 상태가 아닙니다.");
        }
        if (reg.sourceId() != null && isWatch(reg.sourceId())) {
            // 키워드 감시(WatchService.create가 소스·설정을 먼저 만들어 둔다): 승인은 ONBOARD 작업을 넣는 것뿐이다
            jdbc.sql("""
                    INSERT INTO crawl_job (source_id, job_type, trigger) VALUES (?, 'ONBOARD', 'REGISTRATION')
                    ON CONFLICT (source_id) WHERE status IN ('QUEUED', 'RUNNING') DO NOTHING
                    """).param(reg.sourceId()).update();
            updateRequest(requestId, "PROCESSING", "QUEUED",
                    "승인되어 키워드 감시를 시작합니다. 네이버 뉴스 검색 API 키를 확인한 뒤 첫 수집을 합니다.", null);
            audit.log(admin.id(), "REGISTRATION_APPROVED", "registration_request", requestId, null);
            return get(requestId);
        }
        UrlNormalizer.Normalized url = UrlNormalizer.normalize(reg.inputUrl());
        if (findOpenSource(url.domain()).isPresent()) {
            updateRequest(requestId, "DUPLICATE", null, "그 사이에 다른 요청으로 등록되었습니다.", null);
            return get(requestId);
        }
        CurrentUser requester = jdbc.sql("""
                SELECT u.id, u.login_id, u.name, u.role FROM registration_request r
                JOIN app_user u ON u.id = r.user_id WHERE r.id = ?
                """)
                .param(requestId)
                .query((rs, n) -> new CurrentUser(rs.getLong(1), rs.getString(2), rs.getString(3),
                        Role.valueOf(rs.getString(4))))
                .single();
        startOnboarding(requestId, requester, url, Channel.valueOf(reg.channel()));
        audit.log(admin.id(), "REGISTRATION_APPROVED", "registration_request", requestId, null);
        return get(requestId);
    }

    /** 승인 대기 요청을 반려한다. 사유는 요청자에게 그대로 보인다. */
    @Transactional
    public Registration reject(CurrentUser admin, long requestId, String reason) {
        admin.require(Role.ADMIN);
        Registration reg = get(requestId);
        if (!reg.status().equals("AWAITING_APPROVAL")) {
            throw ApiException.conflict("승인 대기 상태가 아닙니다.");
        }
        String message = "관리자가 등록 요청을 반려했습니다."
                + (reason == null || reason.isBlank() ? "" : " 사유: " + reason.strip());
        updateRequest(requestId, "REJECTED", "REJECTED", message, null);
        if (reg.sourceId() != null) {
            // 승인 전에 만들어 둔 소스(키워드 감시)는 수집 대상에서 뺀다
            jdbc.sql("UPDATE source SET status = 'EXCLUDED', status_reason = '관리자 반려', updated_at = now() "
                    + "WHERE id = ? AND status = 'PENDING'").param(reg.sourceId()).update();
        }
        audit.log(admin.id(), "REGISTRATION_REJECTED", "registration_request", requestId, null);
        return get(requestId);
    }

    public List<Registration> mine(CurrentUser user) {
        return jdbc.sql(SELECT + " WHERE user_id = ? ORDER BY created_at DESC LIMIT 100")
                .param(user.id())
                .query(this::map)
                .list();
    }

    public List<Registration> awaitingApproval() {
        return jdbc.sql(SELECT + " WHERE status = 'AWAITING_APPROVAL' ORDER BY created_at")
                .query(this::map)
                .list();
    }

    public Registration get(long id) {
        return jdbc.sql(SELECT + " WHERE id = ?").param(id).query(this::map).optional()
                .orElseThrow(() -> ApiException.notFound("등록 요청"));
    }

    // ------------------------------------------------------------------

    private void startOnboarding(long requestId, CurrentUser requester, UrlNormalizer.Normalized url, Channel channel) {
        long siteId = jdbc.sql("""
                INSERT INTO site (domain, base_url) VALUES (?, ?)
                ON CONFLICT (domain) DO UPDATE SET base_url = EXCLUDED.base_url
                RETURNING id
                """)
                .params(url.domain(), url.baseUrl())
                .query(Long.class).single();

        long sourceId = jdbc.sql("""
                INSERT INTO source (site_id, name, status, requested_by)
                VALUES (?, ?, 'PENDING', ?) RETURNING id
                """)
                .params(siteId, url.domain(), requester.id())
                .query(Long.class).single();

        jdbc.sql("""
                INSERT INTO crawl_job (source_id, job_type, trigger)
                VALUES (?, 'ONBOARD', ?)
                """)
                .params(sourceId, channel == Channel.CHAT ? "CHAT" : "REGISTRATION")
                .update();

        updateRequest(requestId, "PROCESSING", "QUEUED", "등록 요청을 접수했어요. 자동으로 점검과 탐색을 진행합니다.", sourceId);
        audit.log(requester.id(), "SOURCE_REGISTERED", "source", sourceId,
                "{\"domain\":\"" + url.domain() + "\",\"channel\":\"" + channel + "\"}");
    }

    private record ExistingSource(long id, String status) {
    }

    private boolean isWatch(long sourceId) {
        return jdbc.sql("SELECT count(*) FROM source WHERE id = ? AND kind = 'WATCH'").param(sourceId)
                .query(Long.class).single() > 0;
    }

    /** 같은 도메인의 사이트 소스. 키워드 감시(kind WATCH)는 제공자 도메인을 공유하므로 중복 판정에서 뺀다. */
    private Optional<ExistingSource> findOpenSource(String domain) {
        return jdbc.sql("""
                SELECT s.id, s.status FROM source s JOIN site t ON t.id = s.site_id
                WHERE t.domain = ? AND s.kind = 'SITE' AND s.status <> 'EXCLUDED'
                ORDER BY s.id LIMIT 1
                """)
                .param(domain)
                .query((rs, n) -> new ExistingSource(rs.getLong(1), rs.getString(2)))
                .optional();
    }

    private long insertRequest(CurrentUser user, String inputUrl, String domain, Channel channel,
                               String status, String message, Long sourceId) {
        return jdbc.sql("""
                INSERT INTO registration_request (user_id, input_url, normalized_domain, channel, status, result_message, source_id)
                VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id
                """)
                .params(user.id(), inputUrl.strip(), domain, channel.name(), status, message, sourceId)
                .query(Long.class).single();
    }

    private void updateRequest(long id, String status, String step, String message, Long sourceId) {
        jdbc.sql("""
                UPDATE registration_request
                SET status = ?, step = ?, result_message = ?, source_id = COALESCE(?, source_id), updated_at = now()
                WHERE id = ?
                """)
                .params(status, step, message, sourceId, id)
                .update();
    }

    static final String SELECT = """
            SELECT id, input_url, normalized_domain, channel, status, step, result_message, source_id, created_at, batch_id
            FROM registration_request""";

    Registration map(java.sql.ResultSet rs, int n) throws java.sql.SQLException {
        String message = rs.getString("result_message");
        return new Registration(rs.getLong("id"), rs.getString("input_url"), rs.getString("normalized_domain"),
                rs.getString("channel"), rs.getString("status"), rs.getString("step"),
                message, rs.getObject("source_id", Long.class),
                rs.getObject("created_at", OffsetDateTime.class), batchIdOf(rs.getString("batch_id"), message));
    }
}
