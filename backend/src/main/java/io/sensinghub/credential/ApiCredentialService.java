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
import io.sensinghub.naver.NaverSettings;
import io.sensinghub.watch.WatchService;

/**
 * 공개 API 자격 증명 (M2). 키는 사람이 발급하고, 여기서는 암호화 보관·검증 결과 표시·재진행만 한다.
 * <ul>
 *   <li>비밀은 pgcrypto {@code pgp_sym_encrypt}로 저장한다. 대칭키는 {@code sensing.secret-key}(환경 변수
 *       {@code SENSING_SECRET_KEY})이며 워커가 같은 키로 푼다. 비어 있으면 이 API는 503이다.</li>
 *   <li>키를 넣으면 그 제공자의 키를 기다리던 소스(REVIEW · API_KEY_REQUIRED/API_KEY_INVALID)에 ONBOARD 작업을
 *       넣어 자동으로 다시 진행시킨다. 검증(시험 호출)은 워커가 하고 결과를 status/last_error에 남긴다.</li>
 *   <li>네이버 뉴스 검색 키(naver_news, NAVER API HUB "Client ID:Client Secret")는 형식을 확인한다. 기본 표시 전용에서는
 *       감시에 아무것도 붙이지 않고(시험 호출은 {@code POST /api-credentials/naver_news/verify}, io.sensinghub.naver),
 *       저장·분석(store)이면 수집 기사 대조 모드 키워드 감시에도 네이버 검색을 붙인다({@link WatchService#attachNaver}).
 *       지우면 네이버 모드 감시를 대조 모드로 되돌린다({@link WatchService#detachNaver}).</li>
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
     * @param attachedWatches 네이버 키일 때 네이버 검색을 새로 붙인 수집 기사 대조 모드 감시 수 (그 밖의 제공자·표시 전용은 0)
     * @param note            제공자별 안내 (네이버: 사용 방식에 따라 무엇을 하는지), 없으면 null
     */
    public record SaveResult(CredentialView credential, int resumedSources, int attachedWatches, String note) {
    }

    private final JdbcClient jdbc;
    private final AuditLogger audit;
    private final String secretKey;
    private final WatchService watches;
    private final NaverSettings naver;

    public ApiCredentialService(JdbcClient jdbc, AuditLogger audit, @Value("${sensing.secret-key:}") String secretKey,
                                WatchService watches, NaverSettings naver) {
        this.jdbc = jdbc;
        this.audit = audit;
        this.secretKey = secretKey == null ? "" : secretKey;
        this.watches = watches;
        this.naver = naver;
    }

    @Transactional
    public SaveResult save(CurrentUser admin, String provider, String secret, String label) {
        admin.require(Role.ADMIN);
        requireSecretKey();
        String id = normalize(provider);
        String name = label == null || label.isBlank() ? null : label.strip();
        if (WatchService.PROVIDER.equals(id)) {
            requireNaverFormat(secret);
        }
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
        // 위의 재개는 REVIEW 소스만, 이것은 ACTIVE·PAUSED 대조 모드 감시만 고르므로 겹치지 않는다.
        // 기본 표시 전용(display)에서는 붙이지 않는다 (attachNaver가 0) — 결과는 감시 화면의 실시간 보기로만 본다
        int attached = WatchService.PROVIDER.equals(id) ? watches.attachNaver(admin) : 0;
        audit.log(admin.id(), "API_CREDENTIAL_SAVED", "api_credential", null,
                "{\"provider\":\"" + id + "\",\"resumedSources\":" + resumed + ",\"attachedWatches\":" + attached + "}");
        return new SaveResult(get(id), resumed, attached, WatchService.PROVIDER.equals(id) ? naverNote() : null);
    }

    /** 네이버 키는 "Client ID:Client Secret" 한 문자열이다 (화면은 두 칸을 ':'로 이어 보낸다). */
    private static void requireNaverFormat(String secret) {
        String s = secret == null ? "" : secret.strip();
        int colon = s.indexOf(':');
        if (colon <= 0 || s.substring(colon + 1).isBlank() || s.substring(0, colon).isBlank()) {
            throw new ApiException(HttpStatus.BAD_REQUEST, "BAD_REQUEST",
                    "네이버 키는 Client ID와 Client Secret을 ':'로 이은 한 문자열입니다 (Client ID:Client Secret).");
        }
    }

    private String naverNote() {
        return switch (naver.mode()) {
            case DISPLAY -> "네이버는 표시 전용입니다: 검색 결과를 저장·요약하지 않고 키워드 감시 화면의 '네이버 뉴스 실시간'에서만 "
                    + "보여 줍니다. '시험 호출'로 키를 확인하세요.";
            case STORE -> "네이버 저장·분석 모드(store): 키워드 감시에 네이버 검색 수집을 붙입니다. 키는 워커가 첫 수집 때 확인합니다.";
            case OFF -> "네이버 사용이 꺼져 있어(SENSING_NAVER_MODE=off) 키는 저장만 합니다.";
        };
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
