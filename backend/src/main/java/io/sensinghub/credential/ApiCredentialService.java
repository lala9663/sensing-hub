package io.sensinghub.credential;

import java.time.OffsetDateTime;
import java.util.List;
import java.util.Locale;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpStatus;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import io.sensinghub.audit.AuditLogger;
import io.sensinghub.auth.CurrentUser;
import io.sensinghub.auth.Role;
import io.sensinghub.common.ApiException;
import io.sensinghub.watch.WatchService;

/**
 * 공개 API 자격 증명 (M2). 키는 사람이 발급하고, 여기서는 암호화 보관·검증 결과 표시·재진행만 한다.
 * <ul>
 *   <li>비밀은 pgcrypto {@code pgp_sym_encrypt}로 저장한다. 대칭키는 {@code sensing.secret-key}(환경 변수
 *       {@code SENSING_SECRET_KEY})이며 워커가 같은 키로 푼다. 비어 있으면 이 API는 503이다.</li>
 *   <li>키를 넣으면 그 제공자의 키를 기다리던 소스(REVIEW · API_KEY_REQUIRED/API_KEY_INVALID)에 ONBOARD 작업을
 *       넣어 자동으로 다시 진행시킨다. 검증(시험 호출)은 워커가 하고 결과를 status/last_error에 남긴다.</li>
 *   <li>네이버 뉴스 검색 키(naver_news)를 넣으면 수집 기사 대조 모드 키워드 감시에도 네이버 검색을 붙이고
 *       ({@link WatchService#attachNaver}), 지우면 네이버 모드 감시를 대조 모드로 되돌린다({@link WatchService#detachNaver}).</li>
 *   <li>비밀은 응답·감사 로그·오류 메시지 어디에도 내지 않는다.</li>
 * </ul>
 * 화면·챗봇({@code submit_api_key})이 같은 메서드를 쓴다.
 */
@Service
public class ApiCredentialService {

    public record CredentialView(String provider, String label, String status, OffsetDateTime lastVerifiedAt,
                                 String lastError, OffsetDateTime updatedAt) {
    }

    /**
     * @param resumedSources  이 키를 기다리던 소스 중 ONBOARD 작업을 새로 넣은 수
     * @param attachedWatches 네이버 키일 때 네이버 검색을 새로 붙인 수집 기사 대조 모드 감시 수 (그 밖의 제공자는 0)
     */
    public record SaveResult(CredentialView credential, int resumedSources, int attachedWatches) {
    }

    private final JdbcClient jdbc;
    private final AuditLogger audit;
    private final String secretKey;
    private final WatchService watches;

    public ApiCredentialService(JdbcClient jdbc, AuditLogger audit, @Value("${sensing.secret-key:}") String secretKey,
                                WatchService watches) {
        this.jdbc = jdbc;
        this.audit = audit;
        this.secretKey = secretKey == null ? "" : secretKey;
        this.watches = watches;
    }

    @Transactional
    public SaveResult save(CurrentUser admin, String provider, String secret, String label) {
        admin.require(Role.ADMIN);
        requireSecretKey();
        String id = normalize(provider);
        String name = label == null || label.isBlank() ? null : label.strip();
        jdbc.sql("""
                INSERT INTO api_credential (provider, label, secret_enc, status, created_by)
                VALUES (?, ?, pgp_sym_encrypt(?, ?), 'UNVERIFIED', ?)
                ON CONFLICT (provider) DO UPDATE
                    SET secret_enc = EXCLUDED.secret_enc, label = COALESCE(EXCLUDED.label, api_credential.label),
                        status = 'UNVERIFIED', last_error = NULL, last_verified_at = NULL, updated_at = now()
                """)
                .params(id, name, secret.strip(), secretKey, admin.id())
                .update();

        // 이 키를 기다리던 소스를 다시 진행시킨다 (소스당 열린 작업 1개 규칙은 부분 유니크 인덱스가 지킨다)
        int resumed = jdbc.sql("""
                INSERT INTO crawl_job (source_id, job_type, trigger)
                SELECT s.id, 'ONBOARD', 'MANUAL'
                FROM source s JOIN site t ON t.id = s.site_id
                WHERE s.status = 'REVIEW' AND s.status_reason IN ('API_KEY_REQUIRED', 'API_KEY_INVALID')
                  AND t.risk_flags->>'api_provider' = ?
                ON CONFLICT (source_id) WHERE status IN ('QUEUED', 'RUNNING') DO NOTHING
                """)
                .param(id)
                .update();
        if (resumed > 0) {
            jdbc.sql("""
                    UPDATE registration_request r SET status = 'PROCESSING', step = 'QUEUED', updated_at = now(),
                        result_message = 'API 키가 등록되어 자동으로 다시 진행합니다.'
                    FROM source s JOIN site t ON t.id = s.site_id
                    WHERE r.source_id = s.id AND r.status = 'REVIEW'
                      AND s.status = 'REVIEW' AND s.status_reason IN ('API_KEY_REQUIRED', 'API_KEY_INVALID')
                      AND t.risk_flags->>'api_provider' = ?
                    """)
                    .param(id)
                    .update();
        }
        // 네이버 키면 수집 기사 대조 모드로 돌던 키워드 감시에도 네이버 검색을 붙인다 (대조는 그대로 계속된다).
        // 위의 재개는 REVIEW 소스만, 이것은 ACTIVE·PAUSED 대조 모드 감시만 고르므로 겹치지 않는다
        int attached = WatchService.PROVIDER.equals(id) ? watches.attachNaver(admin) : 0;
        audit.log(admin.id(), "API_CREDENTIAL_SAVED", "api_credential", null,
                "{\"provider\":\"" + id + "\",\"resumedSources\":" + resumed + ",\"attachedWatches\":" + attached + "}");
        return new SaveResult(get(id), resumed, attached);
    }

    public List<CredentialView> list(CurrentUser admin) {
        admin.require(Role.ADMIN);
        requireSecretKey();
        return jdbc.sql(SELECT + " ORDER BY provider").query(this::map).list();
    }

    @Transactional
    public void delete(CurrentUser admin, String provider) {
        admin.require(Role.ADMIN);
        requireSecretKey();
        String id = normalize(provider);
        int deleted = jdbc.sql("DELETE FROM api_credential WHERE provider = ?").param(id).update();
        if (deleted == 0) {
            throw ApiException.notFound("자격 증명");
        }
        // 네이버 키를 지우면 네이버 모드 감시를 수집 기사 대조 모드로 되돌린다 (키 대기 큐에 쌓이지 않게)
        int detached = WatchService.PROVIDER.equals(id) ? watches.detachNaver(admin) : 0;
        audit.log(admin.id(), "API_CREDENTIAL_DELETED", "api_credential", null,
                "{\"provider\":\"" + id + "\",\"detachedWatches\":" + detached + "}");
    }

    // ------------------------------------------------------------------

    private void requireSecretKey() {
        if (secretKey.isBlank()) {
            throw new ApiException(HttpStatus.SERVICE_UNAVAILABLE, "NOT_CONFIGURED",
                    "SENSING_SECRET_KEY가 설정되지 않았습니다.");
        }
    }

    private static String normalize(String provider) {
        return provider.strip().toLowerCase(Locale.ROOT);
    }

    private CredentialView get(String provider) {
        return jdbc.sql(SELECT + " WHERE provider = ?").param(provider).query(this::map).single();
    }

    private static final String SELECT = """
            SELECT provider, label, status, last_verified_at, last_error, updated_at FROM api_credential""";

    private CredentialView map(java.sql.ResultSet rs, int n) throws java.sql.SQLException {
        return new CredentialView(rs.getString("provider"), rs.getString("label"), rs.getString("status"),
                rs.getObject("last_verified_at", OffsetDateTime.class), rs.getString("last_error"),
                rs.getObject("updated_at", OffsetDateTime.class));
    }
}
