package io.sensinghub.document;

import java.sql.ResultSet;
import java.sql.SQLException;
import java.time.LocalDate;
import java.time.LocalDateTime;
import java.time.OffsetDateTime;
import java.time.ZoneId;
import java.time.format.DateTimeParseException;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import org.springframework.http.HttpStatus;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Service;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;

import io.sensinghub.auth.CurrentUser;
import io.sensinghub.auth.Role;
import io.sensinghub.common.ApiException;

/**
 * 수집 문서 조회 (M2). 워커가 붙인 요약·분류({@code document_enrichment})와 키워드 감시 기록({@code document_watch})을
 * 문서와 함께 돌려준다. 다이제스트(M2-4)와 화면이 이 API를 읽는다.
 * <ul>
 *   <li>언론 기사는 본문도 요약도 없다 (제목·링크·분류만). 공공기관·기타 문서는 summary와 bodyText가 있다.</li>
 *   <li>{@code since}는 수집 시각(collected_at) 기준이다: "지난 다이제스트 이후 새로 들어온 것"을 묻는 용도. 목록 정렬은 발행일 내림차순.</li>
 *   <li>아직 보강되지 않은 문서도 목록에 나온다 (summary null, topics·tags 빈 배열).</li>
 * </ul>
 */
@Service
public class DocumentService {

    static final int DEFAULT_LIMIT = 50;
    static final int MAX_LIMIT = 200;
    private static final TypeReference<List<String>> STRINGS = new TypeReference<>() { };

    public record DocumentRow(long id, String url, String title, OffsetDateTime publishedAt, OffsetDateTime collectedAt,
                              long sourceId, String domain, String section, String summary, List<String> topics,
                              List<String> tags, List<String> keywords) {
    }

    public record AttachmentRow(String url, String fileName) {
    }

    public record DocumentDetail(long id, String url, String title, OffsetDateTime publishedAt,
                                 OffsetDateTime collectedAt, long sourceId, String domain, String section,
                                 String summary, List<String> topics, List<String> tags, List<String> keywords,
                                 String bodyText, List<AttachmentRow> attachments) {
    }

    private final JdbcClient jdbc;
    private final ObjectMapper json;

    public DocumentService(JdbcClient jdbc, ObjectMapper json) {
        this.jdbc = jdbc;
        this.json = json;
    }

    private static final String ROW_SQL = """
            SELECT d.id, d.url, d.title, d.published_at, d.collected_at, d.source_id, t.domain, d.section,
                   e.summary, COALESCE(e.topics::text, '[]') AS topics, COALESCE(e.tags::text, '[]') AS tags,
                   (SELECT COALESCE(json_agg(w.keyword ORDER BY w.matched_at, w.source_id)
                                    FILTER (WHERE w.keyword IS NOT NULL), '[]'::json)::text
                    FROM document_watch w WHERE w.document_id = d.id) AS keywords
            FROM document d
            JOIN source s ON s.id = d.source_id
            JOIN site t ON t.id = s.site_id
            LEFT JOIN document_enrichment e ON e.document_id = d.id
            """;

    public List<DocumentRow> list(CurrentUser user, String since, Long sourceId, String topic, String keyword,
                                  Integer limit) {
        user.require(Role.VIEWER);
        StringBuilder sql = new StringBuilder(ROW_SQL).append(" WHERE 1 = 1");
        List<Object> params = new ArrayList<>();
        OffsetDateTime from = parseSince(since);
        if (from != null) {
            sql.append(" AND d.collected_at >= ?");
            params.add(from);
        }
        if (sourceId != null) {
            sql.append(" AND d.source_id = ?");
            params.add(sourceId);
        }
        if (topic != null && !topic.isBlank()) {
            sql.append(" AND e.topics @> CAST(? AS jsonb)");
            params.add(toJson(List.of(topic.trim())));
        }
        if (keyword != null && !keyword.isBlank()) {
            sql.append(" AND EXISTS (SELECT 1 FROM document_watch w WHERE w.document_id = d.id AND w.keyword = ?)");
            params.add(keyword.trim());
        }
        sql.append(" ORDER BY d.published_at DESC NULLS LAST, d.id DESC LIMIT ?");
        params.add(clampLimit(limit));
        return jdbc.sql(sql.toString()).params(params).query(this::mapRow).list();
    }

    public DocumentDetail detail(CurrentUser user, long id) {
        user.require(Role.VIEWER);
        DocumentRow row = jdbc.sql(ROW_SQL + " WHERE d.id = ?").param(id).query(this::mapRow).optional()
                .orElseThrow(() -> ApiException.notFound("문서"));
        String body = jdbc.sql("SELECT body_text FROM document WHERE id = ?").param(id).query(String.class)
                .optional().orElse(null);
        List<AttachmentRow> attachments = jdbc.sql(
                "SELECT url, file_name FROM attachment WHERE document_id = ? ORDER BY id").param(id)
                .query((rs, n) -> new AttachmentRow(rs.getString(1), rs.getString(2))).list();
        return new DocumentDetail(row.id(), row.url(), row.title(), row.publishedAt(), row.collectedAt(),
                row.sourceId(), row.domain(), row.section(), row.summary(), row.topics(), row.tags(), row.keywords(),
                body, attachments);
    }

    /** 토픽별 문서 수 (많은 순). since는 수집 시각 기준. */
    public Map<String, Long> topicCounts(CurrentUser user, String since) {
        user.require(Role.VIEWER);
        OffsetDateTime from = parseSince(since);
        String sql = """
                SELECT topic, count(*) AS c
                FROM document d
                JOIN document_enrichment e ON e.document_id = d.id
                CROSS JOIN LATERAL jsonb_array_elements_text(e.topics) AS topic
                """ + (from != null ? " WHERE d.collected_at >= ?" : "")
                + " GROUP BY topic ORDER BY c DESC, topic";
        Map<String, Long> counts = new LinkedHashMap<>();
        JdbcClient.StatementSpec spec = jdbc.sql(sql);
        if (from != null) {
            spec = spec.param(from);
        }
        spec.query(rs -> {
            counts.put(rs.getString(1), rs.getLong(2));
        });
        return counts;
    }

    /** ISO 시각(2026-10-15T09:00:00+09:00, ...Z), 시간대 없는 시각(서버 시간대), 날짜(2026-10-15)를 받는다. 비면 null. */
    /** since 파라미터 해석 (회사 신호 API도 같은 규칙을 쓴다). */
    public static OffsetDateTime parseSince(String since) {
        if (since == null || since.isBlank()) {
            return null;
        }
        String s = since.trim().replace(' ', '+');   // URL에서 인코딩하지 않은 '+'는 공백으로 들어온다
        try {
            return OffsetDateTime.parse(s);
        } catch (DateTimeParseException ignored) {
            // 아래 형식들을 차례로 본다
        }
        ZoneId zone = ZoneId.systemDefault();
        try {
            return LocalDateTime.parse(s).atZone(zone).toOffsetDateTime();
        } catch (DateTimeParseException ignored) {
            // 날짜만
        }
        try {
            return LocalDate.parse(s).atStartOfDay(zone).toOffsetDateTime();
        } catch (DateTimeParseException e) {
            throw new ApiException(HttpStatus.BAD_REQUEST, "BAD_REQUEST",
                    "since는 ISO 시각(예: 2026-10-15T00:00:00Z) 또는 날짜(2026-10-15)여야 합니다.");
        }
    }

    static int clampLimit(Integer limit) {
        if (limit == null) {
            return DEFAULT_LIMIT;
        }
        return Math.max(1, Math.min(MAX_LIMIT, limit));
    }

    private DocumentRow mapRow(ResultSet rs, int n) throws SQLException {
        return new DocumentRow(rs.getLong("id"), rs.getString("url"), rs.getString("title"),
                rs.getObject("published_at", OffsetDateTime.class), rs.getObject("collected_at", OffsetDateTime.class),
                rs.getLong("source_id"), rs.getString("domain"), rs.getString("section"), rs.getString("summary"),
                strings(rs.getString("topics")), strings(rs.getString("tags")), strings(rs.getString("keywords")));
    }

    private List<String> strings(String jsonArray) {
        if (jsonArray == null || jsonArray.isBlank()) {
            return List.of();
        }
        try {
            return json.readValue(jsonArray, STRINGS);
        } catch (JsonProcessingException e) {
            return List.of();
        }
    }

    private String toJson(Object value) {
        try {
            return json.writeValueAsString(value);
        } catch (JsonProcessingException e) {
            throw new IllegalStateException(e);
        }
    }
}
