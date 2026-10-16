package io.sensinghub.digest;

import java.util.List;

import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.PutMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.ResponseStatus;
import org.springframework.web.bind.annotation.RestController;

import io.sensinghub.auth.CurrentUser;
import io.sensinghub.digest.DigestService.Filters;
import io.sensinghub.digest.DigestService.Preview;
import io.sensinghub.digest.DigestService.RunView;
import io.sensinghub.digest.DigestService.SubscriptionView;
import jakarta.validation.Valid;
import jakarta.validation.constraints.Max;
import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;

/**
 * 일간 다이제스트 구독 (VIEWER 이상, 본인 것. ADMIN은 전체).
 * channel EMAIL은 메일 주소, WEBHOOK은 http(s) 절대 URL(내부망 거부). hourKst 0~23(기본 8).
 * filters {"watchIds":[...],"sourceIds":[...],"topics":[...],"companyIds":[...]}는 하나라도 맞으면 포함, 모두 비면 전체.
 * 회사 신호(거래처 신호 절)는 전체 구독이거나 companyIds를 지정한 구독에 들어간다.
 */
@RestController
@RequestMapping("/api/v1/digests")
public class DigestController {

    private final DigestService service;

    public DigestController(DigestService service) {
        this.service = service;
    }

    public record CreateRequest(@NotBlank @Size(max = 200) String name,
                                @NotBlank String channel,
                                @NotBlank @Size(max = 500) String target,
                                @Min(0) @Max(23) Integer hourKst,
                                Filters filters) {
    }

    public record UpdateRequest(@Size(max = 200) String name,
                                String channel,
                                @Size(max = 500) String target,
                                @Min(0) @Max(23) Integer hourKst,
                                Filters filters,
                                Boolean enabled) {
    }

    @PostMapping
    @ResponseStatus(HttpStatus.CREATED)
    public SubscriptionView create(CurrentUser user, @Valid @RequestBody CreateRequest body) {
        return service.create(user, body.name(), body.channel(), body.target(), body.hourKst(), body.filters());
    }

    /** 내 구독 (ADMIN은 전체). */
    @GetMapping
    public List<SubscriptionView> list(CurrentUser user) {
        return service.list(user);
    }

    @GetMapping("/{id}")
    public SubscriptionView get(CurrentUser user, @PathVariable long id) {
        return service.get(user, id);
    }

    /** 생략한 필드는 그대로 둔다. */
    @PutMapping("/{id}")
    public SubscriptionView update(CurrentUser user, @PathVariable long id, @Valid @RequestBody UpdateRequest body) {
        return service.update(user, id, body.name(), body.channel(), body.target(), body.hourKst(), body.filters(),
                body.enabled());
    }

    @DeleteMapping("/{id}")
    @ResponseStatus(HttpStatus.NO_CONTENT)
    public void delete(CurrentUser user, @PathVariable long id) {
        service.delete(user, id);
    }

    /** 지금 기준으로 본문을 만들어 돌려준다 (보내지 않는다). */
    @PostMapping("/{id}/preview")
    public Preview preview(CurrentUser user, @PathVariable long id) {
        return service.preview(user, id);
    }

    /** 즉시 한 번 보낸다 (테스트용). digest_run에 기록된다. */
    @PostMapping("/{id}/send-now")
    public RunView sendNow(CurrentUser user, @PathVariable long id) {
        return service.sendNow(user, id);
    }

    /** 최근 기록(보낸 본문 포함), 최신순. */
    @GetMapping("/{id}/runs")
    public List<RunView> runs(CurrentUser user, @PathVariable long id,
                              @RequestParam(required = false, defaultValue = "30") int limit) {
        return service.runs(user, id, limit);
    }
}
