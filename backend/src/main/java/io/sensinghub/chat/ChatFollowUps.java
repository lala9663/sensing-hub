package io.sensinghub.chat;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Objects;
import java.util.Set;
import java.util.function.Function;
import java.util.stream.Collectors;

import io.sensinghub.chat.ChatTools.Ref;

/**
 * 답마다 붙이는 추천 후속 질문 칩 (LLM을 더 부르지 않는 규칙). 이번 턴에 쓴 도구의 조건과 결과({@link Facts} — 도구가 실행하며 남긴다)와
 * 답의 근거로 2~3개를 만든다.
 * <ul>
 *   <li>문서 검색에 결과가 있으면: "이 중 {회사·태그} 관련만"(근거에 자주 나온 회사, 없으면 태그 — 결과를 실제로 줄이는 값만),
 *       "이 중 {분류}만" 또는 "이 중 {언론사} 기사만", 기간이 30일보다 짧았으면 "‘검색어’ 최근 N일로 넓혀 보기".
 *       "이 중 ~만"은 보내면 LLM이 search_documents(within_previous)로 앞 근거 안에서 거른다. 칩이 모자라면 첫 근거의 "원문 보기"(링크).</li>
 *   <li>결과가 0건이면: 기간 넓혀 다시 찾기(또는 조건 없이), "네이버 - 검색어". 앞 근거 안에서 거른 결과가 0건이면 "‘…’ 기사 새로 찾아 줘".</li>
 *   <li>이슈 목록: "1번 이슈 언론사 목록"(앞 근거 번호로 그 이슈의 기사를 본다), 대표 낱말 기사 찾기, 기간 넓히기.
 *       감시 목록: "‘감시 이름’ 감시 문서 보기". 감시 만들기·사이트 등록 뒤: 목록·진행 상태.</li>
 *   <li>칩 글은 키워드 바로 검색({@link ChatShortcuts#keyword})으로 잘못 가지 않는 문장이다 ("네이버 - 검색어"는 화면이 네이버 결과로 보여 준다).</li>
 * </ul>
 * 칩은 답 meta.followUps에 저장해 대화를 다시 열어도 보인다. 화면은 맨 아래 답의 칩만 보여 준다 (앞 답의 "이 중"은 가리키는 목록이 달라진다).
 */
public final class ChatFollowUps {

    static final int MAX = 3;
    /** 칩에 넣을 회사·태그·언론사 이름의 최대 길이 (넘으면 그 값은 쓰지 않는다). */
    static final int MAX_VALUE_CHARS = 20;

    /** 추천 칩. text를 보낸다. url이 있으면 보내지 않고 그 원문을 새 탭에서 연다. */
    public record FollowUp(String text, String url) {

        static FollowUp ask(String text) {
            return new FollowUp(text, null);
        }
    }

    /** 도구가 실행하며 남기는 조건·결과 요약 (추천 칩용, LLM에 보내지 않는다). */
    sealed interface Facts permits SearchFacts, IssueFacts, WatchFacts, ActionFacts {
    }

    /** 문서 한 건: 언론사(사이트 이름), 기사 종류, 분류, 태그, 연결된 회사. */
    record DocFacts(long documentId, String source, String section, List<String> topics, List<String> tags,
                    List<String> companies) {
    }

    /**
     * 문서 검색 한 번. within: 앞 답의 근거 안에서 거른 검색 (previousCount: 그 근거 수). days: 요청한 기간(일, since·없음이면 null).
     */
    record SearchFacts(String query, String site, List<String> sections, String topic, Integer days, Long watchId,
                       boolean within, int previousCount, List<DocFacts> docs) implements Facts {
    }

    record IssueItem(int ref, String title, List<String> terms) {
    }

    record IssueFacts(int days, List<IssueItem> issues) implements Facts {
    }

    record WatchItem(String name, long documents) {
    }

    record WatchFacts(List<WatchItem> watches) implements Facts {
    }

    /** 감시 만들기·사이트 등록을 해냈다. */
    record ActionFacts(String tool) implements Facts {
    }

    private ChatFollowUps() {
    }

    /**
     * @param facts 이번 턴 도구들이 남긴 요약 (부른 차례). 문서 검색이 있으면 마지막 문서 검색으로, 없으면 이슈·감시·등록 순으로 고른다
     * @param cited 답의 근거 (근거로 쓴 문서만 보고 고른다. 답이 번호를 쓰지 않았으면 검색 결과 전체)
     */
    static List<FollowUp> suggest(List<Facts> facts, List<Ref> cited) {
        Facts pick = last(facts, SearchFacts.class);
        if (pick == null) {
            pick = last(facts, IssueFacts.class);
        }
        if (pick == null) {
            pick = last(facts, WatchFacts.class);
        }
        if (pick == null) {
            pick = last(facts, ActionFacts.class);
        }
        if (pick == null) {
            return List.of();
        }
        List<FollowUp> out;
        if (pick instanceof SearchFacts s) {
            out = search(s, cited == null ? List.of() : cited);
        } else if (pick instanceof IssueFacts i) {
            out = issues(i);
        } else if (pick instanceof WatchFacts w) {
            out = watches(w);
        } else {
            out = action((ActionFacts) pick);
        }
        return out.size() > MAX ? List.copyOf(out.subList(0, MAX)) : List.copyOf(out);
    }

    private static <T extends Facts> T last(List<Facts> facts, Class<T> type) {
        for (int i = facts.size() - 1; i >= 0; i--) {
            if (type.isInstance(facts.get(i))) {
                return type.cast(facts.get(i));
            }
        }
        return null;
    }

    // ------------------------------------------------------------------ 문서 검색

    private static List<FollowUp> search(SearchFacts s, List<Ref> cited) {
        List<FollowUp> out = new ArrayList<>();
        Set<Long> citedIds = cited.stream().map(Ref::documentId).filter(Objects::nonNull).collect(Collectors.toSet());
        List<DocFacts> docs = s.docs().stream().filter(d -> citedIds.contains(d.documentId())).toList();
        if (docs.isEmpty()) {
            docs = s.docs();
        }
        String subject = subject(s);
        if (docs.isEmpty()) {
            if (s.within()) {
                String what = s.query() != null ? s.query() : s.site() != null ? s.site()
                        : !s.sections().isEmpty() ? s.sections().get(0) : s.topic();
                if (what != null) {
                    add(out, "‘" + what + "’ 기사 새로 찾아 줘");
                }
            } else if (s.days() != null && s.days() < 30) {
                add(out, join(subject, "최근 30일로 넓혀 다시 찾아 줘"));
            } else if (s.days() != null && s.days() < 90) {
                add(out, join(subject, "최근 90일로 넓혀 다시 찾아 줘"));
            } else if (s.query() != null && (s.site() != null || !s.sections().isEmpty() || s.topic() != null
                    || s.watchId() != null)) {
                add(out, "‘" + s.query() + "’ 조건 없이 다시 찾아 줘");
            }
            if (s.query() != null) {
                add(out, "네이버 - " + s.query());
            }
            return out;
        }

        List<String> taken = new ArrayList<>();
        if (s.query() != null) {
            taken.add(s.query());
        }
        if (s.site() != null) {
            taken.add(s.site());
        }
        taken.addAll(s.sections());
        if (docs.size() >= 2) {
            String entity = narrowing(docs, DocFacts::companies, taken);
            if (entity == null) {
                entity = narrowing(docs, DocFacts::tags, taken);
            }
            if (entity != null) {
                add(out, "이 중 " + entity + " 관련만");
                taken.add(entity);
            }
            String topic = s.topic() == null ? narrowing(docs, DocFacts::topics, taken) : null;
            if (topic != null) {
                add(out, "이 중 " + topic + "만");
            }
        }
        if (!s.within() && s.days() != null && s.days() < 30) {
            add(out, join(subject, "최근 " + (s.days() < 7 ? 7 : 30) + "일로 넓혀 보기"));
        }
        if (docs.size() >= 2 && s.site() == null && out.size() < MAX) {
            String source = narrowing(docs, d -> d.source() == null ? List.of() : List.of(d.source()), taken);
            if (source != null) {
                add(out, "이 중 " + source + " 기사만");
            }
        }
        if (out.size() < 2) {
            cited.stream().filter(r -> "DOCUMENT".equals(r.kind()) && r.url() != null && r.url().matches("(?i)^https?://.+"))
                    .findFirst().ifPresent(r -> out.add(new FollowUp(r.no() + "번 기사 원문 보기", r.url())));
        }
        return out;
    }

    /** 검색 주제 ("‘반도체’"): 검색어, 없으면 기사 종류·사이트. 없으면 null. */
    private static String subject(SearchFacts s) {
        String what = s.query() != null ? s.query() : !s.sections().isEmpty() ? String.join("·", s.sections()) : s.site();
        return what == null ? null : "‘" + what + "’";
    }

    private static String join(String subject, String rest) {
        return subject == null ? rest : subject + " " + rest;
    }

    /**
     * 결과를 실제로 줄이는 값 중 가장 많이 나온 것: 1건 이상, 전체보다 적게. 같은 수면 먼저 나온 것. 이미 쓴 조건(검색어·사이트·종류)과
     * 겹치거나 너무 긴 값은 뺀다.
     */
    static String narrowing(List<DocFacts> docs, Function<DocFacts, List<String>> values, List<String> taken) {
        Map<String, Integer> counts = new LinkedHashMap<>();
        Map<String, String> shown = new LinkedHashMap<>();
        for (DocFacts d : docs) {
            List<String> list = values.apply(d);
            if (list == null) {
                continue;
            }
            list.stream().filter(v -> v != null && !v.isBlank()).map(String::strip).map(v -> Map.entry(compact(v), v))
                    .collect(Collectors.toMap(Map.Entry::getKey, Map.Entry::getValue, (a, b) -> a, LinkedHashMap::new))
                    .forEach((key, value) -> {
                        counts.merge(key, 1, Integer::sum);
                        shown.putIfAbsent(key, value);
                    });
        }
        String best = null;
        int bestCount = 0;
        for (Map.Entry<String, Integer> e : counts.entrySet()) {
            String value = shown.get(e.getKey());
            int c = e.getValue();
            if (c >= docs.size() || c <= bestCount || value.length() > MAX_VALUE_CHARS || overlaps(e.getKey(), taken)) {
                continue;
            }
            best = value;
            bestCount = c;
        }
        return best;
    }

    private static boolean overlaps(String key, List<String> taken) {
        for (String t : taken) {
            String k = compact(t);
            if (!k.isEmpty() && (k.contains(key) || key.contains(k))) {
                return true;
            }
        }
        return false;
    }

    private static String compact(String s) {
        return s == null ? "" : s.replaceAll("\\s+", "").toLowerCase(Locale.ROOT);
    }

    private static void add(List<FollowUp> out, String text) {
        if (out.stream().noneMatch(f -> f.text().equals(text))) {
            out.add(FollowUp.ask(text));
        }
    }

    // ------------------------------------------------------------------ 그 밖의 도구

    private static List<FollowUp> issues(IssueFacts i) {
        List<FollowUp> out = new ArrayList<>();
        if (i.issues().isEmpty()) {
            if (i.days() < 7) {
                add(out, "최근 7일 이슈로 넓혀 보기");
            } else if (i.days() < 30) {
                add(out, "최근 30일 이슈로 넓혀 보기");
            }
            return out;
        }
        IssueItem first = i.issues().get(0);
        add(out, first.ref() + "번 이슈 언론사 목록");
        first.terms().stream().filter(t -> t != null && t.strip().length() >= 2 && t.length() <= MAX_VALUE_CHARS).findFirst()
                .ifPresent(t -> add(out, "‘" + t.strip() + "’ 관련 기사 찾아 줘"));
        if (i.days() < 7) {
            add(out, "최근 7일 이슈로 넓혀 보기");
        } else if (i.issues().size() >= 2) {
            add(out, i.issues().get(1).ref() + "번 이슈 언론사 목록");
        }
        return out;
    }

    private static List<FollowUp> watches(WatchFacts w) {
        List<FollowUp> out = new ArrayList<>();
        w.watches().stream().filter(x -> x.documents() > 0 && x.name() != null && x.name().length() <= MAX_VALUE_CHARS)
                .limit(2).forEach(x -> add(out, "‘" + x.name() + "’ 감시 문서 보기"));
        return out;
    }

    private static List<FollowUp> action(ActionFacts a) {
        return switch (a.tool()) {
            case ChatTools.CREATE_WATCH -> List.of(FollowUp.ask("감시 목록 보여 줘"));
            case ChatTools.REGISTER_SOURCE -> List.of(FollowUp.ask("등록 진행 상태 알려 줘"));
            default -> List.of();
        };
    }
}
