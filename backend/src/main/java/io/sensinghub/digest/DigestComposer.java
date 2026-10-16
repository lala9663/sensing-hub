package io.sensinghub.digest;

import java.time.OffsetDateTime;
import java.time.ZoneId;
import java.time.format.DateTimeFormatter;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

import org.springframework.stereotype.Component;

/**
 * 다이제스트 본문을 만든다 (DB·전송 없음). 문서 목록을 받아 다음 순서로 묶는다.
 * <ol>
 *   <li>감시별 (watch_name, 그 안에서 검색어별). 구독에 watch_ids가 있으면 그 감시들만 감시 묶음을 만든다.</li>
 *   <li>그 밖(감시 묶음에 나오지 않은 문서)은 토픽별, 분류 체계 순서(정책·규제 → … → 기타). 토픽이 여럿이면 각 토픽에 나온다.</li>
 *   <li>보강 전(토픽 없음) 문서는 "분류 전".</li>
 * </ol>
 * 항목 한 줄은 "제목 — 출처 도메인 · 발행일(KST) · 링크"이고 요약이 있으면 다음 줄에 붙는다 (언론 기사는 요약이 없다).
 * 회사 신호(M3)가 있으면 맨 앞에 "거래처 신호" 절을 두고 회사별로 묶는다 (심각도 HIGH인 회사 먼저, 그 안에서도 HIGH 먼저).
 * 텍스트(마크다운 비슷한 평문, 웹훅용)와 HTML(메일용, 인라인 스타일 최소)을 함께 만든다.
 */
@Component
public class DigestComposer {

    public static final ZoneId KST = ZoneId.of("Asia/Seoul");
    public static final List<String> TOPIC_ORDER = List.of(
            "정책·규제", "입찰·공고", "지원사업", "자사", "경쟁사", "산업동향", "인사·조직", "사건·사고", "기타");
    static final String UNCLASSIFIED = "분류 전";
    static final String FOOTER = "sensing-hub 다이제스트";
    static final String COMPANY_HEADING = "거래처 신호";
    static final List<String> SEVERITY_ORDER = List.of("HIGH", "MEDIUM", "LOW");
    static final Map<String, String> KIND_NAMES = Map.of(
            "BIZ_STATUS", "사업자 상태", "DEBARMENT", "입찰 제한", "DISCLOSURE", "공시", "NEWS", "보도", "FINANCIAL", "재무");

    private static final DateTimeFormatter DATE_TIME = DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm");
    private static final DateTimeFormatter DATE = DateTimeFormatter.ofPattern("yyyy-MM-dd");

    public record WatchHit(long watchId, String watchName, String keyword) {
    }

    public record Document(long id, String url, String title, OffsetDateTime publishedAt, String domain,
                           String summary, List<String> topics, List<WatchHit> hits) {
    }

    /** 회사 신호 한 건 (company_signal). url은 근거 문서가 있을 때만. */
    public record CompanySignal(long companyId, String companyName, String kind, String severity, String title,
                                OffsetDateTime observedAt, String url) {
    }

    /** 만들어진 다이제스트. documentCount는 묶음과 무관한 문서 수(중복 없이), signalCount는 회사 신호 수다. */
    public record Digest(OffsetDateTime periodFrom, OffsetDateTime periodTo, int documentCount, int signalCount,
                         boolean truncated, String subject, String text, String html) {
    }

    /** 한 묶음: 제목(감시 이름 또는 토픽), 소제목(검색어, 감시에만), 문서들. */
    record Group(String heading, Map<String, List<Document>> sections) {
    }

    public Digest compose(String subscriptionName, OffsetDateTime from, OffsetDateTime to, List<Document> documents,
                          Set<Long> watchIds, boolean truncated) {
        return compose(subscriptionName, from, to, documents, watchIds, truncated, List.of());
    }

    public Digest compose(String subscriptionName, OffsetDateTime from, OffsetDateTime to, List<Document> documents,
                          Set<Long> watchIds, boolean truncated, List<CompanySignal> signals) {
        List<Group> groups = group(documents, watchIds);
        Map<String, List<CompanySignal>> byCompany = groupSignals(signals);
        String period = DATE_TIME.format(from.atZoneSameInstant(KST)) + " ~ " + DATE_TIME.format(to.atZoneSameInstant(KST));
        String counts = signals.isEmpty() ? documents.size() + "건"
                : "문서 " + documents.size() + "건 · 신호 " + signals.size() + "건";
        String subject = "[sensing-hub] " + subscriptionName + " 다이제스트 " + DATE.format(to.atZoneSameInstant(KST))
                + " (" + counts + ")";
        return new Digest(from, to, documents.size(), signals.size(), truncated, subject,
                text(subscriptionName, period, documents.size(), truncated, groups, byCompany),
                html(subscriptionName, period, documents.size(), truncated, groups, byCompany));
    }

    // ------------------------------------------------------------------ 회사 신호 묶기 (M3)

    /** 회사별 신호. 회사는 가장 높은 심각도 → 이름 순, 그 안에서 심각도 → 시각 내림차순. */
    static Map<String, List<CompanySignal>> groupSignals(List<CompanySignal> signals) {
        Map<String, List<CompanySignal>> byCompany = new LinkedHashMap<>();
        Comparator<CompanySignal> order = Comparator.comparingInt((CompanySignal s) -> severityRank(s.severity()))
                .thenComparing(s -> s.observedAt() == null ? OffsetDateTime.MIN : s.observedAt(), Comparator.reverseOrder());
        signals.stream()
                .sorted(order)
                .forEach(s -> byCompany.computeIfAbsent(s.companyName() == null ? "회사 " + s.companyId() : s.companyName(),
                        k -> new ArrayList<>()).add(s));
        Map<String, List<CompanySignal>> sorted = new LinkedHashMap<>();
        byCompany.entrySet().stream()
                .sorted(Comparator.comparingInt((Map.Entry<String, List<CompanySignal>> e) -> severityRank(e.getValue().get(0).severity()))
                        .thenComparing(Map.Entry::getKey))
                .forEach(e -> sorted.put(e.getKey(), e.getValue()));
        return sorted;
    }

    static int severityRank(String severity) {
        int i = SEVERITY_ORDER.indexOf(severity);
        return i < 0 ? SEVERITY_ORDER.size() : i;
    }

    /** "[HIGH] 폐업자 (폐업일 2026-09-30) — 사업자 상태 · 2026-10-16 09:00". */
    private static String signalLine(CompanySignal s) {
        String kind = KIND_NAMES.getOrDefault(s.kind(), s.kind());
        String when = s.observedAt() == null ? "" : " · " + DATE_TIME.format(s.observedAt().atZoneSameInstant(KST));
        return "[" + s.severity() + "] " + (s.title() == null ? "" : s.title().strip()) + " — " + kind + when;
    }

    // ------------------------------------------------------------------ 묶기

    List<Group> group(List<Document> documents, Set<Long> watchIds) {
        List<Group> groups = new ArrayList<>();
        Set<Long> shown = new LinkedHashSet<>();

        // ① 감시별 → 검색어별. 감시는 id 순(만든 순서), 검색어는 처음 나온 순서
        Map<Long, String> watchNames = new LinkedHashMap<>();
        Map<Long, Map<String, List<Document>>> byWatch = new LinkedHashMap<>();
        documents.stream()
                .flatMap(d -> d.hits().stream())
                .filter(h -> watchIds.isEmpty() || watchIds.contains(h.watchId()))
                .map(WatchHit::watchId).distinct().sorted()
                .forEach(id -> byWatch.put(id, new LinkedHashMap<>()));
        for (Document d : documents) {
            for (WatchHit hit : d.hits()) {
                Map<String, List<Document>> sections = byWatch.get(hit.watchId());
                if (sections == null) {
                    continue;
                }
                watchNames.putIfAbsent(hit.watchId(), hit.watchName() == null ? "감시 " + hit.watchId() : hit.watchName());
                String keyword = hit.keyword() == null || hit.keyword().isBlank() ? "" : hit.keyword();
                List<Document> list = sections.computeIfAbsent(keyword, k -> new ArrayList<>());
                if (list.stream().noneMatch(x -> x.id() == d.id())) {
                    list.add(d);
                }
                shown.add(d.id());
            }
        }
        byWatch.forEach((id, sections) -> {
            if (!sections.isEmpty()) {
                groups.add(new Group("감시: " + watchNames.get(id), sections));
            }
        });

        // ② 그 밖은 토픽별 (분류 체계 순서, 체계 밖 토픽은 뒤에 이름순) ③ 토픽 없으면 분류 전
        Map<String, List<Document>> byTopic = new LinkedHashMap<>();
        List<Document> unclassified = new ArrayList<>();
        for (Document d : documents) {
            if (shown.contains(d.id())) {
                continue;
            }
            if (d.topics() == null || d.topics().isEmpty()) {
                unclassified.add(d);
                continue;
            }
            for (String topic : new LinkedHashSet<>(d.topics())) {
                byTopic.computeIfAbsent(topic, t -> new ArrayList<>()).add(d);
            }
        }
        byTopic.keySet().stream().sorted(Comparator.comparingInt(DigestComposer::topicRank).thenComparing(t -> t))
                .forEach(topic -> groups.add(new Group(topic, Map.of("", byTopic.get(topic)))));
        if (!unclassified.isEmpty()) {
            groups.add(new Group(UNCLASSIFIED, Map.of("", unclassified)));
        }
        return groups;
    }

    private static int topicRank(String topic) {
        int i = TOPIC_ORDER.indexOf(topic);
        return i < 0 ? TOPIC_ORDER.size() : i;
    }

    // ------------------------------------------------------------------ 텍스트 (웹훅용)

    private static String text(String name, String period, int count, boolean truncated, List<Group> groups,
                               Map<String, List<CompanySignal>> byCompany) {
        StringBuilder sb = new StringBuilder();
        sb.append("*sensing-hub 일간 다이제스트 — ").append(name).append("*\n");
        sb.append("기간: ").append(period).append(" (KST) · 새 문서 ").append(count).append("건");
        if (truncated) {
            sb.append(" (상한에 걸려 최근 ").append(count).append("건만)");
        }
        if (!byCompany.isEmpty()) {
            sb.append(" · 거래처 신호 ").append(byCompany.values().stream().mapToInt(List::size).sum()).append("건");
        }
        sb.append("\n");
        if (!byCompany.isEmpty()) {
            sb.append("\n*").append(COMPANY_HEADING).append("*\n");
            byCompany.forEach((company, list) -> {
                sb.append("_").append(company).append("_\n");
                for (CompanySignal s : list) {
                    sb.append("- ").append(signalLine(s));
                    if (s.url() != null && !s.url().isBlank()) {
                        sb.append(" · ").append(s.url());
                    }
                    sb.append("\n");
                }
            });
        }
        for (Group g : groups) {
            sb.append("\n*").append(g.heading()).append("*\n");
            g.sections().forEach((keyword, docs) -> {
                if (!keyword.isEmpty()) {
                    sb.append("_").append(keyword).append("_\n");
                }
                for (Document d : docs) {
                    sb.append("- ").append(title(d)).append(" — ").append(meta(d)).append(" · ").append(d.url()).append("\n");
                    if (d.summary() != null && !d.summary().isBlank()) {
                        sb.append("  ").append(d.summary().strip()).append("\n");
                    }
                }
            });
        }
        sb.append("\n— ").append(FOOTER).append("\n");
        return sb.toString();
    }

    // ------------------------------------------------------------------ HTML (메일용)

    private static String html(String name, String period, int count, boolean truncated, List<Group> groups,
                               Map<String, List<CompanySignal>> byCompany) {
        StringBuilder sb = new StringBuilder();
        sb.append("<!DOCTYPE html><html lang=\"ko\"><head><meta charset=\"UTF-8\"><title>").append(esc(name))
                .append(" 다이제스트</title></head>\n");
        sb.append("<body style=\"font-family:sans-serif;font-size:14px;line-height:1.5;color:#222\">\n");
        sb.append("<h2 style=\"margin:0 0 4px\">sensing-hub 일간 다이제스트 — ").append(esc(name)).append("</h2>\n");
        sb.append("<p style=\"margin:0 0 16px;color:#555\">기간: ").append(esc(period)).append(" (KST) · 새 문서 <b>")
                .append(count).append("건</b>");
        if (truncated) {
            sb.append(" (상한에 걸려 최근 ").append(count).append("건만)");
        }
        if (!byCompany.isEmpty()) {
            sb.append(" · 거래처 신호 <b>").append(byCompany.values().stream().mapToInt(List::size).sum()).append("건</b>");
        }
        sb.append("</p>\n");
        if (!byCompany.isEmpty()) {
            sb.append("<h3 style=\"margin:16px 0 4px;border-bottom:1px solid #ddd\">").append(COMPANY_HEADING).append("</h3>\n");
            byCompany.forEach((company, list) -> {
                sb.append("<p style=\"margin:8px 0 2px\"><b>").append(esc(company)).append("</b></p>\n");
                sb.append("<ul style=\"margin:0 0 8px;padding-left:20px\">\n");
                for (CompanySignal s : list) {
                    String color = "HIGH".equals(s.severity()) ? "#b00020" : "MEDIUM".equals(s.severity()) ? "#b26a00" : "#555";
                    sb.append("<li style=\"margin:0 0 6px\"><span style=\"color:").append(color).append("\">")
                            .append(esc(signalLine(s))).append("</span>");
                    if (s.url() != null && !s.url().isBlank()) {
                        sb.append(" <a href=\"").append(esc(s.url())).append("\">근거</a>");
                    }
                    sb.append("</li>\n");
                }
                sb.append("</ul>\n");
            });
        }
        for (Group g : groups) {
            sb.append("<h3 style=\"margin:16px 0 4px;border-bottom:1px solid #ddd\">").append(esc(g.heading())).append("</h3>\n");
            g.sections().forEach((keyword, docs) -> {
                if (!keyword.isEmpty()) {
                    sb.append("<p style=\"margin:8px 0 2px;color:#555\"><i>").append(esc(keyword)).append("</i></p>\n");
                }
                sb.append("<ul style=\"margin:0 0 8px;padding-left:20px\">\n");
                for (Document d : docs) {
                    sb.append("<li style=\"margin:0 0 6px\"><a href=\"").append(esc(d.url())).append("\">").append(esc(title(d)))
                            .append("</a> <span style=\"color:#777\">— ").append(esc(meta(d))).append("</span>");
                    if (d.summary() != null && !d.summary().isBlank()) {
                        sb.append("<br><span>").append(esc(d.summary().strip())).append("</span>");
                    }
                    sb.append("</li>\n");
                }
                sb.append("</ul>\n");
            });
        }
        sb.append("<p style=\"margin-top:24px;color:#999;font-size:12px\">— ").append(FOOTER).append("</p>\n");
        sb.append("</body></html>\n");
        return sb.toString();
    }

    // ------------------------------------------------------------------

    private static String title(Document d) {
        return d.title() == null || d.title().isBlank() ? "(제목 없음)" : d.title().strip();
    }

    /** "출처 도메인 · 발행일(KST)". 발행일이 없으면 "발행일 없음". */
    private static String meta(Document d) {
        String domain = d.domain() == null ? "" : d.domain();
        String published = d.publishedAt() == null ? "발행일 없음" : DATE_TIME.format(d.publishedAt().atZoneSameInstant(KST));
        return domain + " · " + published;
    }

    static String esc(String s) {
        if (s == null) {
            return "";
        }
        StringBuilder sb = new StringBuilder(s.length() + 16);
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '<' -> sb.append("&lt;");
                case '>' -> sb.append("&gt;");
                case '&' -> sb.append("&amp;");
                case '"' -> sb.append("&quot;");
                case '\'' -> sb.append("&#39;");
                default -> sb.append(c);
            }
        }
        return sb.toString();
    }
}
