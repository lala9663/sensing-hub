package io.sensinghub.audit;

import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Component;

/** 누가 무엇을 등록·수정·삭제했는지 남긴다 (채팅으로 한 작업 포함). */
@Component
public class AuditLogger {

    private final JdbcClient jdbc;

    public AuditLogger(JdbcClient jdbc) {
        this.jdbc = jdbc;
    }

    public void log(Long actorId, String action, String targetType, Long targetId, String detailJson) {
        jdbc.sql("""
                INSERT INTO audit_log (actor_id, action, target_type, target_id, detail)
                VALUES (?, ?, ?, ?, CAST(? AS jsonb))
                """)
                .params(actorId, action, targetType, targetId, detailJson == null ? "{}" : detailJson)
                .update();
    }
}
