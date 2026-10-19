package io.sensinghub.issue;

import java.sql.ResultSet;
import java.sql.SQLException;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.List;

import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Service;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;

import io.sensinghub.auth.CurrentUser;
import io.sensinghub.auth.Role;
import io.sensinghub.common.ApiException;
import io.sensinghub.document.DocumentService;
import io.sensinghub.document.SearchTerms;

/**
 * 이슈 조회 (M5 기반). 이슈는 워커(issues.py)가 문서를 색인한 직후 제목 낱말 겹침으로 만든 묶음이다:
 * 여러 언론사가 같은 사건을 보도한 기사 수십 건을 하나로 보여 준다. 이 서비스는 읽기만 한다.
 * <ul>
 *   <li>목록 정렬: 언론사 수 → 기사 수 → 마지막 기사 시각 (많은 곳이 다룬 이슈가 위).</li>
 *   <li>since는 이슈의 마지막 기사 시각(last_seen_at) 기준: "그 뒤에도 기사가 나온 이슈".</li>
 *   <li>min_docs 기본 2: 기사가 하나뿐인 이슈(대부분)는 빼고 본다.</li>
 *   <li>q: 대표 제목 부분 일치 또는 서명 낱말(top_terms) 접두 일치. 낱말이 여럿이면 모두 맞아야 한다.</li>
 * </ul>
 */
@Service
public class IssueService {

    static final int DEFAULT_LIMIT = 50;
    static final int MAX_LIMIT = 200;
    static final int MAX_DOCUMENTS = 500;
    private static final TypeReference<List<String>> STRINGS = new TypeReference<>() { };

    public record IssueRow(long id, String title, OffsetDateTime firstSeenAt, OffsetDateTime lastSeenAt, int docCount,
                           int sourceCount, List<String> topTerms, String status) {
    }

    public record IssueDocument(long id, String url, String title, String domain, String siteName, long sourceId,
                                OffsetDateTime publishedAt, double similarity) {
    }

    public record IssueDetail(long id, String title, OffsetDateTime firstSeenAt, OffsetDateTime lastSeenAt,
                              int docCount, int sourceCount, List<String> topTerms, String status,
                              List<IssueDocument> documents) {
    }

    private final JdbcClient jdbc;
    private final ObjectMapper json;

    public IssueService(JdbcClient jdbc, ObjectMapper json) {
        this.jdbc = jdbc;
        this.json = json;
    }

    private static final String ROW_SQL = """
            SELECT i.id, i.title, i.first_seen_at, i.last_seen_at, i.doc_count, i.source_count,
                   i.top_terms::text AS top_terms, i.status
            FROM issue i
            """;

    public List<IssueRow> list(CurrentUser user, String since, Integer minDocs, String q, Integer limit) {
        user.require(Role.VIEWER);
        StringBuilder sql = new StringBuilder(ROW_SQL).append(" WHERE i.doc_count >= ?");
        List<Object> params = new ArrayList<>();
        params.add(minDocs == null ? 2 : Math.max(1, minDocs));
        OffsetDateTime from = DocumentService.parseSince(since);
        if (from != null) {
            sql.append(" AND i.last_seen_at >= ?");
            params.add(from);
        }
        if (q != null && !q.isBlank()) {
            List<String> terms = SearchTerms.terms(q);
            if (terms.isEmpty()) {
                sql.append(" AND i.title ILIKE ?");
                params.add(SearchTerms.likePattern(q.trim()));
            }
            for (String term : terms) {
                sql.append(" AND (i.title ILIKE ? OR EXISTS (SELECT 1 FROM jsonb_array_elements_text(i.top_terms) AS t(term)")
                        .append(" WHERE t.term LIKE ?))");
                params.add(SearchTerms.likePattern(term));
                params.add(term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%");
            }
        }
        sql.append(" ORDER BY i.source_count DESC, i.doc_count DESC, i.last_seen_at DESC, i.id DESC LIMIT ?");
        params.add(limit == null ? DEFAULT_LIMIT : Math.max(1, Math.min(MAX_LIMIT, limit)));
        return jdbc.sql(sql.toString()).params(params).query(this::mapRow).list();
    }

    /** 이슈 + 소속 문서 (발행일 오름차순, 없으면 뒤로, 최대 500건). */
    public IssueDetail detail(CurrentUser user, long id) {
        user.require(Role.VIEWER);
        IssueRow row = jdbc.sql(ROW_SQL + " WHERE i.id = ?").param(id).query(this::mapRow).optional()
                .orElseThrow(() -> ApiException.notFound("이슈"));
        List<IssueDocument> documents = jdbc.sql("""
                SELECT d.id, d.url, d.title, t.domain, t.name AS site_name, d.source_id, d.published_at, idoc.similarity
                FROM issue_document idoc
                JOIN document d ON d.id = idoc.document_id
                JOIN source s ON s.id = d.source_id
                JOIN site t ON t.id = s.site_id
                WHERE idoc.issue_id = ?
                ORDER BY d.published_at ASC NULLS LAST, d.id
                LIMIT ?
                """).params(id, MAX_DOCUMENTS)
                .query((rs, n) -> new IssueDocument(rs.getLong("id"), rs.getString("url"), rs.getString("title"),
                        rs.getString("domain"), rs.getString("site_name"), rs.getLong("source_id"),
                        rs.getObject("published_at", OffsetDateTime.class), rs.getDouble("similarity")))
                .list();
        return new IssueDetail(row.id(), row.title(), row.firstSeenAt(), row.lastSeenAt(), row.docCount(),
                row.sourceCount(), row.topTerms(), row.status(), documents);
    }

    private IssueRow mapRow(ResultSet rs, int n) throws SQLException {
        return new IssueRow(rs.getLong("id"), rs.getString("title"), rs.getObject("first_seen_at", OffsetDateTime.class),
                rs.getObject("last_seen_at", OffsetDateTime.class), rs.getInt("doc_count"), rs.getInt("source_count"),
                strings(rs.getString("top_terms")), rs.getString("status"));
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
}
