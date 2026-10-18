package io.sensinghub.caidentia;

import java.sql.ResultSet;
import java.sql.SQLException;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.List;
import java.util.Optional;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Service;

import io.sensinghub.audit.AuditLogger;
import io.sensinghub.auth.CurrentUser;
import io.sensinghub.auth.Role;
import io.sensinghub.caidentia.CaidentiaClient.CaidentiaException;
import io.sensinghub.caidentia.CaidentiaClient.Supplier;
import io.sensinghub.common.ApiException;
import io.sensinghub.company.CompanyService;
import io.sensinghub.company.CompanyService.CompanyInput;
import io.sensinghub.company.CompanyService.Outcome;
import io.sensinghub.company.CompanyService.UpsertResult;

/**
 * 케이던시아 공급사 마스터 → 회사 마스터 동기화. 한 번의 {@link #sync}가 {@code caidentia_sync_run} 한 건이다.
 * <ul>
 *   <li>공급사 전체를 받아({@link CaidentiaClient#fetchSuppliers}) 한 건씩 반영한다. 매칭은 {@code external_id}(= supplierId) 우선, 없으면 사업자번호
 *       ({@link CompanyService#upsertExternal}). 필드 매핑: name·aliases·bizRegNo→biz_no·dartCorpCode→corp_code·supplierId→external_id,
 *       tags = [tier, category]. 새 회사는 보통 등록과 같이 회사 감시·점검 소스가 생긴다.</li>
 *   <li>INACTIVE 공급사는 그 회사를 보관(ARCHIVED, 감시 EXCLUDED)한다. 다시 ACTIVE가 되면 복구된다 (import와 같은 규칙).</li>
 *   <li>케이던시아 호출 실패(연결·401)는 FAILED + error로 기록하고 예외를 밖으로 내지 않는다. 공급사 한 건의 반영 실패(예: 다른 회사가 쓰는
 *       사업자번호)는 그 건만 건너뛰고 error에 적는다.</li>
 * </ul>
 * 스케줄 실행의 행위자는 app_user {@code system}이다.
 */
@Service
public class CaidentiaSyncService {

    private static final Logger log = LoggerFactory.getLogger(CaidentiaSyncService.class);

    public record RunView(long id, OffsetDateTime startedAt, OffsetDateTime finishedAt, String status, String trigger, int fetched,
                          int created, int updated, int archived, int unchanged, String error) {
    }

    private final JdbcClient jdbc;
    private final CaidentiaClient client;
    private final CompanyService companies;
    private final AuditLogger audit;

    public CaidentiaSyncService(JdbcClient jdbc, CaidentiaClient client, CompanyService companies, AuditLogger audit) {
        this.jdbc = jdbc;
        this.client = client;
        this.companies = companies;
        this.audit = audit;
    }

    /** 동기화 한 번. 호출자에게 예외를 내지 않고 결과를 기록해 돌려준다. trigger는 SCHEDULE 또는 MANUAL. */
    public RunView sync(CurrentUser actor, String trigger) {
        actor.require(Role.ADMIN);
        long runId = jdbc.sql("INSERT INTO caidentia_sync_run (status, trigger) VALUES ('RUNNING', ?) RETURNING id")
                .param(trigger).query(Long.class).single();
        int fetched = 0;
        int created = 0;
        int updated = 0;
        int archived = 0;
        int unchanged = 0;
        List<String> errors = new ArrayList<>();
        try {
            List<Supplier> suppliers = client.fetchSuppliers(null);
            fetched = suppliers.size();
            for (Supplier s : suppliers) {
                try {
                    switch (apply(actor, s)) {
                        case CREATED -> created++;
                        case UPDATED -> updated++;
                        case ARCHIVED -> archived++;
                        case UNCHANGED -> unchanged++;
                    }
                } catch (ApiException | CaidentiaException e) {
                    log.warn("caidentia supplier {} ({}) not applied: {}", s.supplierId(), s.name(), e.getMessage());
                    errors.add(s.supplierId() + ": " + e.getMessage());
                }
            }
        } catch (CaidentiaException e) {
            errors.add(0, e.getMessage());
        } catch (RuntimeException e) {
            log.error("caidentia sync {} failed", runId, e);
            errors.add(0, e.getClass().getSimpleName() + ": " + (e.getMessage() == null ? "" : e.getMessage()));
        }
        String error = errors.isEmpty() ? null : brief(String.join("; ", errors));
        String status = errors.isEmpty() ? "SUCCESS" : "FAILED";
        jdbc.sql("""
                UPDATE caidentia_sync_run SET finished_at = now(), status = ?, fetched = ?, created = ?, updated = ?, archived = ?,
                       unchanged = ?, error = ? WHERE id = ?
                """).params(status, fetched, created, updated, archived, unchanged, error, runId).update();
        audit.log(actor.id(), "CAIDENTIA_SYNC", "caidentia_sync_run", runId,
                "{\"status\":\"" + status + "\",\"fetched\":" + fetched + ",\"created\":" + created + ",\"updated\":" + updated
                        + ",\"archived\":" + archived + ",\"unchanged\":" + unchanged + "}");
        log.info("caidentia sync {} {}: fetched={} created={} updated={} archived={} unchanged={}{}", runId, status, fetched, created,
                updated, archived, unchanged, error == null ? "" : " - " + error);
        return run(runId);
    }

    /** 공급사 한 건 → 회사 한 건. */
    private Outcome apply(CurrentUser actor, Supplier s) {
        if (s.supplierId() == null || s.supplierId().isBlank()) {
            throw new CaidentiaException("supplierId가 없습니다.");
        }
        List<String> tags = new ArrayList<>();
        if (s.tier() != null && !s.tier().isBlank()) {
            tags.add(s.tier().strip());
        }
        if (s.category() != null && !s.category().isBlank()) {
            tags.add(s.category().strip());
        }
        CompanyInput in = new CompanyInput(s.name(), s.aliases() == null ? List.of() : s.aliases(), s.bizRegNo(), s.dartCorpCode(),
                s.supplierId(), tags);
        if ("INACTIVE".equalsIgnoreCase(s.status())) {
            Optional<CompanyService.CompanyView> existing = companies.findExternal(actor, in);
            if (existing.isPresent() && "ACTIVE".equals(existing.get().status())) {
                companies.archive(actor, existing.get().id());
                return Outcome.ARCHIVED;
            }
            return Outcome.UNCHANGED;   // 모르는 회사이거나 이미 보관됨
        }
        UpsertResult r = companies.upsertExternal(actor, in);
        return r.outcome();
    }

    public List<RunView> runs(int limit) {
        return jdbc.sql(SELECT + " ORDER BY id DESC LIMIT ?").param(Math.max(1, Math.min(200, limit))).query(this::map).list();
    }

    public RunView lastRun() {
        return jdbc.sql(SELECT + " ORDER BY id DESC LIMIT 1").query(this::map).optional().orElse(null);
    }

    public RunView run(long id) {
        return jdbc.sql(SELECT + " WHERE id = ?").param(id).query(this::map).single();
    }

    /** 스케줄 실행의 행위자 (app_user 'system', V10). */
    public CurrentUser systemUser() {
        return jdbc.sql("SELECT id, login_id, name, role FROM app_user WHERE login_id = 'system'")
                .query((rs, n) -> new CurrentUser(rs.getLong("id"), rs.getString("login_id"), rs.getString("name"),
                        Role.valueOf(rs.getString("role"))))
                .single();
    }

    private static String brief(String s) {
        return s.length() > 500 ? s.substring(0, 497) + "..." : s;
    }

    private static final String SELECT = """
            SELECT id, started_at, finished_at, status, trigger, fetched, created, updated, archived, unchanged, error
            FROM caidentia_sync_run
            """;

    private RunView map(ResultSet rs, int n) throws SQLException {
        return new RunView(rs.getLong("id"), rs.getObject("started_at", OffsetDateTime.class),
                rs.getObject("finished_at", OffsetDateTime.class), rs.getString("status"), rs.getString("trigger"),
                rs.getInt("fetched"), rs.getInt("created"), rs.getInt("updated"), rs.getInt("archived"), rs.getInt("unchanged"),
                rs.getString("error"));
    }
}
