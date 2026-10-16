package io.sensinghub.company;

import java.sql.ResultSet;
import java.sql.SQLException;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Optional;
import java.util.regex.Pattern;

import org.springframework.http.HttpStatus;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;

import io.sensinghub.audit.AuditLogger;
import io.sensinghub.auth.CurrentUser;
import io.sensinghub.auth.Role;
import io.sensinghub.common.ApiException;
import io.sensinghub.watch.WatchService;

/**
 * 회사(거래처) 마스터 (M3). 문서 중심 위에 "어느 회사에 무슨 일"을 한 층 얹는다.
 * <ul>
 *   <li>회사를 만들면 이름+별칭을 키워드로 한 네이버 뉴스 감시({@link WatchService#createForCompany}, "회사:&lt;이름&gt;", 360분)를
 *       만들어 {@code company.watch_source_id}에 둔다. 걸린 기사는 워커가 {@code document_company}(WATCH)로 연결하고,
 *       보강 토픽이 사건·사고·인사·조직이면 {@code company_signal}(NEWS)을 남긴다. 네이버 키가 없으면 감시는 API_KEY_REQUIRED 큐에 간다.</li>
 *   <li>처음 회사를 만들 때 국세청 사업자등록 상태를 하루 1회 확인하는 점검 소스({@code source.kind = CHECK}, site api.odcloud.kr,
 *       config {@code {"provider":"data_go_kr","check":"nts_status"}})를 하나 만든다. 공공데이터포털 서비스키가 없으면 그것도 큐에 간다.</li>
 *   <li>사업자등록번호는 숫자 10자리로 정규화(하이픈 제거)하고 형식이 틀리면 400, 중복이면 409. import는 사업자번호가 같으면 갱신(upsert).</li>
 *   <li>삭제는 ARCHIVED 처리이고 그 감시는 EXCLUDED로 바뀐다. 신호·연결은 남는다.</li>
 * </ul>
 * 권한: 조회 VIEWER 이상, 등록·수정·삭제·import REGISTRAR 이상.
 */
@Service
public class CompanyService {

    public static final String CHECK_PROVIDER = "data_go_kr";
    public static final String CHECK_DOMAIN = "api.odcloud.kr";
    public static final String CHECK_BASE_URL = "https://api.odcloud.kr";
    public static final String CHECK_NAME = "check:nts_status";
    static final int CHECK_INTERVAL_MINUTES = 1440;
    static final int WATCH_INTERVAL_MINUTES = 360;
    static final int MAX_ALIASES = 49;   // 이름과 합쳐 감시 키워드 50개 이하
    static final int MAX_TAGS = 20;
    static final List<String> KINDS = List.of("BIZ_STATUS", "DEBARMENT", "DISCLOSURE", "NEWS", "FINANCIAL");
    static final List<String> SEVERITIES = List.of("HIGH", "MEDIUM", "LOW");
    private static final Pattern BIZ_NO = Pattern.compile("\\d{10}");
    private static final Pattern CORP_CODE = Pattern.compile("\\d{8}");
    private static final TypeReference<List<String>> STRINGS = new TypeReference<>() { };
    private static final TypeReference<Map<String, Object>> OBJECT = new TypeReference<>() { };

    /** 등록·수정·import 입력 한 건. null 필드는 수정 시 "그대로"다. */
    public record CompanyInput(String name, List<String> aliases, String bizNo, String corpCode, String externalId,
                               List<String> tags) {
    }

    public record SignalBrief(long id, String kind, String severity, String title, OffsetDateTime observedAt) {
    }

    public record CompanyView(long id, String name, List<String> aliases, String bizNo, String corpCode, String externalId,
                              List<String> tags, String status, Long watchSourceId, String watchStatus,
                              String watchStatusReason, Map<String, Object> lastBizStatus, long signalCount,
                              long highSignalCount, SignalBrief lastSignal, long documentCount, String createdBy,
                              OffsetDateTime createdAt, OffsetDateTime updatedAt) {
    }

    public record SignalView(long id, long companyId, String companyName, String kind, String severity, String title,
                             Map<String, Object> detail, OffsetDateTime observedAt, Long documentId, String documentUrl,
                             String documentTitle, Long sourceId, OffsetDateTime createdAt) {
    }

    public record DocumentRef(long id, String url, String title, OffsetDateTime publishedAt, String domain,
                              String matchedBy, OffsetDateTime matchedAt, List<String> topics) {
    }

    public record CompanyDetail(CompanyView company, List<SignalView> signals, List<DocumentRef> documents) {
    }

    public record ImportResult(int created, int updated, List<Long> ids) {
    }

    private final JdbcClient jdbc;
    private final ObjectMapper json;
    private final AuditLogger audit;
    private final WatchService watches;

    public CompanyService(JdbcClient jdbc, ObjectMapper json, AuditLogger audit, WatchService watches) {
        this.jdbc = jdbc;
        this.json = json;
        this.audit = audit;
        this.watches = watches;
    }

    // ------------------------------------------------------------------ 등록·수정·보관

    @Transactional
    public CompanyView create(CurrentUser user, CompanyInput in) {
        user.require(Role.REGISTRAR);
        long id = insert(user, cleanForCreate(in));
        audit.log(user.id(), "COMPANY_CREATED", "company", id, "{\"name\":" + quote(get(user, id).name()) + "}");
        return get(user, id);
    }

    /**
     * 여러 건 한 번에. 사업자번호가 같은 회사가 있으면 갱신(upsert), 없으면 사업자번호 없이 이름이 같은 ACTIVE 회사를 갱신, 그것도 없으면 새로 만든다.
     * 한 건이라도 형식이 틀리면 전체를 400으로 거부한다 (부분 반영 없음).
     */
    @Transactional
    public ImportResult importAll(CurrentUser user, List<CompanyInput> inputs) {
        user.require(Role.REGISTRAR);
        if (inputs == null || inputs.isEmpty()) {
            throw bad("가져올 회사가 없습니다.");
        }
        List<CompanyInput> cleaned = new ArrayList<>();
        for (int i = 0; i < inputs.size(); i++) {
            try {
                cleaned.add(cleanForCreate(inputs.get(i)));
            } catch (ApiException e) {
                throw bad((i + 1) + "번째 행: " + e.getMessage());
            }
        }
        int created = 0;
        int updated = 0;
        List<Long> ids = new ArrayList<>();
        for (CompanyInput in : cleaned) {
            Optional<Long> existing = in.bizNo() != null ? findByBizNo(in.bizNo()) : findActiveByName(in.name());
            if (existing.isPresent()) {
                long id = existing.get();
                applyUpdate(user, id, in, true);
                updated++;
                ids.add(id);
            } else {
                ids.add(insert(user, in));
                created++;
            }
        }
        audit.log(user.id(), "COMPANY_IMPORTED", "company", null, "{\"created\":" + created + ",\"updated\":" + updated + "}");
        return new ImportResult(created, updated, ids);
    }

    @Transactional
    public CompanyView update(CurrentUser user, long id, CompanyInput in) {
        user.require(Role.REGISTRAR);
        CompanyView current = get(user, id);
        if (!current.status().equals("ACTIVE")) {
            throw ApiException.conflict("보관된 회사는 수정할 수 없습니다.");
        }
        applyUpdate(user, id, in, false);
        audit.log(user.id(), "COMPANY_UPDATED", "company", id, "{\"name\":" + quote(get(user, id).name()) + "}");
        return get(user, id);
    }

    /** ARCHIVED 처리. 그 감시는 EXCLUDED. 이미 보관됐으면 그대로 둔다. */
    @Transactional
    public void archive(CurrentUser user, long id) {
        user.require(Role.REGISTRAR);
        CompanyView current = get(user, id);
        if (!current.status().equals("ACTIVE")) {
            return;
        }
        jdbc.sql("UPDATE company SET status = 'ARCHIVED', updated_at = now() WHERE id = ?").param(id).update();
        if (current.watchSourceId() != null) {
            watches.excludeForCompany(user, current.watchSourceId(), "회사 보관: " + current.name());
        }
        audit.log(user.id(), "COMPANY_ARCHIVED", "company", id, "{\"name\":" + quote(current.name()) + "}");
    }

    private long insert(CurrentUser user, CompanyInput in) {
        if (in.bizNo() != null && findByBizNo(in.bizNo()).isPresent()) {
            throw ApiException.conflict("같은 사업자등록번호의 회사가 이미 있습니다: " + in.bizNo());
        }
        long id = jdbc.sql("""
                INSERT INTO company (name, aliases, biz_no, corp_code, external_id, tags, created_by)
                VALUES (?, CAST(? AS jsonb), ?, ?, ?, CAST(? AS jsonb), ?) RETURNING id
                """)
                .params(in.name(), toJson(in.aliases()), in.bizNo(), in.corpCode(), in.externalId(), toJson(in.tags()), user.id())
                .query(Long.class).single();
        long watchId = watches.createForCompany(user, in.name(), keywordsOf(in.name(), in.aliases()), WATCH_INTERVAL_MINUTES).id();
        jdbc.sql("UPDATE company SET watch_source_id = ? WHERE id = ?").params(watchId, id).update();
        ensureCheckSource(user);
        return id;
    }

    /** null 필드는 그대로. 이름·별칭이 바뀌면 감시 키워드를 새 설정 버전으로. */
    private void applyUpdate(CurrentUser user, long id, CompanyInput in, boolean fromImport) {
        CompanyView current = jdbc.sql(SELECT + " WHERE c.id = ?").param(id).query(this::mapCompany).single();
        String name = in.name() == null ? current.name() : cleanName(in.name());
        List<String> aliases = in.aliases() == null ? current.aliases() : cleanAliases(in.aliases());
        String bizNo = in.bizNo() == null ? current.bizNo() : cleanBizNo(in.bizNo());
        String corpCode = in.corpCode() == null ? current.corpCode() : cleanCorpCode(in.corpCode());
        String externalId = in.externalId() == null ? current.externalId() : cleanShort(in.externalId(), 100, "externalId");
        List<String> tags = in.tags() == null ? current.tags() : cleanTags(in.tags());
        if (bizNo != null && !bizNo.equals(current.bizNo())) {
            Optional<Long> other = findByBizNo(bizNo);
            if (other.isPresent() && other.get() != id) {
                throw ApiException.conflict("같은 사업자등록번호의 회사가 이미 있습니다: " + bizNo);
            }
        }
        boolean bizNoChanged = bizNo != null && !bizNo.equals(current.bizNo());
        jdbc.sql("""
                UPDATE company SET name = ?, aliases = CAST(? AS jsonb), biz_no = ?, corp_code = ?, external_id = ?,
                       tags = CAST(? AS jsonb), last_biz_status = CASE WHEN ? THEN NULL ELSE last_biz_status END,
                       updated_at = now()
                WHERE id = ?
                """)
                .params(name, toJson(aliases), bizNo, corpCode, externalId, toJson(tags), bizNoChanged, id).update();
        boolean keywordsChanged = !name.equals(current.name()) || !aliases.equals(current.aliases());
        if (keywordsChanged && current.watchSourceId() != null && "ACTIVE".equals(current.status())) {
            watches.updateForCompany(user, current.watchSourceId(), name, keywordsOf(name, aliases));
        }
        if (fromImport && "ARCHIVED".equals(current.status())) {
            // 보관된 회사를 다시 가져오면 되살린다
            jdbc.sql("UPDATE company SET status = 'ACTIVE', updated_at = now() WHERE id = ?").param(id).update();
            long watchId = watches.createForCompany(user, name, keywordsOf(name, aliases), WATCH_INTERVAL_MINUTES).id();
            jdbc.sql("UPDATE company SET watch_source_id = ? WHERE id = ?").params(watchId, id).update();
        }
    }

    /**
     * 점검 소스(kind CHECK, 국세청 사업자 상태)가 없으면 만든다: site api.odcloud.kr + source PENDING + 설정 v1 + ONBOARD.
     * 워커가 키를 확인하고(없으면 REVIEW/API_KEY_REQUIRED, 관리자 큐 API_KEY) ACTIVE로 만들며 하루 1회 돈다.
     * 관리자가 EXCLUDED로 끈 점검은 다시 만들지 않는다.
     */
    void ensureCheckSource(CurrentUser user) {
        boolean exists = jdbc.sql("SELECT count(*) FROM source WHERE kind = 'CHECK' AND name = ?").param(CHECK_NAME)
                .query(Long.class).single() > 0;
        if (exists) {
            return;
        }
        long siteId = jdbc.sql("""
                INSERT INTO site (domain, base_url, site_type, risk_flags)
                VALUES (?, ?, 'PUBLIC', CAST(? AS jsonb))
                ON CONFLICT (domain) DO UPDATE SET domain = EXCLUDED.domain
                RETURNING id
                """)
                .params(CHECK_DOMAIN, CHECK_BASE_URL, "{\"api_provider\":\"" + CHECK_PROVIDER + "\"}")
                .query(Long.class).single();
        long sourceId = jdbc.sql("""
                INSERT INTO source (site_id, name, kind, method, status, interval_minutes, requested_by)
                VALUES (?, ?, 'CHECK', 'API', 'PENDING', ?, ?) RETURNING id
                """)
                .params(siteId, CHECK_NAME, CHECK_INTERVAL_MINUTES, user.id())
                .query(Long.class).single();
        Map<String, Object> config = new LinkedHashMap<>();
        config.put("method", "API");
        config.put("provider", CHECK_PROVIDER);
        config.put("check", "nts_status");
        long configId = jdbc.sql("""
                INSERT INTO source_config_version (source_id, version, config, status, change_note, created_by)
                VALUES (?, 1, CAST(? AS jsonb), 'ACTIVE', '회사 점검 소스 생성 (국세청 사업자등록 상태)', ?) RETURNING id
                """)
                .params(sourceId, toJson(config), user.id())
                .query(Long.class).single();
        jdbc.sql("UPDATE source SET active_config_version_id = ? WHERE id = ?").params(configId, sourceId).update();
        jdbc.sql("INSERT INTO crawl_job (source_id, job_type, trigger) VALUES (?, 'ONBOARD', 'REGISTRATION')")
                .param(sourceId).update();
        audit.log(user.id(), "CHECK_SOURCE_CREATED", "source", sourceId, "{\"check\":\"nts_status\"}");
    }

    // ------------------------------------------------------------------ 조회

    /** 목록 (이름순). includeArchived가 아니면 ACTIVE만. */
    public List<CompanyView> list(CurrentUser user, boolean includeArchived) {
        user.require(Role.VIEWER);
        String where = includeArchived ? "" : " WHERE c.status = 'ACTIVE'";
        return jdbc.sql(SELECT + where + " ORDER BY c.name, c.id").query(this::mapCompany).list();
    }

    public CompanyView get(CurrentUser user, long id) {
        user.require(Role.VIEWER);
        return jdbc.sql(SELECT + " WHERE c.id = ?").param(id).query(this::mapCompany).optional()
                .orElseThrow(() -> ApiException.notFound("회사"));
    }

    /** 상세: 최근 신호 20건(심각도·시각 내림차순) + 최근 연결 문서 20건. */
    public CompanyDetail detail(CurrentUser user, long id) {
        CompanyView c = get(user, id);
        List<SignalView> signals = signals(user, id, null, 20);
        List<DocumentRef> documents = jdbc.sql("""
                SELECT d.id, d.url, d.title, d.published_at, t.domain, dc.matched_by, dc.matched_at,
                       COALESCE(e.topics::text, '[]') AS topics
                FROM document_company dc
                JOIN document d ON d.id = dc.document_id
                JOIN source s ON s.id = d.source_id
                JOIN site t ON t.id = s.site_id
                LEFT JOIN document_enrichment e ON e.document_id = d.id
                WHERE dc.company_id = ?
                ORDER BY dc.matched_at DESC, d.id DESC LIMIT 20
                """).param(id).query((rs, n) -> new DocumentRef(rs.getLong("id"), rs.getString("url"), rs.getString("title"),
                        rs.getObject("published_at", OffsetDateTime.class), rs.getString("domain"), rs.getString("matched_by"),
                        rs.getObject("matched_at", OffsetDateTime.class), strings(rs.getString("topics")))).list();
        return new CompanyDetail(c, signals, documents);
    }

    /** 회사 하나의 신호. since(observed_at 초과) 선택, 심각도 → 시각 내림차순. */
    public List<SignalView> signals(CurrentUser user, long companyId, OffsetDateTime since, int limit) {
        get(user, companyId);
        return querySignals(companyId, since, null, null, limit);
    }

    /** 전체 신호 (심각도 HIGH → MEDIUM → LOW, 그 안에서 시각 내림차순). */
    public List<SignalView> allSignals(CurrentUser user, OffsetDateTime since, String kind, String severity, int limit) {
        user.require(Role.VIEWER);
        String k = kind == null || kind.isBlank() ? null : kind.strip().toUpperCase(Locale.ROOT);
        String s = severity == null || severity.isBlank() ? null : severity.strip().toUpperCase(Locale.ROOT);
        if (k != null && !KINDS.contains(k)) {
            throw bad("kind는 " + String.join(", ", KINDS) + " 중 하나입니다.");
        }
        if (s != null && !SEVERITIES.contains(s)) {
            throw bad("severity는 HIGH, MEDIUM, LOW 중 하나입니다.");
        }
        return querySignals(null, since, k, s, limit);
    }

    private List<SignalView> querySignals(Long companyId, OffsetDateTime since, String kind, String severity, int limit) {
        StringBuilder sql = new StringBuilder(SIGNAL_SELECT + " WHERE 1 = 1");
        List<Object> params = new ArrayList<>();
        if (companyId != null) {
            sql.append(" AND g.company_id = ?");
            params.add(companyId);
        }
        if (since != null) {
            sql.append(" AND g.observed_at > ?");
            params.add(since);
        }
        if (kind != null) {
            sql.append(" AND g.kind = ?");
            params.add(kind);
        }
        if (severity != null) {
            sql.append(" AND g.severity = ?");
            params.add(severity);
        }
        sql.append(" ORDER BY " + SEVERITY_ORDER + ", g.observed_at DESC, g.id DESC LIMIT ?");
        params.add(Math.max(1, Math.min(500, limit)));
        return jdbc.sql(sql.toString()).params(params).query(this::mapSignal).list();
    }

    // ------------------------------------------------------------------ 검증·정규화

    private CompanyInput cleanForCreate(CompanyInput in) {
        if (in == null) {
            throw bad("회사 정보를 넣어 주세요.");
        }
        return new CompanyInput(cleanName(in.name()), cleanAliases(in.aliases()), cleanBizNo(in.bizNo()),
                cleanCorpCode(in.corpCode()), cleanShort(in.externalId(), 100, "externalId"), cleanTags(in.tags()));
    }

    static String cleanName(String name) {
        String cleaned = name == null ? "" : name.strip().replaceAll("\\s+", " ");
        if (cleaned.isEmpty()) {
            throw bad("회사 이름을 넣어 주세요.");
        }
        if (cleaned.length() > 200) {
            throw bad("회사 이름은 200자 이하여야 합니다.");
        }
        return cleaned;
    }

    static List<String> cleanAliases(List<String> aliases) {
        List<String> out = new ArrayList<>();
        for (String a : aliases == null ? List.<String>of() : aliases) {
            String v = a == null ? "" : a.strip().replaceAll("\\s+", " ");
            if (v.isEmpty() || out.stream().anyMatch(x -> x.equalsIgnoreCase(v))) {
                continue;
            }
            if (v.length() > 200) {
                throw bad("별칭은 200자 이하여야 합니다.");
            }
            out.add(v);
        }
        if (out.size() > MAX_ALIASES) {
            throw bad("별칭은 " + MAX_ALIASES + "개 이하여야 합니다.");
        }
        return out;
    }

    static List<String> cleanTags(List<String> tags) {
        List<String> out = new ArrayList<>();
        for (String t : tags == null ? List.<String>of() : tags) {
            String v = t == null ? "" : t.strip().replaceAll("\\s+", " ");
            if (v.isEmpty() || out.contains(v)) {
                continue;
            }
            if (v.length() > 50) {
                throw bad("태그는 50자 이하여야 합니다.");
            }
            out.add(v);
        }
        if (out.size() > MAX_TAGS) {
            throw bad("태그는 " + MAX_TAGS + "개 이하여야 합니다.");
        }
        return out;
    }

    /** 하이픈·공백을 지운 숫자 10자리. 비어 있으면 null, 형식이 틀리면 400. */
    static String cleanBizNo(String bizNo) {
        if (bizNo == null || bizNo.isBlank()) {
            return null;
        }
        String digits = bizNo.replaceAll("[\\s-]", "");
        if (!BIZ_NO.matcher(digits).matches()) {
            throw bad("사업자등록번호는 숫자 10자리여야 합니다 (예: 123-45-67890): " + bizNo.strip());
        }
        return digits;
    }

    static String cleanCorpCode(String corpCode) {
        if (corpCode == null || corpCode.isBlank()) {
            return null;
        }
        String v = corpCode.strip();
        if (!CORP_CODE.matcher(v).matches()) {
            throw bad("DART 고유번호는 숫자 8자리여야 합니다: " + v);
        }
        return v;
    }

    private static String cleanShort(String value, int max, String field) {
        if (value == null || value.isBlank()) {
            return null;
        }
        String v = value.strip();
        if (v.length() > max) {
            throw bad(field + "은(는) " + max + "자 이하여야 합니다.");
        }
        return v;
    }

    /** 감시 키워드: 이름 + 별칭 (중복 제거는 WatchService가 한다). */
    static List<String> keywordsOf(String name, List<String> aliases) {
        List<String> out = new ArrayList<>();
        out.add(name);
        out.addAll(aliases == null ? List.of() : aliases);
        return out;
    }

    private Optional<Long> findByBizNo(String bizNo) {
        return jdbc.sql("SELECT id FROM company WHERE biz_no = ?").param(bizNo).query(Long.class).optional();
    }

    private Optional<Long> findActiveByName(String name) {
        return jdbc.sql("SELECT id FROM company WHERE status = 'ACTIVE' AND lower(name) = lower(?) ORDER BY id LIMIT 1")
                .param(name).query(Long.class).optional();
    }

    /** CSV 텍스트(헤더 name,aliases,biz_no,corp_code,external_id,tags; aliases·tags는 ';' 구분) → 입력 목록. */
    public static List<CompanyInput> parseCsv(String text) {
        List<CompanyInput> out = new ArrayList<>();
        if (text == null) {
            return out;
        }
        String[] lines = text.replace("﻿", "").split("\\r?\\n");
        List<String> header = null;
        for (String raw : lines) {
            if (raw.isBlank()) {
                continue;
            }
            List<String> cells = splitCsvLine(raw);
            if (header == null) {
                header = cells.stream().map(h -> h.strip().toLowerCase(Locale.ROOT).replace("-", "_")).toList();
                if (!header.contains("name")) {
                    throw bad("CSV 첫 줄은 헤더여야 하며 name 열이 있어야 합니다 (name,aliases,biz_no,corp_code,external_id,tags).");
                }
                continue;
            }
            Map<String, String> row = new LinkedHashMap<>();
            for (int i = 0; i < header.size() && i < cells.size(); i++) {
                row.put(header.get(i), cells.get(i));
            }
            out.add(new CompanyInput(row.get("name"), splitList(row.get("aliases")), row.get("biz_no"), row.get("corp_code"),
                    row.get("external_id"), splitList(row.get("tags"))));
        }
        return out;
    }

    private static List<String> splitList(String value) {
        if (value == null || value.isBlank()) {
            return List.of();
        }
        return List.of(value.split(";"));
    }

    /** 큰따옴표 감싼 셀("a,b")과 ""(따옴표) 이스케이프를 지원하는 최소 CSV 분리. */
    static List<String> splitCsvLine(String line) {
        List<String> cells = new ArrayList<>();
        StringBuilder cur = new StringBuilder();
        boolean quoted = false;
        for (int i = 0; i < line.length(); i++) {
            char c = line.charAt(i);
            if (quoted) {
                if (c == '"') {
                    if (i + 1 < line.length() && line.charAt(i + 1) == '"') {
                        cur.append('"');
                        i++;
                    } else {
                        quoted = false;
                    }
                } else {
                    cur.append(c);
                }
            } else if (c == '"') {
                quoted = true;
            } else if (c == ',') {
                cells.add(cur.toString().strip());
                cur.setLength(0);
            } else {
                cur.append(c);
            }
        }
        cells.add(cur.toString().strip());
        return cells;
    }

    static ApiException bad(String message) {
        return new ApiException(HttpStatus.BAD_REQUEST, "BAD_REQUEST", message);
    }

    private static String quote(String text) {
        return text == null ? "null" : "\"" + text.replace("\\", "\\\\").replace("\"", "\\\"") + "\"";
    }

    private String toJson(Object value) {
        try {
            return json.writeValueAsString(value == null ? List.of() : value);
        } catch (JsonProcessingException e) {
            throw new IllegalStateException(e);
        }
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

    private Map<String, Object> object(String jsonText) {
        if (jsonText == null || jsonText.isBlank()) {
            return null;
        }
        try {
            return json.readValue(jsonText, OBJECT);
        } catch (JsonProcessingException e) {
            return null;
        }
    }

    // ------------------------------------------------------------------ 매핑

    static final String SEVERITY_ORDER = "CASE g.severity WHEN 'HIGH' THEN 0 WHEN 'MEDIUM' THEN 1 ELSE 2 END";

    private static final String SELECT = """
            SELECT c.id, c.name, c.aliases::text AS aliases, c.biz_no, c.corp_code, c.external_id, c.tags::text AS tags,
                   c.status, c.watch_source_id, w.status AS watch_status, w.status_reason AS watch_status_reason,
                   c.last_biz_status::text AS last_biz_status, u.name AS created_by, c.created_at, c.updated_at,
                   (SELECT count(*) FROM company_signal g WHERE g.company_id = c.id) AS signal_count,
                   (SELECT count(*) FROM company_signal g WHERE g.company_id = c.id AND g.severity = 'HIGH') AS high_signal_count,
                   (SELECT count(*) FROM document_company dc WHERE dc.company_id = c.id) AS document_count,
                   l.id AS last_signal_id, l.kind AS last_signal_kind, l.severity AS last_signal_severity,
                   l.title AS last_signal_title, l.observed_at AS last_signal_at
            FROM company c
            LEFT JOIN source w ON w.id = c.watch_source_id
            LEFT JOIN app_user u ON u.id = c.created_by
            LEFT JOIN LATERAL (
                SELECT id, kind, severity, title, observed_at FROM company_signal g
                WHERE g.company_id = c.id ORDER BY g.observed_at DESC, g.id DESC LIMIT 1) l ON true
            """;

    private static final String SIGNAL_SELECT = """
            SELECT g.id, g.company_id, c.name AS company_name, g.kind, g.severity, g.title, g.detail::text AS detail,
                   g.observed_at, g.document_id, d.url AS document_url, d.title AS document_title, g.source_id, g.created_at
            FROM company_signal g
            JOIN company c ON c.id = g.company_id
            LEFT JOIN document d ON d.id = g.document_id
            """;

    private CompanyView mapCompany(ResultSet rs, int n) throws SQLException {
        Long lastSignalId = rs.getObject("last_signal_id", Long.class);
        SignalBrief last = lastSignalId == null ? null : new SignalBrief(lastSignalId, rs.getString("last_signal_kind"),
                rs.getString("last_signal_severity"), rs.getString("last_signal_title"),
                rs.getObject("last_signal_at", OffsetDateTime.class));
        return new CompanyView(rs.getLong("id"), rs.getString("name"), strings(rs.getString("aliases")), rs.getString("biz_no"),
                rs.getString("corp_code"), rs.getString("external_id"), strings(rs.getString("tags")), rs.getString("status"),
                rs.getObject("watch_source_id", Long.class), rs.getString("watch_status"), rs.getString("watch_status_reason"),
                object(rs.getString("last_biz_status")), rs.getLong("signal_count"), rs.getLong("high_signal_count"), last,
                rs.getLong("document_count"), rs.getString("created_by"), rs.getObject("created_at", OffsetDateTime.class),
                rs.getObject("updated_at", OffsetDateTime.class));
    }

    private SignalView mapSignal(ResultSet rs, int n) throws SQLException {
        Map<String, Object> detail = object(rs.getString("detail"));
        return new SignalView(rs.getLong("id"), rs.getLong("company_id"), rs.getString("company_name"), rs.getString("kind"),
                rs.getString("severity"), rs.getString("title"), detail == null ? Map.of() : detail,
                rs.getObject("observed_at", OffsetDateTime.class), rs.getObject("document_id", Long.class),
                rs.getString("document_url"), rs.getString("document_title"), rs.getObject("source_id", Long.class),
                rs.getObject("created_at", OffsetDateTime.class));
    }
}
