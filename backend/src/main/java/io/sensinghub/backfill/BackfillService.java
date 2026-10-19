package io.sensinghub.backfill;

import java.time.LocalDate;
import java.time.OffsetDateTime;
import java.time.ZoneId;
import java.util.List;

import org.springframework.http.HttpStatus;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import io.sensinghub.audit.AuditLogger;
import io.sensinghub.auth.CurrentUser;
import io.sensinghub.auth.Role;
import io.sensinghub.common.ApiException;

/**
 * 과거 기사 채우기(백필): 등록 전에 나온 기사를 기간을 정해 천천히 가져온다.
 * <ul>
 *   <li>요청(backfill 행)만 만든다. 실제 수집은 큐가 빈 워커가 crawl_job(BACKFILL)을 한 묶음(요청 100건 안팎)씩 만들어 한다.
 *       평소 수집·등록이 대기하면 워커가 묶음을 멈추고 양보한다 (worker backfill.py).</li>
 *   <li>사이트 수집 소스(kind SITE, ACTIVE, API 아닌 설정)만. 소스당 진행 중인 채우기는 하나.</li>
 *   <li>기간: 기본 최근 3개월, 길이 최대 1년, 끝 날짜는 오늘(KST)까지.</li>
 *   <li>진행률: 찾은 주소·저장·이미 있음·기간 밖·실패·남은 수(PENDING 주소).</li>
 * </ul>
 */
@Service
public class BackfillService {

    public static final ZoneId KST = ZoneId.of("Asia/Seoul");
    public static final int DEFAULT_MONTHS = 3;
    public static final int MAX_DAYS = 366;

    private final JdbcClient jdbc;
    private final AuditLogger audit;

    public BackfillService(JdbcClient jdbc, AuditLogger audit) {
        this.jdbc = jdbc;
        this.audit = audit;
    }

    /**
     * remaining: 아직 열지 않은 주소(PENDING). requests: 사이트에 보낸 요청 수. chunks: 실행한 묶음 수.
     * message: 워커가 남긴 진행·종료 설명 (예: "보관 사이트맵 2개", "목록 3쪽에서 기간 시작보다 오래된 글이 나와 멈춤").
     */
    public record BackfillView(long id, long sourceId, LocalDate from, LocalDate to, String status, String method,
                               int found, int saved, int skipped, int outOfRange, int failed, long remaining,
                               int requests, int chunks, String message, String requestedBy,
                               OffsetDateTime createdAt, OffsetDateTime startedAt, OffsetDateTime finishedAt,
                               OffsetDateTime updatedAt) {
    }

    private static final String VIEW_SQL = """
            SELECT b.id, b.source_id, b.date_from, b.date_to, b.status, b.method, b.found_count, b.saved_count,
                   b.skipped_count, b.out_of_range_count, b.failed_count,
                   (SELECT count(*) FROM backfill_url u WHERE u.backfill_id = b.id AND u.status = 'PENDING') AS remaining,
                   b.request_count, b.chunk_count, b.message, a.name AS requested_by,
                   b.created_at, b.started_at, b.finished_at, b.updated_at
            FROM backfill b LEFT JOIN app_user a ON a.id = b.requested_by
            """;

    /** 소스의 채우기 기록 (최신 순, 최근 20건). */
    public List<BackfillView> list(CurrentUser user, long sourceId) {
        user.require(Role.REGISTRAR);
        requireSource(sourceId);
        return jdbc.sql(VIEW_SQL + " WHERE b.source_id = ? ORDER BY b.id DESC LIMIT 20")
                .param(sourceId).query(this::map).list();
    }

    /** 채우기 요청. from·to를 비우면 최근 3개월(오늘까지). */
    @Transactional
    public BackfillView request(CurrentUser user, long sourceId, LocalDate from, LocalDate to) {
        user.require(Role.REGISTRAR);
        LocalDate today = LocalDate.now(KST);
        LocalDate end = to != null ? to : today;
        LocalDate start = from != null ? from : end.minusMonths(DEFAULT_MONTHS);
        if (end.isAfter(today)) {
            throw badRequest("끝 날짜는 오늘(" + today + ") 이후일 수 없습니다.");
        }
        if (start.isAfter(end)) {
            throw badRequest("시작 날짜가 끝 날짜보다 늦습니다.");
        }
        if (start.plusDays(MAX_DAYS).isBefore(end)) {
            throw badRequest("기간은 최대 1년입니다. 더 긴 기간은 나눠서 요청해 주세요.");
        }
        SourceRow s = requireSource(sourceId);
        if (!"SITE".equals(s.kind()) || "API".equals(s.method()) || !s.hasConfig()) {
            throw ApiException.conflict("사이트 수집 소스만 과거 기사를 채울 수 있습니다 (키워드 감시·공개 API·점검 소스 제외).");
        }
        if (!"ACTIVE".equals(s.status())) {
            throw ApiException.conflict("수집 중(ACTIVE)인 소스만 과거 기사를 채울 수 있습니다. 현재 상태: " + s.status());
        }
        Long open = jdbc.sql("SELECT id FROM backfill WHERE source_id = ? AND status IN ('QUEUED', 'RUNNING')")
                .param(sourceId).query(Long.class).optional().orElse(null);
        if (open != null) {
            throw ApiException.conflict("이미 진행 중인 과거 기사 채우기(#" + open + ")가 있습니다. 끝나거나 취소한 뒤 다시 요청해 주세요.");
        }
        long id = jdbc.sql("""
                INSERT INTO backfill (source_id, date_from, date_to, requested_by, message)
                VALUES (?, ?, ?, ?, ?) RETURNING id
                """).params(sourceId, start, end, user.id(),
                        "요청을 받았습니다. 워커가 한가할 때 조금씩 진행합니다 (평소 수집이 먼저입니다).")
                .query(Long.class).single();
        audit.log(user.id(), "BACKFILL_REQUESTED", "source", sourceId,
                "{\"backfillId\":" + id + ",\"from\":\"" + start + "\",\"to\":\"" + end + "\"}");
        return get(id);
    }

    /** 취소. 이미 저장한 문서는 그대로 두고, 대기 중인 묶음 작업은 거둔다 (실행 중인 묶음은 워커가 다음 주소 전에 멈춘다). */
    @Transactional
    public BackfillView cancel(CurrentUser user, long sourceId, long backfillId) {
        user.require(Role.REGISTRAR);
        BackfillView b = get(backfillId);
        if (b.sourceId() != sourceId) {
            throw ApiException.notFound("과거 기사 채우기");
        }
        int updated = jdbc.sql("""
                UPDATE backfill SET status = 'CANCELLED', cancelled_by = ?, finished_at = now(), updated_at = now(),
                       message = ?
                WHERE id = ? AND status IN ('QUEUED', 'RUNNING')
                """).params(user.id(), "취소했습니다 (" + user.name() + "). 저장한 문서는 그대로 둡니다.", backfillId).update();
        if (updated == 0) {
            throw ApiException.conflict("이미 끝난 채우기입니다 (" + b.status() + ").");
        }
        jdbc.sql("""
                UPDATE crawl_job SET status = 'SUCCESS', finished_at = now(), note = '과거 기사 채우기 취소'
                WHERE backfill_id = ? AND status = 'QUEUED'
                """).param(backfillId).update();
        audit.log(user.id(), "BACKFILL_CANCELLED", "source", sourceId, "{\"backfillId\":" + backfillId + "}");
        return get(backfillId);
    }

    BackfillView get(long id) {
        return jdbc.sql(VIEW_SQL + " WHERE b.id = ?").param(id).query(this::map).optional()
                .orElseThrow(() -> ApiException.notFound("과거 기사 채우기"));
    }

    private record SourceRow(String kind, String status, String method, boolean hasConfig) {
    }

    private SourceRow requireSource(long sourceId) {
        return jdbc.sql("SELECT kind, status, method, active_config_version_id IS NOT NULL FROM source WHERE id = ?")
                .param(sourceId)
                .query((rs, n) -> new SourceRow(rs.getString(1), rs.getString(2), rs.getString(3), rs.getBoolean(4)))
                .optional().orElseThrow(() -> ApiException.notFound("소스"));
    }

    private static ApiException badRequest(String message) {
        return new ApiException(HttpStatus.BAD_REQUEST, "BAD_REQUEST", message);
    }

    private BackfillView map(java.sql.ResultSet rs, int n) throws java.sql.SQLException {
        return new BackfillView(rs.getLong("id"), rs.getLong("source_id"),
                rs.getObject("date_from", LocalDate.class), rs.getObject("date_to", LocalDate.class),
                rs.getString("status"), rs.getString("method"), rs.getInt("found_count"), rs.getInt("saved_count"),
                rs.getInt("skipped_count"), rs.getInt("out_of_range_count"), rs.getInt("failed_count"),
                rs.getLong("remaining"), rs.getInt("request_count"), rs.getInt("chunk_count"), rs.getString("message"),
                rs.getString("requested_by"), rs.getObject("created_at", OffsetDateTime.class),
                rs.getObject("started_at", OffsetDateTime.class), rs.getObject("finished_at", OffsetDateTime.class),
                rs.getObject("updated_at", OffsetDateTime.class));
    }
}
