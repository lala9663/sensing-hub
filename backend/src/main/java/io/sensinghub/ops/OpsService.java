package io.sensinghub.ops;

import java.time.OffsetDateTime;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Service;

import io.sensinghub.auth.CurrentUser;
import io.sensinghub.auth.Role;
import io.sensinghub.review.ReviewQueueService;

/**
 * 대량 소스 운영 지표 (M3, ADMIN). 소스가 수십~수백 개가 되면 개별 상세가 아니라 "워커가 모자라는가", "큐가 밀리는가",
 * "조용히 멈춘 소스가 어디인가"를 한 화면에서 봐야 한다. 모두 DB에 이미 있는 것(source·crawl_job·crawl_error와 워커가 남기는
 * 생존 신호 worker_heartbeat)만 집계한다.
 */
@Service
public class OpsService {

    static final List<String> SOURCE_STATUSES = List.of("PENDING", "REVIEW", "ACTIVE", "PAUSED", "BROKEN", "BLOCKED", "EXCLUDED");

    /**
     * @param queued                 대기 중 작업 수
     * @param oldestQueuedWaitSeconds 가장 오래 기다린 대기 작업의 대기 시간 (없으면 null)
     * @param running                실행 중 작업 수
     * @param workers                살아 있는 워커 수: 최근 2분 안에 생존 신호(worker_heartbeat)를 남긴 워커. 큐가 비어 쉬는 워커도 센다
     */
    public record JobQueue(long queued, Long oldestQueuedWaitSeconds, long running, long workers) {
    }

    /**
     * 워커 한 대 (worker_heartbeat). 최근 24시간 안에 신호를 남긴 워커를 마지막 신호가 최근인 순으로 준다.
     * @param alive          최근 2분 안에 신호가 있음 (jobQueue.workers에 센다). 아니면 멈췄거나 DB에 닿지 못하는 워커
     * @param currentJobId   처리 중인 작업 (쉬는 중이면 null). currentJobType·currentSourceId·currentDomain은 그 작업의 유형·소스·사이트
     */
    public record WorkerStatus(String workerId, String host, String version, OffsetDateTime startedAt,
                               OffsetDateTime lastSeenAt, long secondsSinceSeen, boolean alive,
                               Long currentJobId, String currentJobType, Long currentSourceId, String currentDomain) {
    }

    /** 최근 24시간에 끝난 작업: 결과별 건수와 평균 소요 시간(초). */
    public record JobResults24h(long success, long partial, long failed, Double avgDurationSeconds) {
    }

    public record SourceCount(long sourceId, String domain, String name, String kind, long count) {
    }

    /** 최근 24시간 수집량: 새 문서 수(작업의 new_count 합)와 소스 상위 10. */
    public record Documents24h(long newDocuments, List<SourceCount> topSources) {
    }

    public record ErrorCount(String code, long count) {
    }

    /**
     * 처리량 추정.
     * @param scheduledRunsPerHour ACTIVE 소스의 시간당 예정 실행 수 (Σ 60/interval_minutes)
     * @param completedLastHour    최근 1시간에 끝난 작업 수
     * @param demandRatio          예정/실제. 1을 넘으면 워커가 예정된 수집을 다 소화하지 못한다 (실제 완료가 0이면 null)
     * @param workersShort         워커가 모자란다고 보는 판정 (demandRatio > 1, 또는 완료가 없는데 예정 실행과 대기 작업이 있음)
     */
    public record Throughput(double scheduledRunsPerHour, long completedLastHour, Double demandRatio, boolean workersShort) {
    }

    public record Overview(OffsetDateTime generatedAt, Map<String, Long> sources, Map<String, Long> reviewQueue,
                           JobQueue jobQueue, JobResults24h jobs24h, Documents24h documents24h,
                           List<ErrorCount> topErrors24h, Throughput throughput, List<WorkerStatus> workers) {
    }

    /** 이 시간(초) 안에 생존 신호가 있어야 살아 있는 워커로 센다. 워커는 30초마다 신호를 남긴다 (SENSING_HEARTBEAT_SECONDS). */
    static final int WORKER_ALIVE_SECONDS = 120;

    /**
     * 소스 건강 상태 한 줄. "조용히 멈춘 소스"를 찾기 위한 것이라 마지막 성공이 오래된 순(없는 것이 먼저)으로 준다.
     * @param avgFound   최근 10회 끝난 실행의 평균 기사 후보 수 (실행이 없으면 null)
     * @param avgNew     최근 10회 끝난 실행의 평균 새 문서 수
     * @param overdue    ACTIVE인데 마지막 성공이 주기의 3배보다 오래됐거나 한 번도 없음
     */
    public record SourceHealth(long id, String kind, String domain, String name, String status, String statusReason,
                               int intervalMinutes, boolean intervalLocked, OffsetDateTime lastSuccessAt,
                               OffsetDateTime nextRunAt, int consecutiveFailures, int consecutiveAnomalies,
                               Double avgFound, Double avgNew, String lastJobStatus, OffsetDateTime lastJobAt,
                               boolean overdue) {
    }

    private final JdbcClient jdbc;
    private final ReviewQueueService reviewQueue;

    public OpsService(JdbcClient jdbc, ReviewQueueService reviewQueue) {
        this.jdbc = jdbc;
        this.reviewQueue = reviewQueue;
    }

    public Overview overview(CurrentUser user) {
        user.require(Role.ADMIN);
        return new Overview(OffsetDateTime.now(), sourceCounts(), reviewQueue.counts(user), jobQueue(), jobs24h(),
                documents24h(), topErrors24h(), throughput(), workers());
    }

    public List<SourceHealth> sourceHealth(CurrentUser user, String status, int limit) {
        user.require(Role.ADMIN);
        boolean filtered = status != null && !status.isBlank();
        String where = filtered ? "s.status = ?" : "s.status <> 'EXCLUDED'";
        var query = jdbc.sql("""
                SELECT s.id, s.kind, t.domain, s.name, s.status, s.status_reason, s.interval_minutes, s.interval_locked,
                       s.last_success_at, s.next_run_at, s.consecutive_failures, s.consecutive_anomalies,
                       r.avg_found, r.avg_new, l.status AS last_job_status, COALESCE(l.finished_at, l.started_at) AS last_job_at,
                       (s.status = 'ACTIVE' AND (s.last_success_at IS NULL
                            OR s.last_success_at < now() - make_interval(mins => s.interval_minutes * 3))) AS overdue
                FROM source s
                JOIN site t ON t.id = s.site_id
                LEFT JOIN LATERAL (
                    SELECT AVG(found_count) AS avg_found, AVG(new_count) AS avg_new FROM (
                        SELECT found_count, new_count FROM crawl_job
                        WHERE source_id = s.id AND status IN ('SUCCESS', 'PARTIAL', 'FAILED') AND finished_at IS NOT NULL
                        ORDER BY id DESC LIMIT 10) recent) r ON true
                LEFT JOIN LATERAL (
                    SELECT status, started_at, finished_at FROM crawl_job WHERE source_id = s.id ORDER BY id DESC LIMIT 1) l ON true
                WHERE %s
                ORDER BY s.last_success_at ASC NULLS FIRST, s.id
                LIMIT ?
                """.formatted(where));
        if (filtered) {
            query = query.param(status.strip().toUpperCase(java.util.Locale.ROOT));
        }
        return query.param(Math.max(1, Math.min(limit, 1000))).query((rs, n) -> new SourceHealth(
                rs.getLong("id"), rs.getString("kind"), rs.getString("domain"), rs.getString("name"), rs.getString("status"),
                rs.getString("status_reason"), rs.getInt("interval_minutes"), rs.getBoolean("interval_locked"),
                rs.getObject("last_success_at", OffsetDateTime.class), rs.getObject("next_run_at", OffsetDateTime.class),
                rs.getInt("consecutive_failures"), rs.getInt("consecutive_anomalies"),
                dbl(rs, "avg_found"), dbl(rs, "avg_new"),
                rs.getString("last_job_status"), rs.getObject("last_job_at", OffsetDateTime.class),
                rs.getBoolean("overdue"))).list();
    }

    // ------------------------------------------------------------------

    private Map<String, Long> sourceCounts() {
        Map<String, Long> counts = new LinkedHashMap<>();
        for (String s : SOURCE_STATUSES) {
            counts.put(s, 0L);
        }
        jdbc.sql("SELECT status, count(*) FROM source GROUP BY status").query(rs -> {
            counts.put(rs.getString(1), rs.getLong(2));
        });
        return counts;
    }

    private JobQueue jobQueue() {
        long workers = jdbc.sql("SELECT count(*) FROM worker_heartbeat WHERE last_seen_at >= now() - make_interval(secs => ?)")
                .param(WORKER_ALIVE_SECONDS).query(Long.class).single();
        return jdbc.sql("""
                SELECT count(*) FILTER (WHERE status = 'QUEUED') AS queued,
                       EXTRACT(EPOCH FROM now() - min(created_at) FILTER (WHERE status = 'QUEUED'))::bigint AS oldest_wait,
                       count(*) FILTER (WHERE status = 'RUNNING') AS running
                FROM crawl_job WHERE status IN ('QUEUED', 'RUNNING')
                """).query((rs, n) -> new JobQueue(rs.getLong("queued"), rs.getObject("oldest_wait", Long.class),
                        rs.getLong("running"), workers)).single();
    }

    private List<WorkerStatus> workers() {
        return jdbc.sql("""
                SELECT w.worker_id, w.host, w.version, w.started_at, w.last_seen_at,
                       EXTRACT(EPOCH FROM now() - w.last_seen_at)::bigint AS seconds_since_seen,
                       w.last_seen_at >= now() - make_interval(secs => ?) AS alive,
                       w.current_job_id, j.job_type, j.source_id, t.domain
                FROM worker_heartbeat w
                LEFT JOIN crawl_job j ON j.id = w.current_job_id
                LEFT JOIN source s ON s.id = j.source_id
                LEFT JOIN site t ON t.id = s.site_id
                WHERE w.last_seen_at >= now() - interval '24 hours'
                ORDER BY w.last_seen_at DESC, w.worker_id
                LIMIT 100
                """).param(WORKER_ALIVE_SECONDS).query((rs, n) -> new WorkerStatus(
                        rs.getString("worker_id"), rs.getString("host"), rs.getString("version"),
                        rs.getObject("started_at", OffsetDateTime.class), rs.getObject("last_seen_at", OffsetDateTime.class),
                        Math.max(0, rs.getLong("seconds_since_seen")), rs.getBoolean("alive"),
                        rs.getObject("current_job_id", Long.class), rs.getString("job_type"),
                        rs.getObject("source_id", Long.class), rs.getString("domain")))
                .list();
    }

    private JobResults24h jobs24h() {
        return jdbc.sql("""
                SELECT count(*) FILTER (WHERE status = 'SUCCESS') AS success,
                       count(*) FILTER (WHERE status = 'PARTIAL') AS partial,
                       count(*) FILTER (WHERE status = 'FAILED') AS failed,
                       AVG(EXTRACT(EPOCH FROM finished_at - started_at)) FILTER (WHERE started_at IS NOT NULL) AS avg_seconds
                FROM crawl_job WHERE finished_at >= now() - interval '24 hours'
                """).query((rs, n) -> new JobResults24h(rs.getLong("success"), rs.getLong("partial"), rs.getLong("failed"),
                        dbl(rs, "avg_seconds"))).single();
    }

    private Documents24h documents24h() {
        long total = jdbc.sql("SELECT COALESCE(sum(new_count), 0) FROM crawl_job WHERE finished_at >= now() - interval '24 hours'")
                .query(Long.class).single();
        List<SourceCount> top = jdbc.sql("""
                SELECT s.id, t.domain, s.name, s.kind, sum(j.new_count) AS c
                FROM crawl_job j JOIN source s ON s.id = j.source_id JOIN site t ON t.id = s.site_id
                WHERE j.finished_at >= now() - interval '24 hours' AND j.new_count > 0
                GROUP BY s.id, t.domain, s.name, s.kind ORDER BY c DESC, s.id LIMIT 10
                """).query((rs, n) -> new SourceCount(rs.getLong(1), rs.getString(2), rs.getString(3), rs.getString(4),
                        rs.getLong(5))).list();
        return new Documents24h(total, top);
    }

    private List<ErrorCount> topErrors24h() {
        return jdbc.sql("""
                SELECT code, count(*) AS c FROM crawl_error WHERE created_at >= now() - interval '24 hours'
                GROUP BY code ORDER BY c DESC, code LIMIT 10
                """).query((rs, n) -> new ErrorCount(rs.getString(1), rs.getLong(2))).list();
    }

    private Throughput throughput() {
        double scheduled = jdbc.sql("SELECT COALESCE(sum(60.0 / GREATEST(interval_minutes, 1)), 0) FROM source WHERE status = 'ACTIVE'")
                .query(java.math.BigDecimal.class).single().doubleValue();
        long completed = jdbc.sql("SELECT count(*) FROM crawl_job WHERE finished_at >= now() - interval '1 hour'")
                .query(Long.class).single();
        long queued = jdbc.sql("SELECT count(*) FROM crawl_job WHERE status = 'QUEUED'").query(Long.class).single();
        Double ratio = completed == 0 ? null : Math.round(scheduled / completed * 100.0) / 100.0;
        boolean workersShort = ratio != null ? ratio > 1.0 : scheduled > 0 && queued > 0;
        return new Throughput(Math.round(scheduled * 100.0) / 100.0, completed, ratio, workersShort);
    }

    /** numeric(AVG 등) 열을 Double로. PgJDBC는 getObject(name, Double.class)로 numeric을 못 바꾸므로 BigDecimal을 거친다. null은 null. */
    private static Double dbl(java.sql.ResultSet rs, String column) throws java.sql.SQLException {
        java.math.BigDecimal value = rs.getBigDecimal(column);
        return value == null ? null : value.doubleValue();
    }

}
