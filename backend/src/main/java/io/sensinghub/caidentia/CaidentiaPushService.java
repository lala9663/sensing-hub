package io.sensinghub.caidentia;

import java.time.OffsetDateTime;
import java.util.List;
import java.util.Map;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Service;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;

import io.sensinghub.caidentia.CaidentiaClient.CaidentiaException;
import io.sensinghub.caidentia.CaidentiaClient.RiskEvent;

/**
 * 회사 신호 → 케이던시아 리스크 이벤트 전달. 아직 보내지 않은({@code company_signal.pushed_at IS NULL}) HIGH·MEDIUM 신호 중
 * 케이던시아가 아는 회사({@code company.external_id}가 있는 회사)의 것을 오래된 순으로 보내고, 성공한 건은 {@code pushed_at}을 찍는다.
 * 한 건이 실패하면 거기서 멈추고(대개 인증·연결 문제라 다음 건도 실패한다) 다음 틱(5분)에 같은 자리부터 다시 보낸다. LOW와 외부 ID가
 * 없는 회사의 신호는 보내지 않으며 대기 수에도 들지 않는다. 예외는 호출자에게 내지 않는다.
 */
@Service
public class CaidentiaPushService {

    private static final Logger log = LoggerFactory.getLogger(CaidentiaPushService.class);
    private static final TypeReference<Map<String, Object>> OBJECT = new TypeReference<>() { };
    static final int BATCH = 200;

    public record PushResult(int pushed, int failed, long pending, String error) {
    }

    private record Pending(long id, String supplierId, String companyName, String kind, String severity, String title,
                           OffsetDateTime observedAt, String detail) {
    }

    private final JdbcClient jdbc;
    private final CaidentiaClient client;
    private final ObjectMapper json;

    public CaidentiaPushService(JdbcClient jdbc, CaidentiaClient client, ObjectMapper json) {
        this.jdbc = jdbc;
        this.client = client;
        this.json = json;
    }

    /** 대기 중인 신호를 최대 {@link #BATCH}건 보낸다. */
    public PushResult pushPending() {
        List<Pending> pending = jdbc.sql("""
                SELECT g.id, c.external_id, c.name AS company_name, g.kind, g.severity, g.title, g.observed_at, g.detail::text AS detail
                FROM company_signal g JOIN company c ON c.id = g.company_id
                WHERE g.pushed_at IS NULL AND g.severity IN ('HIGH', 'MEDIUM') AND c.external_id IS NOT NULL
                ORDER BY g.id LIMIT ?
                """).param(BATCH).query((rs, n) -> new Pending(rs.getLong("id"), rs.getString("external_id"),
                        rs.getString("company_name"), rs.getString("kind"), rs.getString("severity"), rs.getString("title"),
                        rs.getObject("observed_at", OffsetDateTime.class), rs.getString("detail"))).list();
        int pushed = 0;
        String error = null;
        for (Pending p : pending) {
            RiskEvent event = new RiskEvent(p.supplierId(), p.companyName(), p.kind(), p.severity(), p.title(), p.observedAt(),
                    detail(p.detail()));
            try {
                client.pushRiskEvent(event);
            } catch (CaidentiaException e) {
                error = e.getMessage();
                log.warn("caidentia push stopped at signal {} ({}건 보냄): {}", p.id(), pushed, error);
                break;
            }
            jdbc.sql("UPDATE company_signal SET pushed_at = now() WHERE id = ?").param(p.id()).update();
            pushed++;
        }
        if (pushed > 0) {
            log.info("caidentia push: {}건 전달", pushed);
        }
        return new PushResult(pushed, error == null ? 0 : pending.size() - pushed, pendingCount(), error);
    }

    public long pushedCount() {
        return jdbc.sql("SELECT count(*) FROM company_signal WHERE pushed_at IS NOT NULL").query(Long.class).single();
    }

    public long pendingCount() {
        return jdbc.sql("""
                SELECT count(*) FROM company_signal g JOIN company c ON c.id = g.company_id
                WHERE g.pushed_at IS NULL AND g.severity IN ('HIGH', 'MEDIUM') AND c.external_id IS NOT NULL
                """).query(Long.class).single();
    }

    private Map<String, Object> detail(String text) {
        if (text == null || text.isBlank()) {
            return Map.of();
        }
        try {
            return json.readValue(text, OBJECT);
        } catch (JsonProcessingException e) {
            return Map.of();
        }
    }
}
