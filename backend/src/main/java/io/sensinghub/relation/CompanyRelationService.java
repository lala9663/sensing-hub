package io.sensinghub.relation;

import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.stream.Collectors;

import org.springframework.http.HttpStatus;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;

import io.sensinghub.audit.AuditLogger;
import io.sensinghub.auth.CurrentUser;
import io.sensinghub.auth.Role;
import io.sensinghub.common.ApiException;
import io.sensinghub.watch.KeywordMatch;

/**
 * 회사 관계 목록 (자사·경쟁사·고객사·협력사). 문서 분류의 같은 이름 라벨을 LLM이 아니라 이 목록으로 붙인다:
 * 워커가 문서 제목·본문에서 이름·별칭을 키워드 감시와 같은 규칙(한글 부분 일치, 영문 단어 경계)으로 찾는다 (worker relations.py).
 * 누구나 읽고 관리자만 추가·수정·삭제한다. 바꿀 때마다 감사 로그(COMPANY_RELATION_CREATE·UPDATE·DELETE, 전후 값)를 남긴다.
 * 목록을 바꾸면 새로 수집·보강하는 문서부터 반영되고, 이미 보강한 문서는 워커의 다시 라벨링 명령으로 고친다.
 * 거래처(company) 마스터와는 따로다 (company는 공급사 리스크 대상, 이 목록은 고객 삼성전자 입장의 관계).
 */
@Service
public class CompanyRelationService {

    /** 관계 코드 → 분류 이름 (분류에 붙는 순서). */
    public static final Map<String, String> LABELS;

    static {
        Map<String, String> m = new LinkedHashMap<>();
        m.put("OWN", "자사");
        m.put("COMPETITOR", "경쟁사");
        m.put("CUSTOMER", "고객사");
        m.put("PARTNER", "협력사");
        LABELS = java.util.Collections.unmodifiableMap(m);
    }

    public static final int MAX_NAME = 100;      // document_relation.matched와 같다
    public static final int MAX_WORDS = 20;      // 별칭·제외어 각각
    public static final int MAX_NOTE = 300;
    public static final int MIN_NAME = 2;        // 한 글자 이름은 거의 모든 문서에 걸린다 ('삼' → '삼성'·'삼양'…)
    public static final int HISTORY = 30;

    public record RelationInput(String name, List<String> aliases, List<String> exclude, String relation, String note) {
    }

    public record RelationView(long id, String name, List<String> aliases, List<String> exclude, String relation,
                               String label, String note, OffsetDateTime updatedAt, String updatedBy) {
    }

    public record RelationList(List<RelationView> items, Map<String, String> relations) {
    }

    /** 감사 로그 한 줄: 누가 언제 무엇을 바꿨나 (detail은 before·after). */
    public record HistoryEntry(long id, OffsetDateTime at, String actor, String action, Long targetId, JsonNode detail) {
    }

    /** 감사 로그 detail에 남기는 값 (id·시각 없이 내용만). */
    record Snapshot(String name, List<String> aliases, List<String> exclude, String relation, String note) {
        static Snapshot of(RelationView v) {
            return new Snapshot(v.name(), v.aliases(), v.exclude(), v.relation(), v.note());
        }
    }

    private final JdbcClient jdbc;
    private final AuditLogger audit;
    private final ObjectMapper json;

    public CompanyRelationService(JdbcClient jdbc, AuditLogger audit, ObjectMapper json) {
        this.jdbc = jdbc;
        this.audit = audit;
        this.json = json;
    }

    public RelationList list(CurrentUser user) {
        user.require(Role.VIEWER);
        return new RelationList(items(), LABELS);
    }

    List<RelationView> items() {
        return jdbc.sql("""
                SELECT r.id, r.name, r.aliases::text AS aliases, r.exclude::text AS exclude, r.relation, r.note, r.updated_at,
                       u.name AS updated_by
                FROM company_relation r LEFT JOIN app_user u ON u.id = r.updated_by
                ORDER BY CASE r.relation WHEN 'OWN' THEN 0 WHEN 'COMPETITOR' THEN 1 WHEN 'CUSTOMER' THEN 2 ELSE 3 END,
                         lower(r.name), r.id
                """)
                .query((rs, n) -> new RelationView(rs.getLong("id"), rs.getString("name"), strings(rs.getString("aliases")),
                        strings(rs.getString("exclude")), rs.getString("relation"), LABELS.get(rs.getString("relation")),
                        rs.getString("note"), rs.getObject("updated_at", OffsetDateTime.class), rs.getString("updated_by")))
                .list();
    }

    /** 챗봇 프롬프트용 한 줄: "자사: 삼성전자 / 경쟁사: (아직 없음) / …". */
    public String summary() {
        Map<String, List<String>> names = new LinkedHashMap<>();
        LABELS.keySet().forEach(k -> names.put(k, new ArrayList<>()));
        items().forEach(v -> names.get(v.relation()).add(v.name()));
        return names.entrySet().stream()
                .map(e -> LABELS.get(e.getKey()) + ": " + (e.getValue().isEmpty() ? "(아직 없음)" : String.join(", ", e.getValue())))
                .collect(Collectors.joining(" / "));
    }

    @Transactional
    public RelationView create(CurrentUser admin, RelationInput input) {
        admin.require(Role.ADMIN);
        RelationInput in = clean(input);
        checkUnique(in, null);
        long id = jdbc.sql("""
                INSERT INTO company_relation (name, aliases, exclude, relation, note, created_by, updated_by)
                VALUES (?, CAST(? AS jsonb), CAST(? AS jsonb), ?, ?, ?, ?) RETURNING id
                """)
                .params(in.name(), toJson(in.aliases()), toJson(in.exclude()), in.relation(), in.note(), admin.id(), admin.id())
                .query(Long.class).single();
        RelationView created = get(id);
        audit.log(admin.id(), "COMPANY_RELATION_CREATE", "company_relation", id, toJson(Map.of("after", Snapshot.of(created))));
        return created;
    }

    @Transactional
    public RelationView update(CurrentUser admin, long id, RelationInput input) {
        admin.require(Role.ADMIN);
        RelationView before = get(id);
        RelationInput in = clean(input);
        checkUnique(in, id);
        Snapshot after = new Snapshot(in.name(), in.aliases(), in.exclude(), in.relation(), in.note());
        if (after.equals(Snapshot.of(before))) {
            return before;
        }
        jdbc.sql("""
                UPDATE company_relation SET name = ?, aliases = CAST(? AS jsonb), exclude = CAST(? AS jsonb), relation = ?,
                       note = ?, updated_by = ?, updated_at = now()
                WHERE id = ?
                """)
                .params(in.name(), toJson(in.aliases()), toJson(in.exclude()), in.relation(), in.note(), admin.id(), id)
                .update();
        Map<String, Object> detail = new LinkedHashMap<>();
        detail.put("before", Snapshot.of(before));
        detail.put("after", after);
        audit.log(admin.id(), "COMPANY_RELATION_UPDATE", "company_relation", id, toJson(detail));
        return get(id);
    }

    /** 지우면 수집 때 남긴 기록(document_relation)도 함께 지워진다. 이미 붙은 분류 라벨은 다시 라벨링 명령으로 고친다. */
    @Transactional
    public void delete(CurrentUser admin, long id) {
        admin.require(Role.ADMIN);
        RelationView before = get(id);
        jdbc.sql("DELETE FROM company_relation WHERE id = ?").param(id).update();
        audit.log(admin.id(), "COMPANY_RELATION_DELETE", "company_relation", id, toJson(Map.of("before", Snapshot.of(before))));
    }

    /** 이 목록의 최근 변경 (감사 로그, 새것부터). */
    public List<HistoryEntry> history(CurrentUser admin) {
        admin.require(Role.ADMIN);
        return jdbc.sql("""
                SELECT a.id, a.created_at, u.name AS actor, a.action, a.target_id, a.detail::text AS detail
                FROM audit_log a LEFT JOIN app_user u ON u.id = a.actor_id
                WHERE a.target_type = 'company_relation'
                ORDER BY a.id DESC LIMIT ?
                """)
                .param(HISTORY)
                .query((rs, n) -> new HistoryEntry(rs.getLong("id"), rs.getObject("created_at", OffsetDateTime.class),
                        rs.getString("actor"), rs.getString("action"), (Long) rs.getObject("target_id"),
                        readTree(rs.getString("detail"))))
                .list();
    }

    private RelationView get(long id) {
        return items().stream().filter(v -> v.id() == id).findFirst()
                .orElseThrow(() -> new ApiException(HttpStatus.NOT_FOUND, "NOT_FOUND", "회사 관계 항목이 없습니다: " + id));
    }

    // ------------------------------------------------------------------ 검사

    static RelationInput clean(RelationInput input) {
        if (input == null) {
            throw bad("이름과 관계를 입력해 주세요.");
        }
        String name = flat(input.name());
        if (name.isEmpty()) {
            throw bad("회사 이름을 입력해 주세요.");
        }
        checkWord("회사 이름", name);
        String relation = flat(input.relation()).toUpperCase(java.util.Locale.ROOT);
        if (!LABELS.containsKey(relation)) {
            throw bad("관계는 " + String.join("·", LABELS.keySet()) + " 중 하나여야 합니다 (자사·경쟁사·고객사·협력사).");
        }
        List<String> aliases = words("별칭", input.aliases(), List.of(name));
        List<String> exclude = words("제외어", input.exclude(), List.of());
        String note = flat(input.note());
        if (note.length() > MAX_NOTE) {
            throw bad("메모는 " + MAX_NOTE + "자 이하여야 합니다.");
        }
        return new RelationInput(name, aliases, exclude, relation, note.isEmpty() ? null : note);
    }

    /** 빈 값·중복(대소문자·공백 무시)·already에 있는 값은 빼고 순서는 유지한다. */
    private static List<String> words(String what, List<String> raw, List<String> already) {
        List<String> out = new ArrayList<>();
        List<String> seen = new ArrayList<>(already.stream().map(KeywordMatch::normalize).toList());
        for (String w : raw == null ? List.<String>of() : raw) {
            String s = flat(w);
            if (s.isEmpty() || seen.contains(KeywordMatch.normalize(s))) {
                continue;
            }
            checkWord(what, s);
            seen.add(KeywordMatch.normalize(s));
            out.add(s);
        }
        if (out.size() > MAX_WORDS) {
            throw bad(what + " 개수가 너무 많습니다 (" + MAX_WORDS + "개까지).");
        }
        return out;
    }

    private static void checkWord(String what, String s) {
        if (s.length() > MAX_NAME) {
            throw bad("너무 긴 " + what + "입니다 (" + MAX_NAME + "자 이하): " + s);
        }
        if (s.replace(" ", "").length() < MIN_NAME) {
            throw bad("너무 짧은 " + what + "입니다 (" + MIN_NAME + "자 이상 — 한 글자는 거의 모든 문서에 걸립니다): " + s);
        }
    }

    /** 이름·별칭은 목록 전체에서 하나뿐이어야 한다 (한 회사가 두 관계에 걸리면 라벨이 엇갈린다). */
    private void checkUnique(RelationInput in, Long selfId) {
        List<String> mine = new ArrayList<>();
        mine.add(in.name());
        mine.addAll(in.aliases());
        for (RelationView other : items()) {
            if (Objects.equals(other.id(), selfId)) {
                continue;
            }
            List<String> theirs = new ArrayList<>();
            theirs.add(other.name());
            theirs.addAll(other.aliases());
            for (String w : mine) {
                for (String t : theirs) {
                    if (KeywordMatch.normalize(w).equals(KeywordMatch.normalize(t))) {
                        throw bad("'" + w + "'은(는) 이미 '" + other.name() + "'(" + other.label() + ")의 이름·별칭입니다.");
                    }
                }
            }
        }
    }

    private static String flat(String s) {
        return s == null ? "" : s.replaceAll("\\s+", " ").strip();
    }

    private static ApiException bad(String message) {
        return new ApiException(HttpStatus.BAD_REQUEST, "BAD_REQUEST", message);
    }

    private List<String> strings(String jsonArray) {
        try {
            return jsonArray == null ? List.of() : json.readValue(jsonArray, new TypeReference<List<String>>() { });
        } catch (JsonProcessingException e) {
            throw new IllegalStateException(e);
        }
    }

    private JsonNode readTree(String s) {
        try {
            return s == null ? json.createObjectNode() : json.readTree(s);
        } catch (JsonProcessingException e) {
            throw new IllegalStateException(e);
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
