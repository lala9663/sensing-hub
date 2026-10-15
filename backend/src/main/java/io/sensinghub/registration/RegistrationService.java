package io.sensinghub.registration;

import java.time.OffsetDateTime;
import java.util.List;
import java.util.Optional;

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

    public record Registration(long id, String inputUrl, String domain, String channel, String status,
                               String step, String resultMessage, Long sourceId, OffsetDateTime createdAt) {
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

    private Optional<ExistingSource> findOpenSource(String domain) {
        return jdbc.sql("""
                SELECT s.id, s.status FROM source s JOIN site t ON t.id = s.site_id
                WHERE t.domain = ? AND s.status <> 'EXCLUDED'
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

    private static final String SELECT = """
            SELECT id, input_url, normalized_domain, channel, status, step, result_message, source_id, created_at
            FROM registration_request""";

    private Registration map(java.sql.ResultSet rs, int n) throws java.sql.SQLException {
        return new Registration(rs.getLong("id"), rs.getString("input_url"), rs.getString("normalized_domain"),
                rs.getString("channel"), rs.getString("status"), rs.getString("step"),
                rs.getString("result_message"), rs.getObject("source_id", Long.class),
                rs.getObject("created_at", OffsetDateTime.class));
    }
}
