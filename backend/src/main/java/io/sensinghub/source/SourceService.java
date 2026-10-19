package io.sensinghub.source;

import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;

import io.sensinghub.audit.AuditLogger;
import io.sensinghub.auth.CurrentUser;
import io.sensinghub.auth.Role;
import io.sensinghub.common.ApiException;

/** 관리자 화면: 소스 목록·상세·운영 조치·기사 종류 켜고 끄기. */
@Service
public class SourceService {

    private final JdbcClient jdbc;
    private final AuditLogger audit;
    private final ObjectMapper json;

    public SourceService(JdbcClient jdbc, AuditLogger audit, ObjectMapper json) {
        this.jdbc = jdbc;
        this.audit = audit;
        this.json = json;
    }

    /**
     * kind: SITE(사이트 수집) | WATCH(키워드 감시, watchName이 감시 이름) | CHECK(회사 단위 점검, M3).
     * intervalLocked: 사람이 PUT으로 주기를 정해 워커의 주기 자동 조정이 건드리지 않는 소스.
     */
    public record SourceSummary(long id, String kind, String watchName, String domain, String baseUrl, String siteType,
                                String method, String status,
                                String statusReason, Integer configVersion, int intervalMinutes, boolean intervalLocked,
                                OffsetDateTime lastSuccessAt, OffsetDateTime nextRunAt, int consecutiveFailures,
                                int consecutiveAnomalies, long documentCount, boolean aiRestricted) {
    }

    /** note: 워커가 실행에 남긴 메모 (예: 주기 자동 조정 60→120분). */
    public record JobRow(long id, String jobType, String trigger, String status, Integer configVersion,
                         int foundCount, int newCount, int errorCount,
                         OffsetDateTime startedAt, OffsetDateTime finishedAt, String note) {
    }

    /** evidencePath: 워커 증거 폴더(SENSING_EVIDENCE_DIR) 기준 상대 경로. 증거가 없으면 null. */
    public record ErrorRow(long jobId, String url, String stage, String code, Integer httpStatus, String message,
                           String evidencePath, OffsetDateTime createdAt) {
    }

    public record ConfigRow(long id, int version, String status, String changeNote, String config,
                            OffsetDateTime createdAt) {
    }

    /**
     * 활성 설정의 기사 종류(상세 URL 모양) 하나. 워커가 등록·다시 탐색 때 설정 article_patterns에 적는다.
     * count: 탐색 때 보인 서로 다른 주소 수, quality: 품질 검사 설명, note: 꺼진 이유나 관리자 변경.
     */
    public record KindRow(String name, String signature, List<String> hubs, boolean enabled, Integer count,
                          String quality, String note) {
    }

    /**
     * kinds: 활성 설정의 기사 종류. 종류 목록이 없는 예전 설정이면 article_pattern 하나를 이름 없이 보여 주고
     * kindsEditable = false (켜고 끄려면 '다시 탐색'으로 종류를 잡는다).
     */
    public record SourceDetail(SourceSummary source, List<JobRow> jobs, List<ErrorRow> recentErrors,
                               List<ConfigRow> configs, List<KindRow> kinds, boolean kindsEditable) {
    }

    private static final String SUMMARY_SQL = """
            SELECT s.id, s.kind, s.watch_name, t.domain, t.base_url, t.site_type, s.method, s.status, s.status_reason,
                   v.version AS config_version, s.interval_minutes, s.interval_locked, s.last_success_at, s.next_run_at,
                   s.consecutive_failures, s.consecutive_anomalies, t.ai_restricted,
                   (SELECT count(*) FROM document d WHERE d.source_id = s.id) AS document_count
            FROM source s
            JOIN site t ON t.id = s.site_id
            LEFT JOIN source_config_version v ON v.id = s.active_config_version_id
            """;

    public List<SourceSummary> list(CurrentUser user, String status) {
        user.require(Role.ADMIN);
        if (status == null || status.isBlank()) {
            return jdbc.sql(SUMMARY_SQL + " ORDER BY s.id").query(this::mapSummary).list();
        }
        return jdbc.sql(SUMMARY_SQL + " WHERE s.status = ? ORDER BY s.id").param(status).query(this::mapSummary).list();
    }

    public Map<String, Long> statusCounts(CurrentUser user) {
        user.require(Role.ADMIN);
        Map<String, Long> counts = new java.util.LinkedHashMap<>();
        jdbc.sql("SELECT status, count(*) FROM source GROUP BY status ORDER BY status")
                .query(rs -> {
                    counts.put(rs.getString(1), rs.getLong(2));
                });
        return counts;
    }

    public SourceDetail detail(CurrentUser user, long id) {
        user.require(Role.ADMIN);
        SourceSummary summary = jdbc.sql(SUMMARY_SQL + " WHERE s.id = ?").param(id).query(this::mapSummary)
                .optional().orElseThrow(() -> ApiException.notFound("소스"));
        List<JobRow> jobs = jdbc.sql("""
                SELECT j.id, j.job_type, j.trigger, j.status, v.version, j.found_count, j.new_count, j.error_count,
                       j.started_at, j.finished_at, j.note
                FROM crawl_job j LEFT JOIN source_config_version v ON v.id = j.config_version_id
                WHERE j.source_id = ? ORDER BY j.id DESC LIMIT 20
                """).param(id).query((rs, n) -> new JobRow(rs.getLong(1), rs.getString(2), rs.getString(3),
                        rs.getString(4), (Integer) rs.getObject(5), rs.getInt(6), rs.getInt(7), rs.getInt(8),
                        rs.getObject(9, OffsetDateTime.class), rs.getObject(10, OffsetDateTime.class),
                        rs.getString(11))).list();
        List<ErrorRow> errors = jdbc.sql("""
                SELECT e.job_id, e.url, e.stage, e.code, e.http_status, e.message, e.evidence_path, e.created_at
                FROM crawl_error e JOIN crawl_job j ON j.id = e.job_id
                WHERE j.source_id = ? ORDER BY e.id DESC LIMIT 20
                """).param(id).query((rs, n) -> new ErrorRow(rs.getLong(1), rs.getString(2), rs.getString(3),
                        rs.getString(4), (Integer) rs.getObject(5), rs.getString(6), rs.getString(7),
                        rs.getObject(8, OffsetDateTime.class))).list();
        List<ConfigRow> configs = jdbc.sql("""
                SELECT id, version, status, change_note, config::text, created_at
                FROM source_config_version WHERE source_id = ? ORDER BY version DESC
                """).param(id).query((rs, n) -> new ConfigRow(rs.getLong(1), rs.getInt(2), rs.getString(3),
                        rs.getString(4), rs.getString(5), rs.getObject(6, OffsetDateTime.class))).list();
        JsonNode active = configs.stream().filter(c -> "ACTIVE".equals(c.status())).findFirst()
                .map(c -> readJson(c.config())).orElse(null);
        return new SourceDetail(summary, jobs, errors, configs, kindsOf(active), kindArray(active) != null);
    }

    /** 설정의 기사 종류. article_patterns가 없는 예전 설정(또는 키워드 감시·API 설정)은 article_pattern 하나 또는 빈 목록. */
    static List<KindRow> kindsOf(JsonNode config) {
        if (config == null) {
            return List.of();
        }
        ArrayNode kinds = kindArray(config);
        if (kinds != null) {
            List<KindRow> rows = new ArrayList<>();
            for (JsonNode k : kinds) {
                rows.add(new KindRow(text(k, "name"), signatureOf(k), strings(k.path("hubs")),
                        k.path("enabled").asBoolean(true), k.hasNonNull("count") ? k.get("count").asInt() : null,
                        text(k, "quality"), text(k, "note")));
            }
            return rows;
        }
        JsonNode single = config.path("article_pattern");
        if (!single.isObject()) {
            return List.of();
        }
        return List.of(new KindRow(null, signatureOf(single), strings(config.path("hubs")), true, null, null,
                "예전 설정이라 종류 목록이 없습니다. '다시 탐색'을 하면 사이트의 기사 종류를 찾아 새 설정 버전을 만듭니다."));
    }

    private static ArrayNode kindArray(JsonNode config) {
        JsonNode kinds = config == null ? null : config.get("article_patterns");
        return kinds instanceof ArrayNode array && !array.isEmpty() ? array : null;
    }

    private static String signatureOf(JsonNode pattern) {
        return pattern.hasNonNull("signature") ? pattern.get("signature").asText() : text(pattern, "path");
    }

    private static String text(JsonNode node, String field) {
        return node.hasNonNull(field) ? node.get(field).asText() : null;
    }

    private static List<String> strings(JsonNode array) {
        List<String> out = new ArrayList<>();
        array.forEach(n -> out.add(n.asText()));
        return out;
    }

    private JsonNode readJson(String text) {
        try {
            return text == null ? null : json.readTree(text);
        } catch (JsonProcessingException e) {
            return null;
        }
    }

    /**
     * 기사 종류 켜고 끄기 (ADMIN). 활성 설정을 복사해 그 종류의 enabled만 바꾼 새 설정 버전을 활성화한다 (이전 버전은 RETIRED,
     * 롤백은 설정 버전 활성화로). 워커는 다음 수집부터 켜진 종류만 모은다. 종류는 서명(signature)으로 고른다.
     * 처음 자동으로 정해진 이유(note)는 auto_note로 옮기고 note에 관리자 변경을 적는다. 켜진 종류가 하나도 없게는 못 한다.
     */
    @Transactional
    public SourceDetail setKindEnabled(CurrentUser user, long id, String signature, boolean enabled) {
        user.require(Role.ADMIN);
        List<Map<String, Object>> rows = jdbc.sql("""
                SELECT s.kind, v.config::text AS config FROM source s
                LEFT JOIN source_config_version v ON v.id = s.active_config_version_id
                WHERE s.id = ? FOR UPDATE OF s
                """).param(id).query().listOfRows();
        if (rows.isEmpty()) {
            throw ApiException.notFound("소스");
        }
        Map<String, Object> row = rows.get(0);
        JsonNode config = readJson((String) row.get("config"));
        ArrayNode kinds = kindArray(config);
        if (!"SITE".equals(row.get("kind")) || kinds == null) {
            throw ApiException.conflict("이 소스의 설정에는 기사 종류 목록이 없습니다. '다시 탐색'을 하면 기사 종류를 찾습니다.");
        }
        ObjectNode target = null;
        int enabledCount = 0;
        for (JsonNode k : kinds) {
            if (k.path("enabled").asBoolean(true)) {
                enabledCount++;
            }
            if (signature.equals(signatureOf(k))) {
                target = (ObjectNode) k;
            }
        }
        if (target == null) {
            throw ApiException.notFound("기사 종류");
        }
        if (target.path("enabled").asBoolean(true) == enabled) {
            return detail(user, id);
        }
        if (!enabled && enabledCount <= 1) {
            throw ApiException.conflict("켜진 기사 종류가 하나는 있어야 합니다.");
        }
        if (!target.has("auto_note")) {
            target.put("auto_note", text(target, "note"));
        }
        target.put("enabled", enabled);
        target.put("note", enabled ? "관리자가 켬" : "관리자가 끔");
        String name = target.hasNonNull("name") ? target.get("name").asText() : signature;
        int version = jdbc.sql("SELECT COALESCE(MAX(version), 0) + 1 FROM source_config_version WHERE source_id = ?")
                .param(id).query(Integer.class).single();
        jdbc.sql("UPDATE source_config_version SET status = 'RETIRED' WHERE source_id = ? AND status = 'ACTIVE'")
                .param(id).update();
        long configId = jdbc.sql("""
                INSERT INTO source_config_version (source_id, version, config, status, change_note, created_by)
                VALUES (?, ?, CAST(? AS jsonb), 'ACTIVE', ?, ?) RETURNING id
                """).params(id, version, config.toString(),
                        "기사 종류 '" + name + "' " + (enabled ? "켬" : "끔") + " (관리자)", user.id())
                .query(Long.class).single();
        jdbc.sql("UPDATE source SET active_config_version_id = ?, updated_at = now() WHERE id = ?")
                .params(configId, id).update();
        ObjectNode detail = json.createObjectNode().put("signature", signature).put("name", name)
                .put("enabled", enabled).put("version", version);
        audit.log(user.id(), enabled ? "SOURCE_KIND_ENABLED" : "SOURCE_KIND_DISABLED", "source", id, detail.toString());
        return detail(user, id);
    }

    public enum Action { PAUSE, RESUME, RUN, RECHECK, REDISCOVER, EXCLUDE }

    /**
     * 운영 조치. BLOCKED는 자동으로 풀리지 않고, 관리자의 RECHECK(1회 재점검)로만 다시 시도한다.
     */
    @Transactional
    public SourceSummary act(CurrentUser user, long id, Action action) {
        user.require(Role.ADMIN);
        SourceSummary s = detail(user, id).source();
        switch (action) {
            case PAUSE -> {
                requireStatus(s, "ACTIVE", "BROKEN");
                setStatus(id, "PAUSED", "관리자 일시정지");
            }
            case RESUME -> {
                requireStatus(s, "PAUSED");
                jdbc.sql("UPDATE source SET status = 'ACTIVE', status_reason = NULL, next_run_at = now(), updated_at = now() WHERE id = ?")
                        .param(id).update();
            }
            case RUN -> {
                requireStatus(s, "ACTIVE");
                enqueue(id, "CRAWL", "MANUAL");
            }
            case RECHECK -> {
                requireStatus(s, "BLOCKED", "BROKEN");
                enqueue(id, "CRAWL", "MANUAL");
            }
            case REDISCOVER -> {
                requireStatus(s, "BROKEN", "REVIEW", "ACTIVE", "PAUSED");
                enqueue(id, "ONBOARD", "MANUAL");
            }
            case EXCLUDE -> {
                setStatus(id, "EXCLUDED", "관리자 제외");
                // 처리 대기(REVIEW) 중이던 등록 요청은 요청자에게 결과를 알린다
                jdbc.sql("""
                        UPDATE registration_request
                        SET status = 'REJECTED', step = 'REJECTED', updated_at = now(),
                            result_message = '관리자가 확인한 결과 수집 대상에서 제외했습니다.'
                        WHERE source_id = ? AND status IN ('RECEIVED', 'PROCESSING', 'REVIEW')
                        """).param(id).update();
            }
        }
        audit.log(user.id(), "SOURCE_" + action.name(), "source", id, null);
        return detail(user, id).source();
    }

    public static final int MIN_INTERVAL_MINUTES = 5;
    public static final int MAX_INTERVAL_MINUTES = 10080;

    /**
     * 소스 수정 (ADMIN): 수집 주기. 사람이 정한 주기는 interval_locked = true가 되어 워커의 주기 자동 조정이 덮지 않는다.
     * intervalLocked = false를 함께 보내면 값을 바꾸되 자동 조정에 다시 맡긴다. 주기가 바뀌면 다음 실행 시각도 새 주기로 잡는다.
     */
    @Transactional
    public SourceSummary update(CurrentUser user, long id, Integer intervalMinutes, Boolean intervalLocked) {
        user.require(Role.ADMIN);
        SourceSummary s = detail(user, id).source();
        if (intervalMinutes == null && intervalLocked == null) {
            throw new ApiException(org.springframework.http.HttpStatus.BAD_REQUEST, "BAD_REQUEST",
                    "intervalMinutes 또는 intervalLocked를 보내 주세요.");
        }
        if (intervalMinutes != null && (intervalMinutes < MIN_INTERVAL_MINUTES || intervalMinutes > MAX_INTERVAL_MINUTES)) {
            throw new ApiException(org.springframework.http.HttpStatus.BAD_REQUEST, "BAD_REQUEST",
                    "수집 주기는 " + MIN_INTERVAL_MINUTES + "~" + MAX_INTERVAL_MINUTES + "분 사이여야 합니다.");
        }
        boolean locked = intervalLocked != null ? intervalLocked : (intervalMinutes != null || s.intervalLocked());
        int interval = intervalMinutes != null ? intervalMinutes : s.intervalMinutes();
        jdbc.sql("""
                UPDATE source SET interval_minutes = ?, interval_locked = ?,
                       next_run_at = CASE WHEN ? <> interval_minutes AND status = 'ACTIVE'
                                          THEN LEAST(COALESCE(next_run_at, now()), now() + make_interval(mins => ?))
                                          ELSE next_run_at END,
                       updated_at = now()
                WHERE id = ?
                """).params(interval, locked, interval, interval, id).update();
        audit.log(user.id(), "SOURCE_UPDATED", "source", id,
                "{\"intervalMinutes\":" + interval + ",\"intervalLocked\":" + locked + "}");
        return detail(user, id).source();
    }

    /** 설정 버전 활성화(롤백 포함): 포인터만 바꾼다. */
    @Transactional
    public SourceSummary activateConfig(CurrentUser user, long id, int version) {
        user.require(Role.ADMIN);
        Long configId = jdbc.sql("SELECT id FROM source_config_version WHERE source_id = ? AND version = ?")
                .params(id, version).query(Long.class).optional()
                .orElseThrow(() -> ApiException.notFound("설정 버전"));
        jdbc.sql("UPDATE source_config_version SET status = 'RETIRED' WHERE source_id = ? AND status = 'ACTIVE'")
                .param(id).update();
        jdbc.sql("UPDATE source_config_version SET status = 'ACTIVE' WHERE id = ?").param(configId).update();
        jdbc.sql("UPDATE source SET active_config_version_id = ?, updated_at = now() WHERE id = ?")
                .params(configId, id).update();
        audit.log(user.id(), "CONFIG_ACTIVATED", "source", id, "{\"version\":" + version + "}");
        return detail(user, id).source();
    }

    private void enqueue(long sourceId, String jobType, String trigger) {
        // 과거 기사 채우기(BACKFILL) 묶음이 소스의 작업 자리를 잡고 있으면 양보시킨다: 대기 중이면 거둔다
        jdbc.sql("""
                UPDATE crawl_job SET status = 'SUCCESS', finished_at = now(), note = '관리자 작업에 양보'
                WHERE source_id = ? AND job_type = 'BACKFILL' AND status = 'QUEUED'
                """).param(sourceId).update();
        int inserted = jdbc.sql("""
                INSERT INTO crawl_job (source_id, job_type, config_version_id, trigger)
                SELECT id, ?, active_config_version_id, ? FROM source WHERE id = ?
                ON CONFLICT (source_id) WHERE status IN ('QUEUED', 'RUNNING') DO NOTHING
                """).params(jobType, trigger, sourceId).update();
        if (inserted == 0) {
            boolean backfillRunning = jdbc.sql("""
                    SELECT EXISTS (SELECT 1 FROM crawl_job WHERE source_id = ? AND job_type = 'BACKFILL' AND status = 'RUNNING')
                    """).param(sourceId).query(Boolean.class).single();
            if (backfillRunning && "CRAWL".equals(jobType)) {
                // 실행 중인 묶음: 주기 수집 차례를 지금으로 당긴다. 워커가 이것을 보고 묶음을 멈추면 스케줄러가 곧 CRAWL을 넣는다
                jdbc.sql("UPDATE source SET next_run_at = now(), updated_at = now() WHERE id = ?").param(sourceId).update();
                return;
            }
            if (backfillRunning) {
                throw ApiException.conflict("과거 기사 채우기가 실행 중입니다. 소스 상세에서 채우기를 취소한 뒤 다시 시도해 주세요.");
            }
            throw ApiException.conflict("이미 실행 중이거나 대기 중인 작업이 있습니다.");
        }
    }

    private void setStatus(long id, String status, String reason) {
        jdbc.sql("UPDATE source SET status = ?, status_reason = ?, updated_at = now() WHERE id = ?")
                .params(status, reason, id).update();
    }

    private static void requireStatus(SourceSummary s, String... allowed) {
        for (String a : allowed) {
            if (a.equals(s.status())) {
                return;
            }
        }
        throw ApiException.conflict("현재 상태(" + s.status() + ")에서는 할 수 없는 조치입니다.");
    }

    private SourceSummary mapSummary(java.sql.ResultSet rs, int n) throws java.sql.SQLException {
        return new SourceSummary(rs.getLong("id"), rs.getString("kind"), rs.getString("watch_name"),
                rs.getString("domain"), rs.getString("base_url"),
                rs.getString("site_type"), rs.getString("method"), rs.getString("status"),
                rs.getString("status_reason"), (Integer) rs.getObject("config_version"),
                rs.getInt("interval_minutes"), rs.getBoolean("interval_locked"),
                rs.getObject("last_success_at", OffsetDateTime.class),
                rs.getObject("next_run_at", OffsetDateTime.class), rs.getInt("consecutive_failures"),
                rs.getInt("consecutive_anomalies"), rs.getLong("document_count"), rs.getBoolean("ai_restricted"));
    }
}
