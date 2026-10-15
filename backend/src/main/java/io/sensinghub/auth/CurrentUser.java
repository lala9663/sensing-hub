package io.sensinghub.auth;

import io.sensinghub.common.ApiException;

public record CurrentUser(long id, String loginId, String name, Role role) {

    public void require(Role minimum) {
        if (!role.atLeast(minimum)) {
            throw ApiException.forbidden();
        }
    }
}
