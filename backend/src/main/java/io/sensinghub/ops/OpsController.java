package io.sensinghub.ops;

import java.util.List;

import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import io.sensinghub.auth.CurrentUser;
import io.sensinghub.ops.OpsService.Overview;
import io.sensinghub.ops.OpsService.PatternCheck;
import io.sensinghub.ops.OpsService.SourceHealth;

/** 관리자 운영 지표 (M3): 대량 소스를 한 화면에서 운영하기 위한 집계. */
@RestController
@RequestMapping("/api/v1/ops")
public class OpsController {

    private final OpsService service;

    public OpsController(OpsService service) {
        this.service = service;
    }

    /** 소스 상태별·처리 대기 큐·작업 큐 적체·24시간 결과·수집량·실패 코드·처리량 추정. */
    @GetMapping("/overview")
    public Overview overview(CurrentUser user) {
        return service.overview(user);
    }

    /** 소스별 마지막 성공·연속 실패·평균 found/new. 마지막 성공이 오래된 순 (한 번도 없는 것이 먼저). status로 거른다. */
    @GetMapping("/sources/health")
    public List<SourceHealth> sourceHealth(CurrentUser user, @RequestParam(required = false) String status,
                                           @RequestParam(defaultValue = "200") int limit) {
        return service.sourceHealth(user, status, limit);
    }

    /** 소스마다 마지막 누락 점검(공개 목록의 기사 주소 중 켜진 기사 종류에 맞지 않는 비율). 재탐색 후보가 먼저, 누락 비율이 높은 순. */
    @GetMapping("/pattern-checks")
    public List<PatternCheck> patternChecks(CurrentUser user) {
        return service.patternChecks(user);
    }
}
