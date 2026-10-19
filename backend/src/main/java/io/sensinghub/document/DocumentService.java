package io.sensinghub.document;

import java.sql.ResultSet;
import java.sql.SQLException;
import java.time.LocalDate;
import java.time.LocalDateTime;
import java.time.OffsetDateTime;
import java.time.ZoneId;
import java.time.format.DateTimeParseException;
import java.util.ArrayList;
import java.util.Collection;
import java.util.Collections;
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

    /** issueId·issueDocCount: 워커가 묶은 이슈 (아직 색인 전이거나 제목 낱말이 모자라면 null). */
    public record DocumentRow(long id, String url, String title, OffsetDateTime publishedAt, OffsetDateTime collectedAt,
                              long sourceId, String domain, String section, String summary, List<String> topics,
                              List<String> tags, List<String> keywords, Long issueId, Integer issueDocCount) {
    }

    public record AttachmentRow(String url, String fileName) {
    }

    public record DocumentDetail(long id, String url, String title, OffsetDateTime publishedAt,
                                 OffsetDateTime collectedAt, long sourceId, String domain, String section,
                                 String summary, List<String> topics, List<String> tags, List<String> keywords,
                                 Long issueId, Integer issueDocCount, String bodyText, List<AttachmentRow> attachments) {
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
                    FROM document_watch w WHERE w.document_id = d.id) AS keywords,
                   idoc.issue_id, i.doc_count AS issue_doc_count
            FROM document d
            JOIN source s ON s.id = d.source_id
            JOIN site t ON t.id = s.site_id
            LEFT JOIN document_enrichment e ON e.document_id = d.id
            LEFT JOIN issue_document idoc ON idoc.document_id = d.id
            LEFT JOIN issue i ON i.id = idoc.issue_id
            """;

    /**
     * watchId: 그 감시(document_watch.source_id)에 걸린 문서만. keyword와 함께 주면 그 감시에서 그 검색어로 걸린 문서만
     * (document_watch 한 행이 둘 다 만족해야 한다). 문서 목록은 VIEWER 누구나 읽으므로 감시 소유자를 따지지 않는다.
     */
    public List<DocumentRow> list(CurrentUser user, String since, Long sourceId, String topic, Long watchId,
                                  String keyword, String q, Integer limit) {
        user.require(Role.VIEWER);
        StringBuilder sql = new StringBuilder(ROW_SQL).append(" WHERE 1 = 1");
        List<Object> params = new ArrayList<>();
        appendFilters(sql, params, since, sourceId, topic, watchId, keyword);
        if (q != null && !q.isBlank()) {
            appendSearch(sql, params, q);
        }
        sql.append(" ORDER BY d.published_at DESC NULLS LAST, d.id DESC LIMIT ?");
        params.add(clampLimit(limit));
        return jdbc.sql(sql.toString()).params(params).query(this::mapRow).list();
    }

    /** 목록·의미 검색이 함께 쓰는 조건. 별칭 d(document)·e(document_enrichment)가 있어야 한다. */
    private void appendFilters(StringBuilder sql, List<Object> params, String since, Long sourceId, String topic,
                               Long watchId, String keyword) {
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
        boolean hasKeyword = keyword != null && !keyword.isBlank();
        if (watchId != null || hasKeyword) {
            sql.append(" AND EXISTS (SELECT 1 FROM document_watch w WHERE w.document_id = d.id");
            if (watchId != null) {
                sql.append(" AND w.source_id = ?");
                params.add(watchId);
            }
            if (hasKeyword) {
                sql.append(" AND w.keyword = ?");
                params.add(keyword.trim());
            }
            sql.append(")");
        }
    }

    /** 의미 검색 후보 한 건: 문서와 그 문서에서 질의에 가장 가까운 조각의 코사인 유사도. */
    public record SemanticMatch(long documentId, double similarity) {
    }

    /**
     * 의미 검색 후보 (챗봇 2단계, {@link HybridSearch}). 질의 벡터와 조각 벡터({@code chunk.embedding})의 코사인 유사도로
     * 문서마다 가장 가까운 조각을 고르고, 유사도 하한 이상을 높은 순으로 limit건 준다. 목록과 같은 조건(수집 시각·분류·감시)을 건다.
     * <p>{@code embed_model}이 질의를 만든 모델과 같은 조각만 비교한다. 조건을 건 채 문서마다 묶어야 하므로 HNSW 색인 대신 해당
     * 조각을 모두 계산한다 (조각 수만 건에서 수십 ms). 수십만 건을 넘으면 pgvector 0.8의 반복 탐색(hnsw.iterative_scan)으로 바꾼다.
     */
    List<SemanticMatch> semanticMatches(CurrentUser user, String since, String topic, Long watchId, float[] vector,
                                        String model, int limit, double minSimilarity) {
        user.require(Role.VIEWER);
        StringBuilder sql = new StringBuilder("""
                SELECT id, similarity FROM (
                    SELECT d.id, max(1 - (c.embedding <=> q.v)) AS similarity
                    FROM (SELECT CAST(? AS vector) AS v) q
                    JOIN chunk c ON c.embed_model = ? AND c.embedding IS NOT NULL
                    JOIN document d ON d.id = c.document_id
                    LEFT JOIN document_enrichment e ON e.document_id = d.id
                    WHERE 1 = 1
                """);
        List<Object> params = new ArrayList<>();
        params.add(vectorLiteral(vector));
        params.add(model);
        appendFilters(sql, params, since, null, topic, watchId, null);
        sql.append("""
                    GROUP BY d.id
                ) s
                WHERE similarity >= ?
                ORDER BY similarity DESC, id DESC
                LIMIT ?
                """);
        params.add(minSimilarity);
        params.add(Math.max(1, limit));
        return jdbc.sql(sql.toString()).params(params)
                .query((rs, n) -> new SemanticMatch(rs.getLong("id"), rs.getDouble("similarity"))).list();
    }

    /** 이 모델로 만든 조각 벡터가 하나라도 있는가 (없으면 질의를 임베딩하지 않는다 — 호출 한도를 아낀다). */
    boolean hasEmbeddings(String model) {
        return Boolean.TRUE.equals(jdbc.sql(
                "SELECT EXISTS (SELECT 1 FROM chunk WHERE embed_model = ? AND embedding IS NOT NULL)")
                .param(model).query(Boolean.class).single());
    }

    /** 문서 id들의 목록 행 (순서는 보장하지 않는다). */
    List<DocumentRow> rows(Collection<Long> ids) {
        if (ids.isEmpty()) {
            return List.of();
        }
        String marks = String.join(", ", Collections.nCopies(ids.size(), "?"));
        return jdbc.sql(ROW_SQL + " WHERE d.id IN (" + marks + ")").params(new ArrayList<Object>(ids))
                .query(this::mapRow).list();
    }

    /** pgvector 입력 형식 '[0.1,0.2,...]'. */
    static String vectorLiteral(float[] vector) {
        StringBuilder sb = new StringBuilder(vector.length * 10).append('[');
        for (int i = 0; i < vector.length; i++) {
            if (i > 0) {
                sb.append(',');
            }
            sb.append(vector[i]);
        }
        return sb.append(']').toString();
    }

    /**
     * 검색어 q: 낱말(SearchTerms)마다 아래 중 하나를 만족해야 한다 (모든 낱말이 문서에 있어야 한다).
     * <ol>
     *   <li>전문 검색 색인: 문서의 조각(chunk) 중 하나가 그 낱말로 시작하는 낱말을 가진다 ({@code tokens @@ '낱말':*}, GIN).
     *       낱말마다 따로 EXISTS를 거는 이유: 제목(seq 0)과 본문 조각이 따로 있어서, 한 조각 안에 모든 낱말을 요구하면
     *       제목의 '삼성전자'와 본문의 '실적'을 함께 찾지 못한다.</li>
     *   <li>요약(document_enrichment.summary)에 부분 일치 — 요약은 색인하지 않는다.</li>
     *   <li>아직 색인되지 않은 문서(indexed_at IS NULL)는 예전처럼 제목·본문 부분 일치 (워커가 유휴 시간에 색인한다).</li>
     * </ol>
     * 글자·숫자가 하나도 없는 검색어는 통째로 부분 일치한다.
     */
    private static void appendSearch(StringBuilder sql, List<Object> params, String q) {
        List<String> terms = SearchTerms.terms(q);
        if (terms.isEmpty()) {
            String like = SearchTerms.likePattern(q.trim());
            sql.append(" AND (d.title ILIKE ? OR d.body_text ILIKE ? OR e.summary ILIKE ?)");
            params.add(like);
            params.add(like);
            params.add(like);
            return;
        }
        for (String term : terms) {
            String like = SearchTerms.likePattern(term);
            sql.append(" AND (EXISTS (SELECT 1 FROM chunk c WHERE c.document_id = d.id")
                    .append(" AND c.tokens @@ to_tsquery('simple', ?))")
                    .append(" OR e.summary ILIKE ?")
                    .append(" OR (d.indexed_at IS NULL AND (d.title ILIKE ? OR d.body_text ILIKE ?)))");
            params.add(SearchTerms.prefixQuery(term));
            params.add(like);
            params.add(like);
            params.add(like);
        }
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
                row.issueId(), row.issueDocCount(), body, attachments);
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
                strings(rs.getString("topics")), strings(rs.getString("tags")), strings(rs.getString("keywords")),
                rs.getObject("issue_id", Long.class), rs.getObject("issue_doc_count", Integer.class));
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
