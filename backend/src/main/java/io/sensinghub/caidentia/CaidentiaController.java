package io.sensinghub.caidentia;

import java.util.List;
import java.util.Map;

import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import io.sensinghub.auth.CurrentUser;
import io.sensinghub.auth.Role;
import io.sensinghub.caidentia.CaidentiaPushService.PushResult;
import io.sensinghub.caidentia.CaidentiaSyncService.RunView;
import io.sensinghub.common.ApiException;

/**
 * 케이던시아 연동 관리 API (ADMIN).
 * <pre>
 * GET  /api/v1/integrations/caidentia                  상태 {baseUrl, mock, pushEnabled, syncCron, lastSync, pushedCount, pendingCount, runs[]}
 * POST /api/v1/integrations/caidentia/sync-now         공급사 마스터 동기화 지금 (결과 run)
 * POST /api/v1/integrations/caidentia/push-now         대기 중인 HIGH·MEDIUM 신호 지금 전달 {pushed, failed, pending, error}
 * GET  /api/v1/integrations/caidentia/received-events  데모 전용: 가짜 케이던시아가 받은 이벤트 (목업이 아니면 409)
 * </pre>
 */
@RestController
@RequestMapping("/api/v1/integrations/caidentia")
public class CaidentiaController {

    public record Status(String baseUrl, boolean mock, boolean pushEnabled, String syncCron, RunView lastSync, long pushedCount,
                         long pendingCount, List<RunView> runs) {
    }

    private final CaidentiaSettings settings;
    private final CaidentiaSyncService sync;
    private final CaidentiaPushService push;
    private final RestCaidentiaClient rest;

    public CaidentiaController(CaidentiaSettings settings, CaidentiaSyncService sync, CaidentiaPushService push,
                               RestCaidentiaClient rest) {
        this.settings = settings;
        this.sync = sync;
        this.push = push;
        this.rest = rest;
    }

    @GetMapping
    public Status status(CurrentUser user) {
        user.require(Role.ADMIN);
        return new Status(settings.baseUrl(), settings.mock(), settings.pushEnabled(), settings.syncCron(), sync.lastRun(),
                push.pushedCount(), push.pendingCount(), sync.runs(20));
    }

    @PostMapping("/sync-now")
    public RunView syncNow(CurrentUser user) {
        return sync.sync(user, "MANUAL");
    }

    @PostMapping("/push-now")
    public PushResult pushNow(CurrentUser user) {
        user.require(Role.ADMIN);
        return push.pushPending();
    }

    @GetMapping("/received-events")
    public List<Map<String, Object>> receivedEvents(CurrentUser user) {
        user.require(Role.ADMIN);
        if (!settings.mock()) {
            throw ApiException.conflict("목업 케이던시아가 아니어서 받은 이벤트를 볼 수 없습니다: " + settings.baseUrl());
        }
        try {
            return rest.fetchReceivedEvents();
        } catch (CaidentiaClient.CaidentiaException e) {
            throw new ApiException(org.springframework.http.HttpStatus.BAD_GATEWAY, "CAIDENTIA", e.getMessage());
        }
    }
}
