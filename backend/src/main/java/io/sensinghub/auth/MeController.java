package io.sensinghub.auth;

import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

/** 화면이 상단 사용자 선택(X-User)으로 누가·어떤 권한으로 보고 있는지 확인하는 용도. */
@RestController
@RequestMapping("/api/v1/me")
public class MeController {

    public record MeView(long id, String loginId, String name, String role) {
    }

    @GetMapping
    public MeView me(CurrentUser user) {
        return new MeView(user.id(), user.loginId(), user.name(), user.role().name());
    }
}
