package io.sensinghub.caidentia;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

/**
 * 케이던시아 연동 스케줄러: 공급사 마스터 동기화는 {@code sensing.caidentia.sync-cron}(기본 매일 06:00 KST), 리스크 이벤트 전달은
 * {@code push-tick-ms}(기본 5분)마다. {@code sensing.caidentia.enabled=false}로 끈다 (수동 API는 그대로 된다).
 * {@code push-enabled=false}면 전달만 쉰다.
 */
@Component
@ConditionalOnProperty(name = "sensing.caidentia.enabled", havingValue = "true", matchIfMissing = true)
public class CaidentiaScheduler {

    private static final Logger log = LoggerFactory.getLogger(CaidentiaScheduler.class);

    private final CaidentiaSyncService sync;
    private final CaidentiaPushService push;
    private final CaidentiaSettings settings;

    public CaidentiaScheduler(CaidentiaSyncService sync, CaidentiaPushService push, CaidentiaSettings settings) {
        this.sync = sync;
        this.push = push;
        this.settings = settings;
    }

    @Scheduled(cron = "${sensing.caidentia.sync-cron:0 0 6 * * *}", zone = "Asia/Seoul")
    public void syncTick() {
        try {
            sync.sync(sync.systemUser(), "SCHEDULE");
        } catch (RuntimeException e) {
            log.error("caidentia scheduled sync failed", e);
        }
    }

    @Scheduled(fixedDelayString = "${sensing.caidentia.push-tick-ms:300000}", initialDelay = 60000)
    public void pushTick() {
        if (!settings.pushEnabled()) {
            return;
        }
        try {
            push.pushPending();
        } catch (RuntimeException e) {
            log.error("caidentia scheduled push failed", e);
        }
    }
}
