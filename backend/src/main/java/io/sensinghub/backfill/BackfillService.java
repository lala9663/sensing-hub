package io.sensinghub.backfill;

import java.time.LocalDate;
import java.time.OffsetDateTime;
import java.time.ZoneId;
import java.time.temporal.ChronoUnit;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpStatus;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import io.sensinghub.audit.AuditLogger;
import io.sensinghub.auth.CurrentUser;
import io.sensinghub.auth.Role;
import io.sensinghub.common.ApiException;

/**
 * 과거 기사 채우기(백필): 등록 전에 나온 기사를 기간 묶음 단위로, 가장 최근 묶음부터 과거로 천천히 가져온다.
 * <ul>
 *   <li>요청(backfill 행)만 만든다. 실제 수집은 큐가 빈 워커가 crawl_job(BACKFILL)을 한 번에 요청 100건 안팎씩 만들어 한다.
 *       평소 수집·등록이 대기하면 워커가 멈추고 양보한다 (worker backfill.py).</li>
 *   <li>기간 묶음: 기본 {@code sensing.backfill.segment-months}(2)개월. 워커가 보관 사이트맵의 단위(한 달·두 달·석 달 파일)를 보면
 *       그 단위로 바꾼다. 시작 날짜는 묶음의 첫날로 맞춘다. 묶음마다 상태·수가 남고, 끊기면 남은 묶음부터 잇는다.</li>
 *   <li>기간: 기본 최근 3개월, 끝 날짜는 오늘(KST)까지. 길이 상한 {@code sensing.backfill.max-days}(366일)은 등록 담당자에게만
 *       적용하고 관리자는 더 길게(1990년부터) 정할 수 있다. "두 달 더"(extend)는 가장 오래된 묶음 앞에 묶음을 더한다.</li>
 *   <li>사이트 수집 소스(kind SITE, ACTIVE, API 아닌 설정)만. 소스당 진행 중인 채우기는 하나.</li>
 * </ul>
 */
@Service
public class BackfillService {

    public static final ZoneId KST = ZoneId.of("Asia/Seoul");
    public static final int DEFAULT_MONTHS = 3;
    public static final LocalDate EARLIEST = LocalDate.of(1990, 1, 1);

    private final JdbcClient jdbc;
    private final AuditLogger audit;
    private final int maxDays;
    private final int segmentMonths;

    public BackfillService(JdbcClient jdbc, AuditLogger audit,
                           @Value("${sensing.backfill.max-days:366}") int maxDays,
                           @Value("${sensing.backfill.segment-months:2}") int segmentMonths) {
        this.jdbc = jdbc;
        this.audit = audit;
        this.maxDays = maxDays;
        this.segmentMonths = Math.max(1, Math.min(12, segmentMonths));
    }

    /**
     * 기간 묶음 하나. label은 "2026-09~10", percent는 찾은 주소 중 처리한 비율(찾은 것이 없으면 0, 완료면 100).
     */
    public record SegmentView(long id, int seq, LocalDate from, LocalDate to, String label, String status, String method,
                              int found, int saved, int skipped, int outOfRange, int failed, int percent, String message,
                              OffsetDateTime startedAt, OffsetDateTime finishedAt) {
    }

    /**
     * remaining: 아직 열지 않은 주소(PENDING). requests: 사이트에 보낸 요청 수. chunks: 실행한 작업 수.
     * unitMonths: 기간 묶음 길이(개월). segments: 최근 묶음부터 (워커가 사이트맵 색인을 읽은 뒤 생긴다).
     * message: 워커가 남긴 진행·종료 설명.
     */
    public record BackfillView(long id, long sourceId, LocalDate from, LocalDate to, String status, String method,
                               int found, int saved, int skipped, int outOfRange, int failed, long remaining,
                               int requests, int chunks, int unitMonths, List<SegmentView> segments, String message,
                               String requestedBy, OffsetDateTime createdAt, OffsetDateTime startedAt,
                               OffsetDateTime finishedAt, OffsetDateTime updatedAt) {
    }

    private record Row(long id, long sourceId, LocalDate from, LocalDate to, String status, String method, int found,
                       int saved, int skipped, int outOfRange, int failed, long remaining, int requests, int chunks,
                       int unitMonths, int unitOffset, String message, String requestedBy, OffsetDateTime createdAt,
                       OffsetDateTime startedAt, OffsetDateTime finishedAt, OffsetDateTime updatedAt) {
    }

    private static final String VIEW_SQL = """
            SELECT b.id, b.source_id, b.date_from, b.date_to, b.status, b.method, b.found_count, b.saved_count,
                   b.skipped_count, b.out_of_range_count, b.failed_count,
                   (SELECT count(*) FROM backfill_url u WHERE u.backfill_id = b.id AND u.status = 'PENDING') AS remaining,
                   b.request_count, b.chunk_count, b.unit_months, b.unit_offset, b.message, a.name AS requested_by,
                   b.created_at, b.started_at, b.finished_at, b.updated_at
            FROM backfill b LEFT JOIN app_user a ON a.id = b.requested_by
            """;

    /** 소스의 채우기 기록 (최신 순, 최근 20건), 묶음 포함. */
    public List<BackfillView> list(CurrentUser user, long sourceId) {
        user.require(Role.REGISTRAR);
        requireSource(sourceId);
        List<Row> rows = jdbc.sql(VIEW_SQL + " WHERE b.source_id = ? ORDER BY b.id DESC LIMIT 20")
                .param(sourceId).query(this::map).list();
        Map<Long, List<SegmentView>> segments = segments(rows.stream().map(Row::id).toList());
        return rows.stream().map(r -> view(r, segments.getOrDefault(r.id(), List.of()))).toList();
    }

    /** 채우기 요청. from·to를 비우면 최근 3개월(오늘까지). 시작 날짜는 기간 묶음의 첫날로 맞춘다. */
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
        checkLength(user, start, end);
        SourceRow s = requireSource(sourceId);
        if (!"SITE".equals(s.kind()) || "API".equals(s.method()) || !s.hasConfig()) {
            throw ApiException.conflict("사이트 수집 소스만 과거 기사를 채울 수 있습니다 (키워드 감시·공개 API·점검 소스 제외).");
        }
        if (!"ACTIVE".equals(s.status())) {
            throw ApiException.conflict("수집 중(ACTIVE)인 소스만 과거 기사를 채울 수 있습니다. 현재 상태: " + s.status());
        }
        requireNoOpen(sourceId, null);
        LocalDate aligned = segmentStart(start, segmentMonths, 0);
        long id = jdbc.sql("""
                INSERT INTO backfill (source_id, date_from, date_to, requested_by, unit_months, unit_offset, message)
                VALUES (?, ?, ?, ?, ?, 0, ?) RETURNING id
                """).params(sourceId, aligned, end, user.id(), segmentMonths,
                        "요청을 받았습니다. 워커가 한가할 때 최근 묶음부터 조금씩 진행합니다 (평소 수집이 먼저입니다).")
                .query(Long.class).single();
        audit.log(user.id(), "BACKFILL_REQUESTED", "source", sourceId,
                "{\"backfillId\":" + id + ",\"from\":\"" + aligned + "\",\"to\":\"" + end + "\"}");
        return get(id);
    }

    /**
     * 기간을 뒤로 늘린다: 가장 오래된 묶음 앞에 묶음 segments개(기본 1개 = "두 달 더")를 더한다. 끝났거나 취소한 채우기는 다시 연다
     * (그 소스에 진행 중인 다른 채우기가 없을 때). 등록 담당자는 길이 상한을 넘길 수 없다.
     */
    @Transactional
    public BackfillView extend(CurrentUser user, long sourceId, long backfillId, int segments) {
        user.require(Role.REGISTRAR);
        if (segments < 1 || segments > 120) {
            throw badRequest("segments는 1~120이어야 합니다.");
        }
        Row b = getRow(backfillId);
        if (b.sourceId() != sourceId) {
            throw ApiException.notFound("과거 기사 채우기");
        }
        LocalDate start = segmentStart(b.from(), b.unitMonths(), b.unitOffset());
        LocalDate newFrom = monthStart(monthIndex(start) - b.unitMonths() * segments);
        checkLength(user, newFrom, b.to());
        boolean open = "QUEUED".equals(b.status()) || "RUNNING".equals(b.status());
        if (!open) {
            requireNoOpen(sourceId, backfillId);
            SourceRow s = requireSource(sourceId);
            if (!"ACTIVE".equals(s.status())) {
                throw ApiException.conflict("수집 중(ACTIVE)인 소스만 과거 기사를 채울 수 있습니다. 현재 상태: " + s.status());
            }
        }
        jdbc.sql("""
                UPDATE backfill SET date_from = ?, updated_at = now(), message = ?,
                       status = CASE WHEN status IN ('QUEUED', 'RUNNING') THEN status
                                     WHEN started_at IS NULL THEN 'QUEUED' ELSE 'RUNNING' END,
                       finished_at = CASE WHEN status IN ('QUEUED', 'RUNNING') THEN finished_at END
                WHERE id = ?
                """).params(newFrom, "기간을 " + newFrom + "부터로 늘렸습니다. 더 오래된 묶음을 이어서 채웁니다.", backfillId).update();
        audit.log(user.id(), "BACKFILL_EXTENDED", "source", sourceId,
                "{\"backfillId\":" + backfillId + ",\"from\":\"" + newFrom + "\"}");
        return get(backfillId);
    }

    /** 취소. 이미 저장한 문서는 그대로 두고, 대기 중인 작업은 거둔다 (실행 중인 작업은 워커가 다음 기사 전에 멈춘다). */
    @Transactional
    public BackfillView cancel(CurrentUser user, long sourceId, long backfillId) {
        user.require(Role.REGISTRAR);
        Row b = getRow(backfillId);
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
        jdbc.sql("UPDATE backfill_segment SET status = 'QUEUED', updated_at = now() WHERE backfill_id = ? AND status = 'RUNNING'")
                .param(backfillId).update();
        jdbc.sql("""
                UPDATE crawl_job SET status = 'SUCCESS', finished_at = now(), note = '과거 기사 채우기 취소'
                WHERE backfill_id = ? AND status = 'QUEUED'
                """).param(backfillId).update();
        audit.log(user.id(), "BACKFILL_CANCELLED", "source", sourceId, "{\"backfillId\":" + backfillId + "}");
        return get(backfillId);
    }

    BackfillView get(long id) {
        Row r = getRow(id);
        return view(r, segments(List.of(id)).getOrDefault(id, List.of()));
    }

    // ------------------------------------------------------------------ 기간 묶음 (워커 backfill.py와 같은 계산)

    static int monthIndex(LocalDate d) {
        return d.getYear() * 12 + d.getMonthValue() - 1;
    }

    static LocalDate monthStart(int index) {
        return LocalDate.of(Math.floorDiv(index, 12), Math.floorMod(index, 12) + 1, 1);
    }

    /** d가 든 묶음의 첫날. 묶음은 monthIndex % unit == offset인 달에서 시작한다 (2/0: 1~2월, 3~4월 …). */
    public static LocalDate segmentStart(LocalDate d, int unit, int offset) {
        int i = monthIndex(d);
        return monthStart(i - Math.floorMod(i - offset, unit));
    }

    /** "2026-09~10", "2026-09", "2025-11~2026-01" */
    static String label(LocalDate from, LocalDate to) {
        if (from.getYear() == to.getYear() && from.getMonthValue() == to.getMonthValue()) {
            return "%d-%02d".formatted(from.getYear(), from.getMonthValue());
        }
        if (from.getYear() == to.getYear()) {
            return "%d-%02d~%02d".formatted(from.getYear(), from.getMonthValue(), to.getMonthValue());
        }
        return "%d-%02d~%d-%02d".formatted(from.getYear(), from.getMonthValue(), to.getYear(), to.getMonthValue());
    }

    // ------------------------------------------------------------------ 내부

    private void checkLength(CurrentUser user, LocalDate start, LocalDate end) {
        if (start.isBefore(EARLIEST)) {
            throw badRequest("시작 날짜는 " + EARLIEST + " 이후여야 합니다.");
        }
        if (!user.role().atLeast(Role.ADMIN) && ChronoUnit.DAYS.between(start, end) > maxDays) {
            throw badRequest("기간은 최대 " + maxDays + "일입니다. 더 긴 기간은 관리자가 요청하거나 나눠서 요청해 주세요.");
        }
    }

    private void requireNoOpen(long sourceId, Long except) {
        Long open = jdbc.sql("SELECT id FROM backfill WHERE source_id = ? AND status IN ('QUEUED', 'RUNNING') AND id <> ?")
                .params(sourceId, except == null ? -1L : except).query(Long.class).optional().orElse(null);
        if (open != null) {
            throw ApiException.conflict("이미 진행 중인 과거 기사 채우기(#" + open + ")가 있습니다. 끝나거나 취소한 뒤 다시 요청해 주세요.");
        }
    }

    private Row getRow(long id) {
        return jdbc.sql(VIEW_SQL + " WHERE b.id = ?").param(id).query(this::map).optional()
                .orElseThrow(() -> ApiException.notFound("과거 기사 채우기"));
    }

    private Map<Long, List<SegmentView>> segments(List<Long> backfillIds) {
        Map<Long, List<SegmentView>> out = new LinkedHashMap<>();
        if (backfillIds.isEmpty()) {
            return out;
        }
        String in = String.join(",", backfillIds.stream().map(x -> "?").toList());
        jdbc.sql("""
                SELECT backfill_id, id, seq, date_from, date_to, status, method, found_count, saved_count, skipped_count,
                       out_of_range_count, failed_count, message, started_at, finished_at
                FROM backfill_segment WHERE backfill_id IN (%s) ORDER BY backfill_id, seq
                """.formatted(in)).params(new ArrayList<Object>(backfillIds)).query(rs -> {
            LocalDate from = rs.getObject("date_from", LocalDate.class);
            LocalDate to = rs.getObject("date_to", LocalDate.class);
            int found = rs.getInt("found_count");
            int handled = rs.getInt("saved_count") + rs.getInt("skipped_count") + rs.getInt("out_of_range_count")
                    + rs.getInt("failed_count");
            String status = rs.getString("status");
            int percent = "DONE".equals(status) ? 100 : found == 0 ? 0 : Math.min(99, handled * 100 / found);
            out.computeIfAbsent(rs.getLong("backfill_id"), k -> new ArrayList<>()).add(new SegmentView(
                    rs.getLong("id"), rs.getInt("seq"), from, to, label(from, to), status, rs.getString("method"), found,
                    rs.getInt("saved_count"), rs.getInt("skipped_count"), rs.getInt("out_of_range_count"),
                    rs.getInt("failed_count"), percent, rs.getString("message"),
                    rs.getObject("started_at", OffsetDateTime.class), rs.getObject("finished_at", OffsetDateTime.class)));
        });
        return out;
    }

    private BackfillView view(Row r, List<SegmentView> segments) {
        return new BackfillView(r.id(), r.sourceId(), r.from(), r.to(), r.status(), r.method(), r.found(), r.saved(),
                r.skipped(), r.outOfRange(), r.failed(), r.remaining(), r.requests(), r.chunks(), r.unitMonths(), segments,
                r.message(), r.requestedBy(), r.createdAt(), r.startedAt(), r.finishedAt(), r.updatedAt());
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

    private Row map(java.sql.ResultSet rs, int n) throws java.sql.SQLException {
        return new Row(rs.getLong("id"), rs.getLong("source_id"),
                rs.getObject("date_from", LocalDate.class), rs.getObject("date_to", LocalDate.class),
                rs.getString("status"), rs.getString("method"), rs.getInt("found_count"), rs.getInt("saved_count"),
                rs.getInt("skipped_count"), rs.getInt("out_of_range_count"), rs.getInt("failed_count"),
                rs.getLong("remaining"), rs.getInt("request_count"), rs.getInt("chunk_count"), rs.getInt("unit_months"),
                rs.getInt("unit_offset"), rs.getString("message"), rs.getString("requested_by"),
                rs.getObject("created_at", OffsetDateTime.class), rs.getObject("started_at", OffsetDateTime.class),
                rs.getObject("finished_at", OffsetDateTime.class), rs.getObject("updated_at", OffsetDateTime.class));
    }
}
