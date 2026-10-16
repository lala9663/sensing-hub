package io.sensinghub.credential;

import java.util.List;

import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.ResponseStatus;
import org.springframework.web.bind.annotation.RestController;

import io.sensinghub.auth.CurrentUser;
import io.sensinghub.credential.ApiCredentialService.CredentialView;
import io.sensinghub.credential.ApiCredentialService.SaveResult;
import jakarta.validation.Valid;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Pattern;
import jakarta.validation.constraints.Size;

/** 관리자: 공개 API 키 등록·조회·삭제. 비밀은 요청 본문으로만 받고 어떤 응답에도 내지 않는다. */
@RestController
@RequestMapping("/api/v1/api-credentials")
public class ApiCredentialController {

    private final ApiCredentialService service;

    public ApiCredentialController(ApiCredentialService service) {
        this.service = service;
    }

    /** provider: 워커 카탈로그의 제공자 id (예: dart). secret: 발급받은 키. */
    public record SaveRequest(@NotBlank @Size(max = 50) @Pattern(regexp = "(?i)[a-z0-9_-]+") String provider,
                              @NotBlank @Size(max = 500) String secret,
                              @Size(max = 200) String label) {
    }

    @PostMapping
    public SaveResult save(CurrentUser user, @Valid @RequestBody SaveRequest body) {
        return service.save(user, body.provider(), body.secret(), body.label());
    }

    @GetMapping
    public List<CredentialView> list(CurrentUser user) {
        return service.list(user);
    }

    @DeleteMapping("/{provider}")
    @ResponseStatus(HttpStatus.NO_CONTENT)
    public void delete(CurrentUser user, @PathVariable String provider) {
        service.delete(user, provider);
    }
}
