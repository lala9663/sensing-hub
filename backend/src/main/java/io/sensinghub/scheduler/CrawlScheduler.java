package io.sensinghub.scheduler;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;
import org.springframework.transaction.annotation.Transactional;

/**
 * 주기 수집 스케줄러. 작업을 큐에 넣기만 하고, 실제 수집은 Python 워커가 한다.
 * <ul>
 *   <li>lease가 끝난 RUNNING 작업(워커가 죽은 경우)을 회수한다.</li>
 *   <li>next_run_at이 지난 ACTIVE 소스에 CRAWL 작업을 만든다. 소스당 열린 작업은 1개뿐이다.</li>
 * </ul>
 */
@Component
@ConditionalOnProperty(name = "sensing.scheduler.enabled", havingValue = "true", matchIfMissing = true)
public class CrawlScheduler {

    private static final Logger log = LoggerFactory.getLogger(CrawlScheduler.class);
    static final int MAX_ATTEMPTS = 3;

    private final JdbcClient jdbc;

    public CrawlScheduler(JdbcClient jdbc) {
        this.jdbc = jdbc;
    }

    @Scheduled(fixedDelayString = "${sensing.scheduler.tick-ms}", initialDelay = 5000)
    @Transactional
    public void tick() {
        int reclaimed = reclaimExpiredLeases();
        int enqueued = enqueueDueSources();
        if (reclaimed > 0 || enqueued > 0) {
            log.info("scheduler tick: reclaimed={}, enqueued={}", reclaimed, enqueued);
        }
    }

    @Transactional
    public int reclaimExpiredLeases() {
        int failed = jdbc.sql("""
                UPDATE crawl_job SET status = 'FAILED', finished_at = now(), locked_by = NULL
                WHERE status = 'RUNNING' AND lease_until < now() AND attempt >= ?
                """).param(MAX_ATTEMPTS).update();
        int requeued = jdbc.sql("""
                UPDATE crawl_job SET status = 'QUEUED', locked_by = NULL, lease_until = NULL
                WHERE status = 'RUNNING' AND lease_until < now()
                """).update();
        return failed + requeued;
    }

    @Transactional
    public int enqueueDueSources() {
        int enqueued = jdbc.sql("""
                INSERT INTO crawl_job (source_id, job_type, config_version_id, trigger)
                SELECT id, 'CRAWL', active_config_version_id, 'SCHEDULE'
                FROM source
                WHERE status = 'ACTIVE' AND next_run_at <= now()
                ON CONFLICT (source_id) WHERE status IN ('QUEUED', 'RUNNING') DO NOTHING
                """).update();
        jdbc.sql("""
                UPDATE source SET next_run_at = now() + make_interval(mins => interval_minutes)
                WHERE status = 'ACTIVE' AND next_run_at <= now()
                """).update();
        return enqueued;
    }
}
