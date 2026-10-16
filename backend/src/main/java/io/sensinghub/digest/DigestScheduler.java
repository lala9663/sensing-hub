package io.sensinghub.digest;

import java.time.OffsetDateTime;
import java.time.ZonedDateTime;
import java.util.List;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

import io.sensinghub.digest.DigestService.RunView;
import io.sensinghub.digest.DigestService.SubscriptionView;

/**
 * 다이제스트 스케줄러 (5분마다). KST 기준 현재 시가 hour_kst이고 last_sent_at이 오늘(KST) 이전(또는 없음)인 enabled 구독을
 * 하나씩 보낸다. 구독 하나의 실패(FAILED 기록, last_sent_at 그대로)는 다른 구독을 막지 않고, 같은 시간대의 다음 틱에 다시 시도한다.
 * EMPTY도 last_sent_at을 갱신하므로 빈 날은 한 번만 건너뛴다. {@code sensing.digest.enabled=false}로 끈다.
 */
@Component
@ConditionalOnProperty(name = "sensing.digest.enabled", havingValue = "true", matchIfMissing = true)
public class DigestScheduler {

    private static final Logger log = LoggerFactory.getLogger(DigestScheduler.class);

    private final JdbcClient jdbc;
    private final DigestService service;

    public DigestScheduler(JdbcClient jdbc, DigestService service) {
        this.jdbc = jdbc;
        this.service = service;
    }

    @Scheduled(fixedDelayString = "${sensing.digest.tick-ms:300000}", initialDelay = 10000)
    public void tick() {
        tick(ZonedDateTime.now(DigestComposer.KST));
    }

    /** 처리한 구독 수를 돌려준다. */
    public int tick(ZonedDateTime nowKst) {
        OffsetDateTime todayStart = nowKst.toLocalDate().atStartOfDay(DigestComposer.KST).toOffsetDateTime();
        List<Long> due = jdbc.sql("""
                SELECT id FROM digest_subscription
                WHERE enabled AND hour_kst = ? AND (last_sent_at IS NULL OR last_sent_at < ?)
                ORDER BY id
                """).params(nowKst.getHour(), todayStart).query(Long.class).list();
        int processed = 0;
        for (long id : due) {
            try {
                SubscriptionView s = service.load(id);
                if (s == null) {
                    continue;
                }
                RunView run = service.deliver(s, nowKst.toOffsetDateTime(), null);
                log.info("digest {} ({}): {} {}건{}", id, s.name(), run.status(), run.documentCount(),
                        run.error() == null ? "" : " - " + run.error());
                processed++;
            } catch (RuntimeException e) {
                log.error("digest {} failed", id, e);
            }
        }
        return processed;
    }
}
