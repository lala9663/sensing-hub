package io.sensinghub.source;

import java.time.OffsetDateTime;
import java.util.List;
import java.util.Map;

import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import io.sensinghub.audit.AuditLogger;
import io.sensinghub.auth.CurrentUser;
import io.sensinghub.auth.Role;
import io.sensinghub.common.ApiException;

/** 관리자 화면: 소스 목록·상세·운영 조치. */
@Service
public class SourceService {

    private final JdbcClient jdbc;
    private final AuditLogger audit;

    public SourceService(JdbcClient jdbc, AuditLogger audit) {
        this.jdbc = jdbc;
        this.audit = audit;
    }

    /**
     * kind: SITE(사이트 수집) | WATCH(키워드 감시, watchName이 감시 이름) | CHECK(회사 단위 점검, M3).
     * intervalLocked: 사람이 PUT으로 주기를 정해 워커의 주기 자동 조정이 건드리지 않는 소스.
     */
    public record SourceSummary(long id, String kind, String watchName, String domain, String baseUrl, String siteType,
                                String method, String status,
                                String statusReason, Integer configVersion, int intervalMinutes, boolean intervalLocked,
                                OffsetDateTime lastSuccessAt, OffsetDateTime nextRunAt, int consecutiveFailures,
                                int consecutiveAnomalies, long documentCount, boolean aiRestricted) {
    }

    /** note: 워커가 실행에 남긴 메모 (예: 주기 자동 조정 60→120분). */
    public record JobRow(long id, String jobType, String trigger, String status, Integer configVersion,
                         int foundCount, int newCount, int errorCount,
                         OffsetDateTime startedAt, OffsetDateTime finishedAt, String note) {
    }

    /** evidencePath: 워커 증거 폴더(SENSING_EVIDENCE_DIR) 기준 상대 경로. 증거가 없으면 null. */
    public record ErrorRow(long jobId, String url, String stage, String code, Integer httpStatus, String message,
                           String evidencePath, OffsetDateTime createdAt) {
    }

    public record ConfigRow(long id, int version, String status, String changeNote, String config,
                            OffsetDateTime createdAt) {
    }

    public record SourceDetail(SourceSummary source, List<JobRow> jobs, List<ErrorRow> recentErrors,
                               List<ConfigRow> configs) {
    }

    private static final String SUMMARY_SQL = """
            SELECT s.id, s.kind, s.watch_name, t.domain, t.base_url, t.site_type, s.method, s.status, s.status_reason,
                   v.version AS config_version, s.interval_minutes, s.interval_locked, s.last_success_at, s.next_run_at,
                   s.consecutive_failures, s.consecutive_anomalies, t.ai_restricted,
                   (SELECT count(*) FROM document d WHERE d.source_id = s.id) AS document_count
            FROM source s
            JOIN site t ON t.id = s.site_id
            LEFT JOIN source_config_version v ON v.id = s.active_config_version_id
            """;

    public List<SourceSummary> list(CurrentUser user, String status) {
        user.require(Role.ADMIN);
        if (status == null || status.isBlank()) {
            return jdbc.sql(SUMMARY_SQL + " ORDER BY s.id").query(this::mapSummary).list();
        }
        return jdbc.sql(SUMMARY_SQL + " WHERE s.status = ? ORDER BY s.id").param(status).query(this::mapSummary).list();
    }

    public Map<String, Long> statusCounts(CurrentUser user) {
        user.require(Role.ADMIN);
        Map<String, Long> counts = new java.util.LinkedHashMap<>();
        jdbc.sql("SELECT status, count(*) FROM source GROUP BY status ORDER BY status")
                .query(rs -> {
                    counts.put(rs.getString(1), rs.getLong(2));
                });
        return counts;
    }

    public SourceDetail detail(CurrentUser user, long id) {
        user.require(Role.ADMIN);
        SourceSummary summary = jdbc.sql(SUMMARY_SQL + " WHERE s.id = ?").param(id).query(this::mapSummary)
                .optional().orElseThrow(() -> ApiException.notFound("소스"));
        List<JobRow> jobs = jdbc.sql("""
                SELECT j.id, j.job_type, j.trigger, j.status, v.version, j.found_count, j.new_count, j.error_count,
                       j.started_at, j.finished_at, j.note
                FROM crawl_job j LEFT JOIN source_config_version v ON v.id = j.config_version_id
                WHERE j.source_id = ? ORDER BY j.id DESC LIMIT 20
                """).param(id).query((rs, n) -> new JobRow(rs.getLong(1), rs.getString(2), rs.getString(3),
                        rs.getString(4), (Integer) rs.getObject(5), rs.getInt(6), rs.getInt(7), rs.getInt(8),
                        rs.getObject(9, OffsetDateTime.class), rs.getObject(10, OffsetDateTime.class),
                        rs.getString(11))).list();
        List<ErrorRow> errors = jdbc.sql("""
                SELECT e.job_id, e.url, e.stage, e.code, e.http_status, e.message, e.evidence_path, e.created_at
                FROM crawl_error e JOIN crawl_job j ON j.id = e.job_id
                WHERE j.source_id = ? ORDER BY e.id DESC LIMIT 20
                """).param(id).query((rs, n) -> new ErrorRow(rs.getLong(1), rs.getString(2), rs.getString(3),
                        rs.getString(4), (Integer) rs.getObject(5), rs.getString(6), rs.getString(7),
                        rs.getObject(8, OffsetDateTime.class))).list();
        List<ConfigRow> configs = jdbc.sql("""
                SELECT id, version, status, change_note, config::text, created_at
                FROM source_config_version WHERE source_id = ? ORDER BY version DESC
                """).param(id).query((rs, n) -> new ConfigRow(rs.getLong(1), rs.getInt(2), rs.getString(3),
                        rs.getString(4), rs.getString(5), rs.getObject(6, OffsetDateTime.class))).list();
        return new SourceDetail(summary, jobs, errors, configs);
    }

    public enum Action { PAUSE, RESUME, RUN, RECHECK, REDISCOVER, EXCLUDE }

    /**
     * 운영 조치. BLOCKED는 자동으로 풀리지 않고, 관리자의 RECHECK(1회 재점검)로만 다시 시도한다.
     */
    @Transactional
    public SourceSummary act(CurrentUser user, long id, Action action) {
        user.require(Role.ADMIN);
        SourceSummary s = detail(user, id).source();
        switch (action) {
            case PAUSE -> {
                requireStatus(s, "ACTIVE", "BROKEN");
                setStatus(id, "PAUSED", "관리자 일시정지");
            }
            case RESUME -> {
                requireStatus(s, "PAUSED");
                jdbc.sql("UPDATE source SET status = 'ACTIVE', status_reason = NULL, next_run_at = now(), updated_at = now() WHERE id = ?")
                        .param(id).update();
            }
            case RUN -> {
                requireStatus(s, "ACTIVE");
                enqueue(id, "CRAWL", "MANUAL");
            }
            case RECHECK -> {
                requireStatus(s, "BLOCKED", "BROKEN");
                enqueue(id, "CRAWL", "MANUAL");
            }
            case REDISCOVER -> {
                requireStatus(s, "BROKEN", "REVIEW", "ACTIVE", "PAUSED");
                enqueue(id, "ONBOARD", "MANUAL");
            }
            case EXCLUDE -> {
                setStatus(id, "EXCLUDED", "관리자 제외");
                // 처리 대기(REVIEW) 중이던 등록 요청은 요청자에게 결과를 알린다
                jdbc.sql("""
                        UPDATE registration_request
                        SET status = 'REJECTED', step = 'REJECTED', updated_at = now(),
                            result_message = '관리자가 확인한 결과 수집 대상에서 제외했습니다.'
                        WHERE source_id = ? AND status IN ('RECEIVED', 'PROCESSING', 'REVIEW')
                        """).param(id).update();
            }
        }
        audit.log(user.id(), "SOURCE_" + action.name(), "source", id, null);
        return detail(user, id).source();
    }

    public static final int MIN_INTERVAL_MINUTES = 5;
    public static final int MAX_INTERVAL_MINUTES = 10080;

    /**
     * 소스 수정 (ADMIN): 수집 주기. 사람이 정한 주기는 interval_locked = true가 되어 워커의 주기 자동 조정이 덮지 않는다.
     * intervalLocked = false를 함께 보내면 값을 바꾸되 자동 조정에 다시 맡긴다. 주기가 바뀌면 다음 실행 시각도 새 주기로 잡는다.
     */
    @Transactional
    public SourceSummary update(CurrentUser user, long id, Integer intervalMinutes, Boolean intervalLocked) {
        user.require(Role.ADMIN);
        SourceSummary s = detail(user, id).source();
        if (intervalMinutes == null && intervalLocked == null) {
            throw new ApiException(org.springframework.http.HttpStatus.BAD_REQUEST, "BAD_REQUEST",
                    "intervalMinutes 또는 intervalLocked를 보내 주세요.");
        }
        if (intervalMinutes != null && (intervalMinutes < MIN_INTERVAL_MINUTES || intervalMinutes > MAX_INTERVAL_MINUTES)) {
            throw new ApiException(org.springframework.http.HttpStatus.BAD_REQUEST, "BAD_REQUEST",
                    "수집 주기는 " + MIN_INTERVAL_MINUTES + "~" + MAX_INTERVAL_MINUTES + "분 사이여야 합니다.");
        }
        boolean locked = intervalLocked != null ? intervalLocked : (intervalMinutes != null || s.intervalLocked());
        int interval = intervalMinutes != null ? intervalMinutes : s.intervalMinutes();
        jdbc.sql("""
                UPDATE source SET interval_minutes = ?, interval_locked = ?,
                       next_run_at = CASE WHEN ? <> interval_minutes AND status = 'ACTIVE'
                                          THEN LEAST(COALESCE(next_run_at, now()), now() + make_interval(mins => ?))
                                          ELSE next_run_at END,
                       updated_at = now()
                WHERE id = ?
                """).params(interval, locked, interval, interval, id).update();
        audit.log(user.id(), "SOURCE_UPDATED", "source", id,
                "{\"intervalMinutes\":" + interval + ",\"intervalLocked\":" + locked + "}");
        return detail(user, id).source();
    }

    /** 설정 버전 활성화(롤백 포함): 포인터만 바꾼다. */
    @Transactional
    public SourceSummary activateConfig(CurrentUser user, long id, int version) {
        user.require(Role.ADMIN);
        Long configId = jdbc.sql("SELECT id FROM source_config_version WHERE source_id = ? AND version = ?")
                .params(id, version).query(Long.class).optional()
                .orElseThrow(() -> ApiException.notFound("설정 버전"));
        jdbc.sql("UPDATE source_config_version SET status = 'RETIRED' WHERE source_id = ? AND status = 'ACTIVE'")
                .param(id).update();
        jdbc.sql("UPDATE source_config_version SET status = 'ACTIVE' WHERE id = ?").param(configId).update();
        jdbc.sql("UPDATE source SET active_config_version_id = ?, updated_at = now() WHERE id = ?")
                .params(configId, id).update();
        audit.log(user.id(), "CONFIG_ACTIVATED", "source", id, "{\"version\":" + version + "}");
        return detail(user, id).source();
    }

    private void enqueue(long sourceId, String jobType, String trigger) {
        // 과거 기사 채우기(BACKFILL) 묶음이 소스의 작업 자리를 잡고 있으면 양보시킨다: 대기 중이면 거둔다
        jdbc.sql("""
                UPDATE crawl_job SET status = 'SUCCESS', finished_at = now(), note = '관리자 작업에 양보'
                WHERE source_id = ? AND job_type = 'BACKFILL' AND status = 'QUEUED'
                """).param(sourceId).update();
        int inserted = jdbc.sql("""
                INSERT INTO crawl_job (source_id, job_type, config_version_id, trigger)
                SELECT id, ?, active_config_version_id, ? FROM source WHERE id = ?
                ON CONFLICT (source_id) WHERE status IN ('QUEUED', 'RUNNING') DO NOTHING
                """).params(jobType, trigger, sourceId).update();
        if (inserted == 0) {
            boolean backfillRunning = jdbc.sql("""
                    SELECT EXISTS (SELECT 1 FROM crawl_job WHERE source_id = ? AND job_type = 'BACKFILL' AND status = 'RUNNING')
                    """).param(sourceId).query(Boolean.class).single();
            if (backfillRunning && "CRAWL".equals(jobType)) {
                // 실행 중인 묶음: 주기 수집 차례를 지금으로 당긴다. 워커가 이것을 보고 묶음을 멈추면 스케줄러가 곧 CRAWL을 넣는다
                jdbc.sql("UPDATE source SET next_run_at = now(), updated_at = now() WHERE id = ?").param(sourceId).update();
                return;
            }
            if (backfillRunning) {
                throw ApiException.conflict("과거 기사 채우기가 실행 중입니다. 소스 상세에서 채우기를 취소한 뒤 다시 시도해 주세요.");
            }
            throw ApiException.conflict("이미 실행 중이거나 대기 중인 작업이 있습니다.");
        }
    }

    private void setStatus(long id, String status, String reason) {
        jdbc.sql("UPDATE source SET status = ?, status_reason = ?, updated_at = now() WHERE id = ?")
                .params(status, reason, id).update();
    }

    private static void requireStatus(SourceSummary s, String... allowed) {
        for (String a : allowed) {
            if (a.equals(s.status())) {
                return;
            }
        }
        throw ApiException.conflict("현재 상태(" + s.status() + ")에서는 할 수 없는 조치입니다.");
    }

    private SourceSummary mapSummary(java.sql.ResultSet rs, int n) throws java.sql.SQLException {
        return new SourceSummary(rs.getLong("id"), rs.getString("kind"), rs.getString("watch_name"),
                rs.getString("domain"), rs.getString("base_url"),
                rs.getString("site_type"), rs.getString("method"), rs.getString("status"),
                rs.getString("status_reason"), (Integer) rs.getObject("config_version"),
                rs.getInt("interval_minutes"), rs.getBoolean("interval_locked"),
                rs.getObject("last_success_at", OffsetDateTime.class),
                rs.getObject("next_run_at", OffsetDateTime.class), rs.getInt("consecutive_failures"),
                rs.getInt("consecutive_anomalies"), rs.getLong("document_count"), rs.getBoolean("ai_restricted"));
    }
}
