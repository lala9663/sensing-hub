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
 *   <li>next_run_at이 지난 ACTIVE 소스에 CRAWL 작업을 만든다. 소스당 열린 작업은 1개뿐이다 (과거 기사 채우기 묶음은 양보한다).</li>
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
        int kinds = enqueueWaitingKindRequests();
        int enqueued = enqueueDueSources();
        if (reclaimed > 0 || kinds > 0 || enqueued > 0) {
            log.info("scheduler tick: reclaimed={}, kindRequests={}, enqueued={}", reclaimed, kinds, enqueued);
        }
    }

    /**
     * 이미 수집 중인 소스에 다른 목록 주소를 등록했는데 그때 진행 중인 작업이 있어 기다리는 요청(step KIND_WAITING):
     * 소스에 열린 작업이 없어지면 ONBOARD(기사 종류 더하기) 작업을 넣고 요청을 QUEUED로 바꾼다. 주기 수집보다 먼저 본다 —
     * 이 작업이 끝에 한 번 수집하므로 같은 틱의 주기 수집은 열린 작업에 막혀 건너뛴다.
     */
    @Transactional
    public int enqueueWaitingKindRequests() {
        int enqueued = jdbc.sql("""
                INSERT INTO crawl_job (source_id, job_type, config_version_id, trigger)
                SELECT DISTINCT s.id, 'ONBOARD', s.active_config_version_id, 'REGISTRATION'
                FROM registration_request r JOIN source s ON s.id = r.source_id
                WHERE r.status = 'PROCESSING' AND r.step = 'KIND_WAITING'
                  AND NOT EXISTS (SELECT 1 FROM crawl_job j WHERE j.source_id = s.id AND j.status IN ('QUEUED', 'RUNNING'))
                ON CONFLICT (source_id) WHERE status IN ('QUEUED', 'RUNNING') DO NOTHING
                """).update();
        if (enqueued > 0) {
            jdbc.sql("""
                    UPDATE registration_request r SET step = 'QUEUED', updated_at = now()
                    WHERE r.status = 'PROCESSING' AND r.step = 'KIND_WAITING'
                      AND EXISTS (SELECT 1 FROM crawl_job j WHERE j.source_id = r.source_id AND j.status = 'QUEUED'
                                  AND j.job_type = 'ONBOARD')
                    """).update();
        }
        return enqueued;
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

    /**
     * 과거 기사 채우기(BACKFILL) 묶음은 평소 수집에 양보한다. 소스당 열린 작업은 1개라, 차례가 온 소스의 자리를 대기 중인 묶음이
     * 잡고 있으면 거두고 CRAWL을 넣는다. 실행 중인 묶음이면 next_run_at을 미루지 않고 둔다 — 워커가 "이 소스 차례가 왔다"를 보고
     * 묶음을 멈추면 다음 틱에 CRAWL이 들어간다.
     */
    @Transactional
    public int enqueueDueSources() {
        jdbc.sql("""
                UPDATE crawl_job SET status = 'SUCCESS', finished_at = now(), note = '평소 수집에 양보'
                WHERE job_type = 'BACKFILL' AND status = 'QUEUED'
                  AND source_id IN (SELECT id FROM source WHERE status = 'ACTIVE' AND next_run_at <= now())
                """).update();
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
                  AND NOT EXISTS (SELECT 1 FROM crawl_job j WHERE j.source_id = source.id AND j.job_type = 'BACKFILL'
                                  AND j.status IN ('QUEUED', 'RUNNING'))
                """).update();
        return enqueued;
    }
}
