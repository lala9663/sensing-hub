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
 * 텍스트(마크다운 비슷한 평문, 웹훅용)와 HTML(메일용, 인라인 스타일 최소)을 함께 만든다.
 */
@Component
public class DigestComposer {

    public static final ZoneId KST = ZoneId.of("Asia/Seoul");
    public static final List<String> TOPIC_ORDER = List.of(
            "정책·규제", "입찰·공고", "지원사업", "자사", "경쟁사", "산업동향", "인사·조직", "사건·사고", "기타");
    static final String UNCLASSIFIED = "분류 전";
    static final String FOOTER = "sensing-hub 다이제스트";

    private static final DateTimeFormatter DATE_TIME = DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm");
    private static final DateTimeFormatter DATE = DateTimeFormatter.ofPattern("yyyy-MM-dd");

    public record WatchHit(long watchId, String watchName, String keyword) {
    }

    public record Document(long id, String url, String title, OffsetDateTime publishedAt, String domain,
                           String summary, List<String> topics, List<WatchHit> hits) {
    }

    /** 만들어진 다이제스트. documentCount는 묶음과 무관한 문서 수(중복 없이)다. */
    public record Digest(OffsetDateTime periodFrom, OffsetDateTime periodTo, int documentCount, boolean truncated,
                         String subject, String text, String html) {
    }

    /** 한 묶음: 제목(감시 이름 또는 토픽), 소제목(검색어, 감시에만), 문서들. */
    record Group(String heading, Map<String, List<Document>> sections) {
    }

    public Digest compose(String subscriptionName, OffsetDateTime from, OffsetDateTime to, List<Document> documents,
                          Set<Long> watchIds, boolean truncated) {
        List<Group> groups = group(documents, watchIds);
        String period = DATE_TIME.format(from.atZoneSameInstant(KST)) + " ~ " + DATE_TIME.format(to.atZoneSameInstant(KST));
        String subject = "[sensing-hub] " + subscriptionName + " 다이제스트 " + DATE.format(to.atZoneSameInstant(KST))
                + " (" + documents.size() + "건)";
        return new Digest(from, to, documents.size(), truncated, subject,
                text(subscriptionName, period, documents.size(), truncated, groups),
                html(subscriptionName, period, documents.size(), truncated, groups));
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

    private static String text(String name, String period, int count, boolean truncated, List<Group> groups) {
        StringBuilder sb = new StringBuilder();
        sb.append("*sensing-hub 일간 다이제스트 — ").append(name).append("*\n");
        sb.append("기간: ").append(period).append(" (KST) · 새 문서 ").append(count).append("건");
        if (truncated) {
            sb.append(" (상한에 걸려 최근 ").append(count).append("건만)");
        }
        sb.append("\n");
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

    private static String html(String name, String period, int count, boolean truncated, List<Group> groups) {
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
        sb.append("</p>\n");
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
