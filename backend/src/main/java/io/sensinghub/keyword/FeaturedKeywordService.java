package io.sensinghub.keyword;

import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Optional;
import java.util.Set;

import org.springframework.http.HttpStatus;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;

import io.sensinghub.audit.AuditLogger;
import io.sensinghub.auth.CurrentUser;
import io.sensinghub.auth.Role;
import io.sensinghub.common.ApiException;

/**
 * 주요 키워드 (네이버 뉴스 탭의 빠른 검색 칩, 챗봇 시작 화면의 칩). 누구나 읽고 관리자만 고친다.
 * 고칠 때는 목록 전체를 순서대로 받아 바꾼다 (추가·삭제·순서 변경이 한 번에 된다). 바꾼 전후 목록을 감사 로그에 남긴다.
 * 키워드 감시(source WATCH)·회사 감시와는 따로다.
 */
@Service
public class FeaturedKeywordService {

    public static final int MAX_ITEMS = 20;   // 네이버 뉴스 탭 칩 한도와 같다
    public static final int MAX_LABEL = 30;
    public static final int MAX_QUERY = 100;   // 네이버 뉴스 검색어 한도와 같다

    /** label: 칩 이름. query: 실제 검색어 (label과 다를 수 있다: 'DS' → '삼성전자 DS'). */
    public record FeaturedKeyword(String label, String query) {
    }

    public record KeywordList(List<FeaturedKeyword> items, OffsetDateTime updatedAt) {
    }

    private final JdbcClient jdbc;
    private final AuditLogger audit;
    private final ObjectMapper json;

    public FeaturedKeywordService(JdbcClient jdbc, AuditLogger audit, ObjectMapper json) {
        this.jdbc = jdbc;
        this.audit = audit;
        this.json = json;
    }

    public KeywordList list(CurrentUser user) {
        user.require(Role.VIEWER);
        OffsetDateTime updatedAt = jdbc.sql("SELECT max(updated_at) AS at FROM featured_keyword")
                .query((rs, n) -> rs.getObject("at", OffsetDateTime.class)).list().get(0);
        return new KeywordList(items(), updatedAt);
    }

    List<FeaturedKeyword> items() {
        return jdbc.sql("SELECT label, query FROM featured_keyword ORDER BY position, id")
                .query((rs, n) -> new FeaturedKeyword(rs.getString("label"), rs.getString("query"))).list();
    }

    /** 챗봇: 사용자가 칩 이름을 그대로 쳤으면(대소문자 무시) 그 칩의 검색어. */
    public Optional<FeaturedKeyword> byLabel(String label) {
        if (label == null || label.isBlank()) {
            return Optional.empty();
        }
        return jdbc.sql("SELECT label, query FROM featured_keyword WHERE lower(label) = lower(?) LIMIT 1")
                .param(label.strip())
                .query((rs, n) -> new FeaturedKeyword(rs.getString("label"), rs.getString("query"))).optional();
    }

    /** 목록 전체를 이 순서로 바꾼다. query가 비면 label과 같게 둔다. */
    @Transactional
    public KeywordList replace(CurrentUser admin, List<FeaturedKeyword> requested) {
        admin.require(Role.ADMIN);
        List<FeaturedKeyword> cleaned = clean(requested);
        List<FeaturedKeyword> before = items();
        jdbc.sql("DELETE FROM featured_keyword").update();
        for (int i = 0; i < cleaned.size(); i++) {
            FeaturedKeyword k = cleaned.get(i);
            jdbc.sql("INSERT INTO featured_keyword (label, query, position, updated_by) VALUES (?, ?, ?, ?)")
                    .params(k.label(), k.query(), i, admin.id()).update();
        }
        if (!before.equals(cleaned)) {
            Map<String, Object> detail = new LinkedHashMap<>();
            detail.put("before", before);
            detail.put("after", cleaned);
            audit.log(admin.id(), "FEATURED_KEYWORDS_UPDATE", "featured_keyword", null, toJson(detail));
        }
        return list(admin);
    }

    static List<FeaturedKeyword> clean(List<FeaturedKeyword> requested) {
        List<FeaturedKeyword> in = requested == null ? List.of() : requested;
        if (in.size() > MAX_ITEMS) {
            throw bad("주요 키워드는 " + MAX_ITEMS + "개까지 둘 수 있습니다.");
        }
        List<FeaturedKeyword> out = new ArrayList<>();
        Set<String> seen = new HashSet<>();
        for (FeaturedKeyword k : in) {
            String label = flat(k == null ? null : k.label());
            String query = flat(k == null ? null : k.query());
            if (label.isEmpty()) {
                throw bad("키워드 이름을 입력해 주세요.");
            }
            if (label.length() > MAX_LABEL) {
                throw bad("키워드 이름은 " + MAX_LABEL + "자 이하여야 합니다: " + label);
            }
            if (query.isEmpty()) {
                query = label;
            }
            if (query.length() > MAX_QUERY) {
                throw bad("검색어는 " + MAX_QUERY + "자 이하여야 합니다: " + label);
            }
            if (!seen.add(label.toLowerCase(Locale.ROOT))) {
                throw bad("같은 키워드가 두 번 있습니다: " + label);
            }
            out.add(new FeaturedKeyword(label, query));
        }
        return out;
    }

    private static String flat(String s) {
        return s == null ? "" : s.replaceAll("\\s+", " ").strip();
    }

    private static ApiException bad(String message) {
        return new ApiException(HttpStatus.BAD_REQUEST, "BAD_REQUEST", message);
    }

    private String toJson(Object value) {
        try {
            return json.writeValueAsString(value);
        } catch (JsonProcessingException e) {
            throw new IllegalStateException(e);
        }
    }
}
