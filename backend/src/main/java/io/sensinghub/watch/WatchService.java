package io.sensinghub.watch;

import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpStatus;
import org.springframework.jdbc.core.RowMapper;
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
import io.sensinghub.naver.NaverSettings;
import io.sensinghub.registration.RegistrationService;

/**
 * 키워드 감시 (M2). 현업은 사이트가 아니라 키워드(자사·경쟁사·이슈) 단위로 보도를 본다.
 * <ul>
 *   <li>감시 하나는 {@code source} 한 행({@code kind = WATCH}, {@code watch_name})이고, 키워드는 설정 버전의
 *       config({@code {"method":"API","provider":"naver_news","naver":true|false,"keywords":[...],"exclude":[...]}})에 둔다.
 *       site는 제공자 도메인 한 행(news.naver.com)을 여러 감시가 공유한다.</li>
 *   <li>기본 동작은 <b>수집 기사 대조</b>다: 워커가 등록된 언론사에서 긁어 온 기사마다 모든 감시의 키워드를 대조해
 *       document_watch로 남긴다(워커 watchmatch). 이 모드({@code naver: false})의 감시는 ACTIVE지만
 *       {@code next_run_at}이 NULL이라 스케줄러가 작업을 넣지 않는다.</li>
 *   <li>네이버 뉴스 검색은 사용 방식({@link NaverSettings}, SENSING_NAVER_MODE)에 따른다. 기본 {@code display}(표시 전용)에서는
 *       감시에 네이버 저장 수집을 <b>붙이지 않는다</b> — 개정 약관(2026-09-07)이 검색 결과의 저장·AI 입력을 금지해, 네이버 결과는
 *       감시 화면의 "네이버 뉴스 실시간"(io.sensinghub.naver)으로 보기만 한다. 감시는 키가 있어도 수집 기사 대조 모드다.</li>
 *   <li>{@code store}(법무 확인 뒤 켜는 저장·분석)이고 NAVER API HUB 키(api_credential naver_news, INVALID 아님)가 있으면
 *       {@code naver: true}로 네이버 검색도 함께 붙인다: ONBOARD → 주기 수집. 키가 나중에 들어오면 {@link #attachNaver}가 대조 모드
 *       감시에 네이버 검색을 붙이고, 키를 지우면 {@link #detachNaver}가 대조 모드로 되돌린다. config에 {@code naver}가 없는
 *       감시(V13 이전에 만든 것)는 네이버 모드로 본다. 화면의 {@link WatchView#naver()}는 store일 때만 true다(워커도 store일 때만
 *       네이버를 부른다).</li>
 *   <li>등록 규칙은 사이트 등록과 같다: REGISTRAR 이상은 바로 시작하고, VIEWER는 승인 대기
 *       ({@code registration_request} AWAITING_APPROVAL, 승인은 /registrations/{id}/approve → {@link #start}).</li>
 *   <li>진행 상태는 registration_request 한 행({@code input_url = "watch:<이름>"})으로 /registrations/mine에서 본다.</li>
 *   <li>키워드 수정은 설정 새 버전 활성화다 (이전 버전은 RETIRED로 남아 롤백할 수 있다). 모드는 그대로 이어진다.</li>
 *   <li>보는 범위: 관리자(ADMIN 역할 사용자)가 만든 감시는 <b>공용</b>이라 누구나 본다(목록·키워드별 기사·네이버 실시간). 그 밖의 감시는
 *       만든 사람과 ADMIN만 본다. 키워드 수정은 ADMIN 또는 만든 사람(그래서 공용 감시는 ADMIN만), 제외·다시 켜기는 ADMIN만이다.
 *       목록은 제외(EXCLUDED)된 감시를 빼고, ADMIN은 {@code include_excluded}로 함께 볼 수 있다.</li>
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
    /** 수집 기사 대조 모드의 안내 문구 (등록 요청 결과 메시지). */
    public static final String MATCH_MESSAGE = "등록된 언론사에서 수집한 기사에 키워드를 대조합니다.";

    /**
     * @param naver 네이버 뉴스 검색으로 저장 수집도 하는지 (store 모드에서만 true, false면 수집 기사 대조만)
     * @param shared 공용 감시인가 (만든 사람이 ADMIN 역할: 모든 사용자가 읽기 전용으로 본다)
     * @param mine 보는 사람이 만든 감시인가
     * @param editable 보는 사람이 키워드·이름을 고칠 수 있는가 (ADMIN 또는 만든 사람)
     */
    public record WatchView(long id, String name, List<String> keywords, List<String> exclude, boolean naver,
                            String status, String statusReason, Integer configVersion, int intervalMinutes,
                            OffsetDateTime lastSuccessAt, OffsetDateTime nextRunAt, long documentCount,
                            String requestedBy, Long registrationId, String registrationStatus,
                            String registrationMessage, OffsetDateTime createdAt,
                            boolean shared, boolean mine, boolean editable) {
    }

    private final JdbcClient jdbc;
    private final AuditLogger audit;
    private final ObjectMapper json;
    private final boolean viewerNeedsApproval;
    private final NaverSettings naverSettings;

    public WatchService(JdbcClient jdbc, AuditLogger audit, ObjectMapper json,
                        @Value("${sensing.registration.viewer-needs-approval}") boolean viewerNeedsApproval,
                        NaverSettings naverSettings) {
        this.jdbc = jdbc;
        this.audit = audit;
        this.json = json;
        this.viewerNeedsApproval = viewerNeedsApproval;
        this.naverSettings = naverSettings;
    }

    @Transactional
    public WatchView create(CurrentUser user, String name, List<String> keywords, List<String> exclude) {
        return create(user, name, keywords, exclude, RegistrationService.Channel.WEB);
    }

    /** 화면(WEB)과 챗봇(CHAT)이 같은 규칙으로 만든다. 채널은 등록 요청(registration_request.channel)에 남는다. */
    @Transactional
    public WatchView create(CurrentUser user, String name, List<String> keywords, List<String> exclude,
                            RegistrationService.Channel channel) {
        return create(user, name, keywords, exclude, DEFAULT_INTERVAL_MINUTES,
                channel == RegistrationService.Channel.CHAT ? "키워드 감시 생성 (챗봇)" : "키워드 감시 생성", channel);
    }

    /**
     * 회사(거래처) 마스터의 감시 (M3): 이름 "회사:&lt;회사명&gt;", 키워드는 회사 이름·별칭, 주기는 호출자가 정한다(360분).
     * 등록 규칙(REGISTRAR 이상 즉시 진행, 키 없으면 API_KEY_REQUIRED 큐)은 일반 감시와 같다.
     */
    @Transactional
    public WatchView createForCompany(CurrentUser user, String companyName, List<String> keywords, int intervalMinutes) {
        return create(user, COMPANY_PREFIX + companyName, keywords, List.of(), intervalMinutes, "회사 감시 생성",
                RegistrationService.Channel.WEB);
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
        boolean naver = isNaver(sourceId);
        int version = activateVersion(sourceId, user, terms, List.of(), naver, "회사 이름·별칭 수정");
        pullNextRun(sourceId, naver);
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
                             int intervalMinutes, String note, RegistrationService.Channel channel) {
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
        boolean naver = naverStoreAvailable();
        activateVersion(sourceId, user, terms, excluded, naver, note);

        boolean needsApproval = viewerNeedsApproval && !user.role().atLeast(Role.REGISTRAR);
        long requestId = jdbc.sql("""
                INSERT INTO registration_request (user_id, input_url, normalized_domain, channel, status, step,
                                                  result_message, source_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING id
                """)
                .params(user.id(), "watch:" + watchName, DOMAIN, channel.name(),
                        needsApproval ? "AWAITING_APPROVAL" : "RECEIVED", null,
                        needsApproval ? "키워드 감시 요청이 접수되었습니다. 관리자 승인 후 자동으로 진행됩니다." : null,
                        sourceId)
                .query(Long.class).single();
        if (!needsApproval) {
            start(user, sourceId, requestId, false);
        }
        audit.log(user.id(), needsApproval ? "WATCH_REQUESTED" : "WATCH_CREATED", "source", sourceId,
                "{\"name\":" + quote(watchName) + ",\"keywords\":" + terms.size() + ",\"naver\":" + naver
                        + ",\"registrationId\":" + requestId + "}");
        return get(user, sourceId);
    }

    /**
     * 감시를 시작한다 (REGISTRAR 이상의 생성, VIEWER 요청의 관리자 승인). 지금 네이버 저장 수집을 붙일 수 있는지(store 모드 +
     * 키, {@link #naverStoreAvailable})로 모드를 정하고, 설정의 표시가
     * 다르면(승인을 기다리는 사이 키가 들어오거나 빠졌으면, 또는 V13 이전 요청이면) 같은 키워드로 설정 새 버전을 만든다.
     * <ul>
     *   <li>네이버 모드: ONBOARD 작업 → 워커가 키 확인·첫 검색 후 ACTIVE (요청은 PROCESSING).</li>
     *   <li>대조 모드: 바로 ACTIVE(next_run_at NULL, 스케줄러가 잡지 않는다), 요청은 COMPLETED/DONE.</li>
     * </ul>
     * @return 네이버 모드인지
     */
    @Transactional
    public boolean start(CurrentUser user, long sourceId, long requestId, boolean approved) {
        return begin(user, sourceId, requestId, approved ? "승인되어 키워드 감시를 시작합니다. " : "키워드 감시를 시작합니다. ",
                "REGISTRATION");
    }

    /**
     * 제외(EXCLUDED)한 감시를 다시 켠다 (ADMIN). 시작({@link #start})과 같은 규칙으로 모드를 맞춘다: 대조 모드면 ACTIVE + next_run_at NULL,
     * 네이버 모드(store + 키)면 ACTIVE로 두고 ONBOARD(MANUAL)를 넣어 워커가 키를 확인하고 첫 검색을 한다({@link #attachNaver}와 같다).
     * 같은 이름의 감시가 이미 켜져 있거나(회사를 다시 가져와 새 감시가 생긴 경우 등), 보관(ARCHIVED)한 회사의 감시면 409.
     * 마지막 등록 요청은 결과 메시지를 새로 적는다(승인 대기였다면 관리자가 다시 켠 것이 곧 승인이다).
     */
    @Transactional
    public WatchView reactivate(CurrentUser user, long id) {
        user.require(Role.ADMIN);
        WatchView w = get(user, id);
        if (!"EXCLUDED".equals(w.status())) {
            throw ApiException.conflict("제외된 감시만 다시 켤 수 있습니다 (지금 상태: " + w.status() + ").");
        }
        Optional<Long> other = findOpenWatch(w.name());
        if (other.isPresent() && other.get() != id) {
            throw ApiException.conflict("같은 이름의 감시가 이미 켜져 있습니다: " + w.name() + " (#" + other.get() + ")");
        }
        Optional<String> archivedCompany = jdbc.sql("SELECT name FROM company WHERE watch_source_id = ? AND status = 'ARCHIVED'")
                .param(id).query(String.class).optional();
        if (archivedCompany.isPresent()) {
            throw ApiException.conflict("보관한 회사(" + archivedCompany.get() + ")의 감시입니다. 회사를 다시 가져오면 감시도 새로 만듭니다.");
        }
        jdbc.sql("""
                UPDATE source SET status = 'ACTIVE', status_reason = NULL, next_run_at = NULL, consecutive_failures = 0,
                       updated_at = now()
                WHERE id = ?
                """).param(id).update();
        Long requestId = jdbc.sql("SELECT id FROM registration_request WHERE source_id = ? ORDER BY id DESC LIMIT 1").param(id)
                .query(Long.class).optional().orElse(null);
        boolean naver = begin(user, id, requestId, "관리자가 감시를 다시 켰습니다. ", "MANUAL");
        audit.log(user.id(), "WATCH_REACTIVATED", "source", id,
                "{\"name\":" + quote(w.name()) + ",\"naver\":" + naver + "}");
        return get(user, id);
    }

    /**
     * 감시를 지금 모드로 시작한다 ({@link #start}, {@link #reactivate}). 네이버 모드면 ONBOARD를 넣고(소스 상태는 그대로 — 새 감시는
     * PENDING, 다시 켠 감시는 ACTIVE), 대조 모드면 ACTIVE + next_run_at NULL. requestId가 있으면 그 등록 요청에 결과를 적는다.
     */
    private boolean begin(CurrentUser user, long sourceId, Long requestId, String lead, String trigger) {
        boolean naver = naverStoreAvailable();
        String config = activeConfig(sourceId);
        if (config == null || !parse(config).containsKey("naver") || naverOf(config) != naver) {
            activateVersion(sourceId, user, terms(config, "keywords"), terms(config, "exclude"), naver,
                    naver ? "네이버 뉴스 검색 키가 있어 네이버 검색 함께"
                            : naverSettings.storeEnabled() ? "네이버 키 없음 — 수집 기사 대조" : "네이버 표시 전용 — 수집 기사 대조");
        }
        if (naver) {
            jdbc.sql("""
                    INSERT INTO crawl_job (source_id, job_type, trigger) VALUES (?, 'ONBOARD', ?)
                    ON CONFLICT (source_id) WHERE status IN ('QUEUED', 'RUNNING') DO NOTHING
                    """).params(sourceId, trigger).update();
            updateRequest(requestId, "PROCESSING", "QUEUED",
                    lead + MATCH_MESSAGE + " 네이버 뉴스 검색 API 키를 확인한 뒤 네이버 검색도 함께 첫 수집을 합니다.");
        } else {
            jdbc.sql("""
                    UPDATE source SET status = 'ACTIVE', status_reason = NULL, next_run_at = NULL, consecutive_failures = 0,
                           updated_at = now()
                    WHERE id = ?
                    """).param(sourceId).update();
            updateRequest(requestId, "COMPLETED", "DONE", lead + MATCH_MESSAGE);
        }
        return naver;
    }

    /**
     * 감시에 네이버 저장 수집을 붙일 수 있는가: 사용 방식이 store(법무 확인 뒤 켜는 저장·분석)이고 네이버 키가 있다
     * (워커가 INVALID로 표시한 키는 없는 것으로 본다). 기본 표시 전용(display)·off에서는 늘 false다.
     */
    public boolean naverStoreAvailable() {
        return naverSettings.storeEnabled()
                && jdbc.sql("SELECT count(*) FROM api_credential WHERE provider = ? AND status <> 'INVALID'")
                .param(PROVIDER).query(Long.class).single() > 0;
    }

    /** 네이버 사용 방식이 저장·분석(store)인가. 아니면(표시 전용·off) 감시의 네이버 표시는 화면에서 false다. */
    public boolean naverStoreMode() {
        return naverSettings.storeEnabled();
    }

    /**
     * 네이버 키가 들어왔다: 대조 모드 감시에 네이버 검색을 붙인다 (설정 새 버전 naver true). 수집 중(ACTIVE)이면 ONBOARD를 넣어
     * 워커가 키를 확인하고 첫 검색을 하게 하고, 일시정지(PAUSED)면 설정만 바꾼다 (재개하면 네이버로 수집한다).
     * 승인 대기(PENDING) 감시는 승인 때({@link #start}) 정한다. 수집 기사 대조는 모드와 관계없이 계속된다.
     * 사용 방식이 store가 아니면(기본 표시 전용) 아무것도 붙이지 않는다 — 네이버 결과는 감시 화면에서 실시간으로 보기만 한다.
     * @return 네이버 검색을 붙인 감시 수
     */
    @Transactional
    public int attachNaver(CurrentUser admin) {
        if (!naverSettings.storeEnabled()) {
            return 0;
        }
        List<Long> ids = watchesInMode("s.status IN ('ACTIVE', 'PAUSED')", false);
        for (long id : ids) {
            String config = activeConfig(id);
            activateVersion(id, admin, terms(config, "keywords"), terms(config, "exclude"), true,
                    "네이버 뉴스 검색 API 키 등록 — 네이버 검색 함께");
            int queued = jdbc.sql("""
                    INSERT INTO crawl_job (source_id, job_type, trigger)
                    SELECT id, 'ONBOARD', 'MANUAL' FROM source WHERE id = ? AND status = 'ACTIVE'
                    ON CONFLICT (source_id) WHERE status IN ('QUEUED', 'RUNNING') DO NOTHING
                    """).param(id).update();
            if (queued > 0) {
                jdbc.sql("""
                        UPDATE registration_request SET status = 'PROCESSING', step = 'QUEUED', updated_at = now(),
                            result_message = '네이버 뉴스 검색 API 키가 등록되어 수집 기사 대조에 네이버 검색을 함께 붙입니다.'
                        WHERE id = (SELECT max(id) FROM registration_request WHERE source_id = ?) AND status = 'COMPLETED'
                        """).param(id).update();
            }
        }
        return ids.size();
    }

    /**
     * 네이버 키를 지웠다: 네이버 모드 감시(키를 기다리며 멈춘 것 포함)를 수집 기사 대조 모드로 되돌린다.
     * 수집 중·키 대기(REVIEW API_KEY_*)는 ACTIVE(next_run_at NULL)로, 일시정지·대기(PENDING)는 설정만 바꾼다
     * (대기 중인 ONBOARD는 워커가 대조 모드로 끝낸다, 승인 대기는 승인 때 다시 정한다). 열린 등록 요청은 완료로 닫는다.
     * @return 대조 모드로 바꾼 감시 수
     */
    @Transactional
    public int detachNaver(CurrentUser admin) {
        List<Long> ids = watchesInMode("(s.status IN ('ACTIVE', 'PAUSED', 'PENDING') OR (s.status = 'REVIEW' "
                + "AND s.status_reason IN ('API_KEY_REQUIRED', 'API_KEY_INVALID')))", true);
        for (long id : ids) {
            String config = activeConfig(id);
            activateVersion(id, admin, terms(config, "keywords"), terms(config, "exclude"), false,
                    "네이버 뉴스 검색 API 키 삭제 — 수집 기사 대조");
            jdbc.sql("""
                    UPDATE source SET status = 'ACTIVE', status_reason = NULL, next_run_at = NULL, consecutive_failures = 0,
                           updated_at = now()
                    WHERE id = ? AND status IN ('ACTIVE', 'REVIEW')
                    """).param(id).update();
            jdbc.sql("""
                    UPDATE registration_request SET status = 'COMPLETED', step = 'DONE', result_message = ?, updated_at = now()
                    WHERE source_id = ? AND status IN ('RECEIVED', 'PROCESSING', 'REVIEW')
                    """).params("네이버 뉴스 검색 API 키가 없어 " + MATCH_MESSAGE, id).update();
        }
        return ids.size();
    }

    /** 활성 설정의 네이버 모드가 naver와 같은 감시 중 상태 조건에 맞는 것 (naver 표시가 없으면 네이버 모드로 본다). */
    private List<Long> watchesInMode(String statusCondition, boolean naver) {
        return jdbc.sql("""
                SELECT s.id FROM source s JOIN source_config_version v ON v.id = s.active_config_version_id
                WHERE s.kind = 'WATCH' AND %s AND COALESCE((v.config->>'naver')::boolean, true) = ?
                ORDER BY s.id
                """.formatted(statusCondition)).param(naver).query(Long.class).list();
    }

    /**
     * 네이버 모드로 감시 중이면 다음 틱에 새 키워드로 바로 검색하게 한다. 대조 모드는 next_run_at을 NULL로 둔다.
     * 표시 전용(store가 아님)에서는 설정이 네이버 모드여도 워커가 수집하지 않으므로 당기지 않는다.
     */
    private void pullNextRun(long sourceId, boolean naver) {
        if (naver && naverSettings.storeEnabled()) {
            jdbc.sql("UPDATE source SET next_run_at = now(), updated_at = now() WHERE id = ? AND status = 'ACTIVE'")
                    .param(sourceId).update();
        }
    }

    private void updateRequest(Long requestId, String status, String step, String message) {
        if (requestId == null) {
            return;
        }
        jdbc.sql("UPDATE registration_request SET status = ?, step = ?, result_message = ?, updated_at = now() WHERE id = ?")
                .params(status, step, message, requestId).update();
    }

    /**
     * 키워드 수정 = 설정 새 버전 활성화 (모드는 그대로). 네이버 모드로 감시 중(ACTIVE)이면 다음 틱에 새 키워드로 바로 검색하게
     * next_run_at을 당긴다. 대조 모드는 워커가 다음 수집 작업부터 새 키워드로 대조하므로 당길 것이 없다.
     */
    @Transactional
    public WatchView update(CurrentUser user, long id, String name, List<String> keywords, List<String> exclude) {
        WatchView current = get(user, id);
        if (!current.editable()) {
            throw new ApiException(HttpStatus.FORBIDDEN, "FORBIDDEN", "공용 감시는 관리자만 고칠 수 있습니다.");
        }
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
        boolean naver = isNaver(id);
        int version = activateVersion(id, user, terms, excluded, naver, "키워드 수정");
        pullNextRun(id, naver);
        audit.log(user.id(), "WATCH_UPDATED", "source", id,
                "{\"version\":" + version + ",\"keywords\":" + terms.size() + "}");
        return get(user, id);
    }

    /** 켜져 있는(제외하지 않은) 감시: 공용 + 내 감시, ADMIN은 전체. */
    public List<WatchView> list(CurrentUser user) {
        return list(user, false);
    }

    /**
     * 보이는 감시: 공용(ADMIN이 만든 것) 먼저 만든 순서로, 그다음 내 감시(ADMIN에게는 다른 사용자의 감시). 제외(EXCLUDED)된 감시는
     * 빼고, includeExcluded는 ADMIN에게만 듣는다(다시 켜기를 위한 "제외된 감시 보기").
     */
    public List<WatchView> list(CurrentUser user, boolean includeExcluded) {
        boolean admin = user.role().atLeast(Role.ADMIN);
        return jdbc.sql(SELECT + """
                 WHERE s.kind = 'WATCH' AND (CAST(? AS boolean) OR s.requested_by = ? OR u.role = 'ADMIN')
                   AND (CAST(? AS boolean) OR s.status <> 'EXCLUDED')
                 ORDER BY (u.role = 'ADMIN') IS TRUE DESC, s.id
                """).params(admin, user.id(), admin && includeExcluded).query(mapper(user)).list();
    }

    /** 공용 감시는 누구나, 그 밖의 감시는 만든 사람과 ADMIN만 (아니면 404). 제외된 감시도 볼 수 있다(문서 필터·다시 켜기). */
    public WatchView get(CurrentUser user, long id) {
        WatchView w = jdbc.sql(SELECT + " WHERE s.id = ? AND s.kind = 'WATCH'").param(id).query(mapper(user)).optional()
                .orElseThrow(() -> ApiException.notFound("키워드 감시"));
        if (!user.role().atLeast(Role.ADMIN) && !w.mine() && !w.shared()) {
            throw ApiException.notFound("키워드 감시");
        }
        return w;
    }

    // ------------------------------------------------------------------

    private Optional<Long> findOpenWatch(String watchName) {
        return jdbc.sql("""
                SELECT id FROM source WHERE kind = 'WATCH' AND lower(watch_name) = lower(?) AND status <> 'EXCLUDED'
                ORDER BY id LIMIT 1
                """)
                .param(watchName).query(Long.class).optional();
    }

    /** 새 설정 버전을 ACTIVE로 만들고 소스가 가리키게 한다 (이전 ACTIVE는 RETIRED). 버전 번호를 돌려준다. */
    private int activateVersion(long sourceId, CurrentUser user, List<String> keywords, List<String> exclude,
                                boolean naver, String note) {
        Map<String, Object> config = new LinkedHashMap<>();
        config.put("method", "API");
        config.put("provider", PROVIDER);
        config.put("naver", naver);
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

    private Map<String, Object> parse(String configJson) {
        if (configJson == null) {
            return Map.of();
        }
        try {
            return json.readValue(configJson, new TypeReference<Map<String, Object>>() { });
        } catch (JsonProcessingException e) {
            return Map.of();
        }
    }

    private List<String> terms(String configJson, String field) {
        if (parse(configJson).get(field) instanceof List<?> list) {
            return list.stream().filter(String.class::isInstance).map(String.class::cast).toList();
        }
        return List.of();
    }

    /** config의 naver 표시. 없으면(V13 이전에 만든 감시) 네이버 모드로 본다. 워커 jobs.naver_enabled와 같은 규칙. */
    private boolean naverOf(String configJson) {
        return !Boolean.FALSE.equals(parse(configJson).get("naver"));
    }

    private boolean isNaver(long sourceId) {
        return naverOf(activeConfig(sourceId));
    }

    private String activeConfig(long sourceId) {
        return jdbc.sql("""
                SELECT v.config::text FROM source s JOIN source_config_version v ON v.id = s.active_config_version_id
                WHERE s.id = ?
                """).param(sourceId).query(String.class).optional().orElse(null);
    }

    private static String quote(String text) {
        return "\"" + text.replace("\\", "\\\\").replace("\"", "\\\"") + "\"";
    }

    private static final String SELECT = """
            SELECT s.id, s.watch_name, s.status, s.status_reason, s.interval_minutes, s.last_success_at, s.next_run_at,
                   s.created_at, v.version AS config_version, v.config::text AS config, u.name AS requested_by,
                   s.requested_by AS requested_by_id, (u.role = 'ADMIN') IS TRUE AS shared,
                   (SELECT count(*) FROM document_watch w WHERE w.source_id = s.id) AS document_count,
                   r.id AS registration_id, r.status AS registration_status, r.result_message AS registration_message
            FROM source s
            LEFT JOIN source_config_version v ON v.id = s.active_config_version_id
            LEFT JOIN app_user u ON u.id = s.requested_by
            LEFT JOIN LATERAL (
                SELECT id, status, result_message FROM registration_request
                WHERE source_id = s.id ORDER BY id DESC LIMIT 1) r ON true
            """;

    /** 보는 사람(user)에 따라 mine·editable이 달라진다. */
    private RowMapper<WatchView> mapper(CurrentUser user) {
        boolean admin = user.role().atLeast(Role.ADMIN);
        return (rs, n) -> {
            String config = rs.getString("config");
            Long owner = rs.getObject("requested_by_id", Long.class);
            boolean mine = owner != null && owner == user.id();
            return new WatchView(rs.getLong("id"), rs.getString("watch_name"), terms(config, "keywords"),
                    terms(config, "exclude"), naverOf(config) && naverSettings.storeEnabled(), rs.getString("status"),
                    rs.getString("status_reason"),
                    (Integer) rs.getObject("config_version"), rs.getInt("interval_minutes"),
                    rs.getObject("last_success_at", OffsetDateTime.class), rs.getObject("next_run_at", OffsetDateTime.class),
                    rs.getLong("document_count"), rs.getString("requested_by"),
                    rs.getObject("registration_id", Long.class), rs.getString("registration_status"),
                    rs.getString("registration_message"), rs.getObject("created_at", OffsetDateTime.class),
                    rs.getBoolean("shared"), mine, admin || mine);
        };
    }
}
