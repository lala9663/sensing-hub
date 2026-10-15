package io.sensinghub.auth;

/** 권한 등급. 선언 순서가 곧 높낮이다. */
public enum Role {
    VIEWER,
    REGISTRAR,
    ADMIN;

    public boolean atLeast(Role other) {
        return this.ordinal() >= other.ordinal();
    }
}
