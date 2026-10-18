package io.sensinghub.registration;

import java.util.List;

import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.ResponseStatus;
import org.springframework.web.bind.annotation.RestController;

import io.sensinghub.auth.CurrentUser;
import io.sensinghub.auth.Role;
import io.sensinghub.registration.BulkRegistrationService.BatchSummary;
import io.sensinghub.registration.BulkRegistrationService.BulkResult;
import io.sensinghub.registration.RegistrationService.Registration;
import jakarta.validation.Valid;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Size;

@RestController
@RequestMapping("/api/v1/registrations")
public class RegistrationController {

    private final RegistrationService service;
    private final BulkRegistrationService bulk;

    public RegistrationController(RegistrationService service, BulkRegistrationService bulk) {
        this.service = service;
        this.bulk = bulk;
    }

    public record RegisterRequest(@NotBlank @Size(max = 2000) String url) {
    }

    /** 사용자가 하는 유일한 단계: URL 입력. */
    @PostMapping
    @ResponseStatus(HttpStatus.ACCEPTED)
    public Registration register(CurrentUser user, @Valid @RequestBody RegisterRequest body) {
        return service.register(user, body.url(), RegistrationService.Channel.WEB);
    }

    public record BulkRequest(@NotNull @Size(max = BulkRegistrationService.MAX_URLS) List<@Size(max = 2000) String> urls) {
    }

    /** 일괄 등록 (REGISTRAR 이상): JSON {"urls":[...]} 최대 500건. 한 건의 오류가 다른 건을 막지 않는다. */
    @PostMapping(value = "/bulk", consumes = MediaType.APPLICATION_JSON_VALUE)
    @ResponseStatus(HttpStatus.ACCEPTED)
    public BulkResult bulk(CurrentUser user, @Valid @RequestBody BulkRequest body) {
        return bulk.register(user, body.urls());
    }

    /** 일괄 등록 (REGISTRAR 이상): text/plain 한 줄에 URL 하나, {@code #} 뒤는 메모. */
    @PostMapping(value = "/bulk", consumes = MediaType.TEXT_PLAIN_VALUE)
    @ResponseStatus(HttpStatus.ACCEPTED)
    public BulkResult bulkText(CurrentUser user, @RequestBody(required = false) String body) {
        return bulk.register(user, BulkRegistrationService.parseLines(body));
    }

    /** 묶음 진행 요약: 몇 곳이 자동 등록됐고 몇 곳이 관리자 큐에 갔는지. 요청자 본인 또는 ADMIN. */
    @GetMapping("/batches/{batchId}")
    public BatchSummary batch(CurrentUser user, @PathVariable String batchId) {
        return bulk.summary(user, batchId);
    }

    @GetMapping("/mine")
    public List<Registration> mine(CurrentUser user) {
        return service.mine(user);
    }

    @GetMapping("/{id}")
    public Registration get(CurrentUser user, @PathVariable long id) {
        Registration reg = service.get(id);
        if (!user.role().atLeast(Role.ADMIN) && service.mine(user).stream().noneMatch(r -> r.id() == id)) {
            throw io.sensinghub.common.ApiException.notFound("등록 요청");
        }
        return reg;
    }

    @GetMapping("/awaiting-approval")
    public List<Registration> awaitingApproval(CurrentUser user) {
        user.require(Role.ADMIN);
        return service.awaitingApproval();
    }

    @PostMapping("/{id}/approve")
    public Registration approve(CurrentUser user, @PathVariable long id) {
        return service.approve(user, id);
    }

    public record RejectRequest(@Size(max = 300) String reason) {
    }

    @PostMapping("/{id}/reject")
    public Registration reject(CurrentUser user, @PathVariable long id,
                               @Valid @RequestBody(required = false) RejectRequest body) {
        return service.reject(user, id, body == null ? null : body.reason());
    }
}
