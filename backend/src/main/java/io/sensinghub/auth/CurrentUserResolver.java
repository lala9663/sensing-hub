package io.sensinghub.auth;

import java.util.List;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.core.MethodParameter;
import org.springframework.http.HttpStatus;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Component;
import org.springframework.web.bind.support.WebDataBinderFactory;
import org.springframework.web.context.request.NativeWebRequest;
import org.springframework.web.method.support.HandlerMethodArgumentResolver;
import org.springframework.web.method.support.ModelAndViewContainer;
import org.springframework.web.servlet.config.annotation.WebMvcConfigurer;

import io.sensinghub.common.ApiException;

/**
 * PoC 인증: {@code X-User} 헤더의 login_id로 사용자를 찾는다.
 * SSO(OIDC/SAML) 연동 시 이 클래스만 교체하고, 컨트롤러는 {@link CurrentUser}를 그대로 받는다.
 */
@Component
public class CurrentUserResolver implements HandlerMethodArgumentResolver, WebMvcConfigurer {

    public static final String HEADER = "X-User";

    private final JdbcClient jdbc;
    private final String defaultUser;

    public CurrentUserResolver(JdbcClient jdbc, @Value("${sensing.auth.default-user}") String defaultUser) {
        this.jdbc = jdbc;
        this.defaultUser = defaultUser;
    }

    @Override
    public void addArgumentResolvers(List<HandlerMethodArgumentResolver> resolvers) {
        resolvers.add(this);
    }

    @Override
    public boolean supportsParameter(MethodParameter parameter) {
        return parameter.getParameterType().equals(CurrentUser.class);
    }

    @Override
    public Object resolveArgument(MethodParameter parameter, ModelAndViewContainer mavContainer,
                                  NativeWebRequest webRequest, WebDataBinderFactory binderFactory) {
        String loginId = webRequest.getHeader(HEADER);
        if (loginId == null || loginId.isBlank()) {
            loginId = defaultUser;
        }
        return jdbc.sql("SELECT id, login_id, name, role FROM app_user WHERE login_id = ?")
                .param(loginId.strip())
                .query((rs, n) -> new CurrentUser(rs.getLong("id"), rs.getString("login_id"),
                        rs.getString("name"), Role.valueOf(rs.getString("role"))))
                .optional()
                .orElseThrow(() -> new ApiException(HttpStatus.UNAUTHORIZED, "UNAUTHORIZED", "알 수 없는 사용자입니다."));
    }
}
