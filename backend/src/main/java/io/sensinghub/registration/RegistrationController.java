package io.sensinghub.registration;

import java.util.List;

import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.ResponseStatus;
import org.springframework.web.bind.annotation.RestController;

import io.sensinghub.auth.CurrentUser;
import io.sensinghub.auth.Role;
import io.sensinghub.registration.RegistrationService.Registration;
import jakarta.validation.Valid;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;

@RestController
@RequestMapping("/api/v1/registrations")
public class RegistrationController {

    private final RegistrationService service;

    public RegistrationController(RegistrationService service) {
        this.service = service;
    }

    public record RegisterRequest(@NotBlank @Size(max = 2000) String url) {
    }

    /** 사용자가 하는 유일한 단계: URL 입력. */
    @PostMapping
    @ResponseStatus(HttpStatus.ACCEPTED)
    public Registration register(CurrentUser user, @Valid @RequestBody RegisterRequest body) {
        return service.register(user, body.url(), RegistrationService.Channel.WEB);
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
