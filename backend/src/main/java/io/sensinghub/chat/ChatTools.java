package io.sensinghub.chat;

import java.time.OffsetDateTime;
import java.time.format.DateTimeFormatter;
import java.util.ArrayList;
import java.util.Collection;
import java.util.Comparator;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.function.BiFunction;
import java.util.stream.Collectors;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Component;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;

import io.sensinghub.auth.CurrentUser;
import io.sensinghub.auth.Role;
import io.sensinghub.chat.ChatFollowUps.DocFacts;
import io.sensinghub.chat.ChatFollowUps.SearchFacts;
import io.sensinghub.common.ApiException;
import io.sensinghub.common.UrlNormalizer;
import io.sensinghub.digest.DigestComposer;
import io.sensinghub.document.DocumentService;
import io.sensinghub.document.DocumentService.DocumentRow;
import io.sensinghub.document.DocumentService.Filter;
import io.sensinghub.document.DocumentService.SiteMatch;
import io.sensinghub.document.HybridSearch;
import io.sensinghub.document.SearchTerms;
import io.sensinghub.issue.IssueService;
import io.sensinghub.issue.IssueService.IssueRow;
import io.sensinghub.llm.LlmClient;
import io.sensinghub.registration.RegistrationService;
import io.sensinghub.registration.RegistrationService.Registration;
import io.sensinghub.watch.WatchService;
import io.sensinghub.watch.WatchService.WatchView;

/**
 * 챗봇 도구 (1단계). LLM은 사용자 말을 이 도구 호출로 바꿀 뿐이고, 조회·등록·권한·중복 판정은 화면과 같은 서비스가 한다.
 * <ul>
 *   <li>도구 결과는 LLM에 보내는 JSON 문자열이다. 문서·이슈에는 이번 턴의 근거 번호(ref)를 붙이고, 답에 [ref]로 쓰게 한다
 *       ({@link Refs}). URL은 LLM에 보내지 않고 화면이 번호에 원문 링크를 붙인다.</li>
 *   <li>언론 기사와 AI 봇을 막은 사이트(site.ai_restricted)의 문서는 제목·분류만 보낸다 (요약 없음). 공공기관·기타 문서는 저장된 요약을 보낸다.</li>
 *   <li>API 키는 받지 않는다: 키가 필요하면 관리자 'API 키' 화면으로 안내만 한다 (대화가 DB에 영구 저장되므로).</li>
 *   <li>도구 오류(권한·중복·형식)는 예외가 아니라 {@code {"ok": false, "error": "..."}} 결과로 돌려 LLM이 그대로 전하게 한다.</li>
 *   <li>문서 검색은 2단계부터 하이브리드({@link HybridSearch}: 낱말 일치 + 의미 유사도, RRF)다. 문서마다 match(낱말·의미·낱말+의미)를
 *       붙여 LLM이 '뜻만 가까운' 문서를 가려 쓰게 한다. 임베딩이 없거나 실패하면 1단계와 같은 전문 검색(최신순)이다.</li>
 *   <li>사이트(site)·기사 종류(section)는 검색어가 아니라 조건이다. LLM이 이름을 정확히 모를 수 있어 종류 이름은 그 사이트의 실제 이름에
 *       맞추고(같은 이름, 없으면 포함하는 이름), 못 맞추면 문서 대신 고를 수 있는 이름(sites·site_sections)을 준다. 검색어 낱말이 하나도
 *       맞지 않았는데 검색어에 사이트·종류 이름이 있으면 site·section으로 거르라고 note로 알려 준다.</li>
 *   <li>앞 결과 안에서 다시 거르기: search_documents의 within_previous(·previous_refs)면 새로 찾지 않고 앞 답의 근거({@link Context#previous})
 *       안에서만 기존 조건(검색어·분류·사이트·종류·기간·감시)으로 거른다. 문서 id는 서버가 chat_citation에서 꺼낸다 — LLM은 표시와 근거 번호만 쓴다.
 *       이슈 근거는 그 이슈에 묶인 문서로 넓힌다. 이때는 의미 검색 없이 낱말·태그·회사·사이트 이름이 맞는 문서만, 앞 답의 순서로 준다.</li>
 *   <li>도구는 결과와 함께 추천 후속 질문용 요약({@link Outcome#facts()}, {@link ChatFollowUps})을 남긴다.</li>
 * </ul>
 */
@Component
public class ChatTools {

    public static final String SEARCH_DOCUMENTS = "search_documents";
    public static final String LIST_ISSUES = "list_issues";
    public static final String LIST_WATCHES = "list_watches";
    public static final String CREATE_WATCH = "create_watch";
    public static final String REGISTER_SOURCE = "register_source";
    public static final String GET_SOURCE_STATUS = "get_source_status";

    static final int DEFAULT_DOCUMENTS = 10;
    static final int MAX_DOCUMENTS = 20;
    static final int DEFAULT_ISSUES = 10;
    static final int MAX_ISSUES = 20;
    static final int SUMMARY_CHARS = 300;
    static final int SECTION_HINTS = 20;
    static final int MAX_WATCHES = 30;
    /** 앞 답의 근거 안에서 거를 때: 이슈 근거 하나에서 꺼낼 문서 수, 거를 문서 수의 상한. */
    static final int MAX_ISSUE_DOCUMENTS = 50;
    static final int MAX_WITHIN_BASE = 100;
    /** 키워드 바로 검색 기간: 먼저 7일, 낱말이 든 문서가 없으면 30일. */
    static final int[] QUICK_DAYS = {7, 30};
    static final String CREDENTIALS_PATH = "#/admin/credentials";
    static final String CREDENTIALS_GUIDE = "API 키는 채팅으로 받지 않습니다. 관리자가 관리자 메뉴의 'API 키' 화면에서 등록하면 "
            + "그 키를 기다리던 소스가 자동으로 이어서 진행됩니다.";

    private static final Logger log = LoggerFactory.getLogger(ChatTools.class);
    private static final DateTimeFormatter KST_TIME = DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm");

    static final Map<String, String> LABELS = Map.of(
            SEARCH_DOCUMENTS, "문서 검색", LIST_ISSUES, "이슈 목록", LIST_WATCHES, "감시 목록",
            CREATE_WATCH, "감시 만들기", REGISTER_SOURCE, "사이트 등록", GET_SOURCE_STATUS, "진행 상태");

    static final Map<String, String> STATUS_LABELS = Map.ofEntries(
            Map.entry("RECEIVED", "접수"), Map.entry("PROCESSING", "진행 중"), Map.entry("REVIEW", "확인 필요(관리자 검토)"),
            Map.entry("AWAITING_APPROVAL", "관리자 승인 대기"), Map.entry("COMPLETED", "완료"), Map.entry("REJECTED", "반려"),
            Map.entry("DUPLICATE", "이미 등록됨"), Map.entry("INVALID", "형식 오류"), Map.entry("ACTIVE", "수집 중"),
            Map.entry("PENDING", "대기"), Map.entry("BROKEN", "중단(이상)"), Map.entry("BLOCKED", "차단"),
            Map.entry("PAUSED", "일시정지"), Map.entry("EXCLUDED", "제외"));

    static final Map<String, String> STEP_LABELS = Map.of(
            "QUEUED", "대기 중", "PROBE", "사전 점검", "DISCOVERY", "자동 탐색", "FIRST_CRAWL", "첫 수집", "DONE", "완료",
            "REJECTED", "반려");

    /** 근거 후보: 문서 또는 이슈. url은 문서면 원문, 이슈면 화면 주소(#/issues/{id}). */
    public record Ref(int no, String kind, Long documentId, Long issueId, String title, String url, String sourceLabel,
                      OffsetDateTime publishedAt) {
    }

    /** 한 턴 동안 도구 결과에 붙인 근거 번호. 같은 문서·이슈는 같은 번호를 받는다. */
    public static final class Refs {
        private final Map<String, Ref> byKey = new LinkedHashMap<>();
        private final Map<Integer, Ref> byNo = new HashMap<>();

        int document(long id, String title, String url, String sourceLabel, OffsetDateTime publishedAt) {
            return add("D" + id, no -> new Ref(no, "DOCUMENT", id, null, title, url, sourceLabel, publishedAt));
        }

        int issue(long id, String title, String sourceLabel, OffsetDateTime lastSeenAt) {
            return add("I" + id, no -> new Ref(no, "ISSUE", null, id, title, "#/issues/" + id, sourceLabel, lastSeenAt));
        }

        private int add(String key, java.util.function.IntFunction<Ref> make) {
            Ref existing = byKey.get(key);
            if (existing != null) {
                return existing.no();
            }
            Ref ref = make.apply(byKey.size() + 1);
            byKey.put(key, ref);
            byNo.put(ref.no(), ref);
            return ref.no();
        }

        public Ref get(int no) {
            return byNo.get(no);
        }

        public List<Ref> all() {
            return List.copyOf(byKey.values());
        }

        public boolean isEmpty() {
            return byKey.isEmpty();
        }
    }

    /**
     * @param content LLM에 보내고 TOOL 메시지로 저장하는 JSON
     * @param link    화면이 도구 아래에 보여 줄 바로 가기 (해시 경로, 없으면 null)
     * @param facts   추천 후속 질문용 조건·결과 요약 ({@link ChatFollowUps}, LLM에 보내지 않는다. 없으면 null)
     */
    public record Outcome(String content, boolean ok, String link, String linkLabel, ChatFollowUps.Facts facts) {

        public Outcome(String content, boolean ok, String link, String linkLabel) {
            this(content, ok, link, linkLabel, null);
        }
    }

    /**
     * 도구가 대화에서 알아야 할 것. previous: 앞 답의 근거 (근거가 있는 가장 최근 답의 chat_citation, 번호 순) —
     * search_documents의 within_previous·previous_refs가 그 안에서 거른다. LLM은 문서 id를 다루지 않고 이 표시만 쓴다.
     */
    public record Context(List<Ref> previous) {

        public static final Context NONE = new Context(List.of());

        public Context {
            previous = previous == null ? List.of() : List.copyOf(previous);
        }
    }

    private final HybridSearch search;
    private final DocumentService documents;
    private final IssueService issues;
    private final WatchService watches;
    private final RegistrationService registrations;
    private final JdbcClient jdbc;
    private final ObjectMapper json;

    public ChatTools(HybridSearch search, DocumentService documents, IssueService issues, WatchService watches,
                     RegistrationService registrations, JdbcClient jdbc, ObjectMapper json) {
        this.search = search;
        this.documents = documents;
        this.issues = issues;
        this.watches = watches;
        this.registrations = registrations;
        this.jdbc = jdbc;
        this.json = json;
    }

    public static String label(String toolName) {
        return LABELS.getOrDefault(toolName, toolName);
    }

    /** 스트리밍 단계 문구: 이 도구를 실행하는 동안 화면에 보일 말. */
    public static String stageLabel(LlmClient.ToolCall call) {
        Map<String, Object> args = call.arguments() == null ? Map.of() : call.arguments();
        return switch (call.name()) {
            case SEARCH_DOCUMENTS -> bool(args, "within_previous") || !integers(args, "previous_refs").isEmpty()
                    ? "앞 결과 안에서 거르는 중…" : "수집 기사 검색 중…";
            case LIST_ISSUES -> "이슈를 모으는 중…";
            case LIST_WATCHES -> "감시 목록을 보는 중…";
            case CREATE_WATCH -> "감시를 만드는 중…";
            case REGISTER_SOURCE -> "사이트를 등록하는 중…";
            case GET_SOURCE_STATUS -> "진행 상태를 확인하는 중…";
            default -> "도구를 실행하는 중…";
        };
    }

    /** 문서 검색 방식 (상태 표시용): "낱말+의미 (모델/차원)" 또는 "낱말". */
    public String searchMode() {
        return search.describe();
    }

    // ------------------------------------------------------------------ 정의

    public List<LlmClient.Tool> definitions() {
        return List.of(
                new LlmClient.Tool(SEARCH_DOCUMENTS,
                        "수집한 문서(언론 기사·공공기관 문서)를 찾는다. 검색어의 낱말을 모두 포함하는 문서(조사는 무시, 낱말 앞부분 일치)와 "
                                + "낱말은 달라도 뜻이 가까운 문서(의미 검색이 켜져 있을 때)를 관련도순으로 준다. 사이트(site, 예: 정책브리핑은 "
                                + "korea.kr, 동아일보)와 기사 종류(section, 예: 연설문·보도자료·정책뉴스, 언론사는 스포츠·경제 같은 섹션)로 거를 수 "
                                + "있다. 사이트·종류 이름은 query에 넣지 말고 site·section으로 준다: 다른 주제어가 없으면 query는 비운다 (최신순). "
                                + "사용자가 종류·섹션을 말하면 정확한 이름이 아니어도 그 낱말을 section에 준다 (도구가 실제 이름에 맞춘다: "
                                + "'브리핑' → '부처 브리핑'·'청와대 브리핑'). site를 주면 결과의 site_sections에 그 사이트의 기사 종류 이름이 온다. 문서의 match가 "
                                + "'의미'면 검색어 낱말은 없고 뜻만 가까운 문서다. 각 문서의 ref 번호를 답에 [1]처럼 붙인다. "
                                + "type이 '언론'인 문서는 제목·분류만 있고 내용은 없다. "
                                + "사용자가 '그중', '이 중', '위 기사 중', '앞 결과에서'처럼 앞 답의 목록을 가리키면 새로 찾지 말고 "
                                + "within_previous=true에 거를 조건(query·topic·site·section·days)을 준다: 앞 답의 근거 안에서만 거른다 "
                                + "('그중 삼성전자만' → query '삼성전자', '이 중 정책·규제만' → topic, '위 기사 중 이데일리 것만' → site '이데일리'). "
                                + "앞 답의 특정 번호를 가리키면 previous_refs ('2번 기사' → [2], '1번 이슈 언론사 목록' → [1]: 이슈 번호면 그 이슈의 기사들).",
                        object(Map.of(
                                "query", prop("string", "주제 검색어 (예: '반도체 수출', '가정폭력 피해자 보호'). 사이트·기사 종류 이름은 "
                                        + "넣지 않는다. 비우면 다른 조건에 맞는 최신 문서"),
                                "site", prop("string", "이 사이트의 문서만: 도메인 또는 사이트 이름 (예: 'korea.kr'(정책브리핑), "
                                        + "'yna.co.kr'(연합뉴스), '동아일보'). 도메인이 가장 정확하다"),
                                "section", prop("string", "이 기사 종류의 문서만 (예: '연설문', '보도자료', '정책뉴스', '스포츠', '문화'). "
                                        + "사용자가 말한 종류 낱말을 그대로 준다: 이름 일부면 그 낱말이 든 종류 모두, 없는 종류면 고를 수 있는 이름이 "
                                        + "결과에 온다. 여럿이면 쉼표로"),
                                "days", prop("integer", "최근 며칠 동안 수집한 문서만 (1~90)"),
                                "since", prop("string", "이 날짜(YYYY-MM-DD, 한국 시각) 이후 수집한 문서만. days보다 우선"),
                                "topic", enumProp("분류 (이 값 중 하나)", DigestComposer.TOPIC_ORDER),
                                "watch_id", prop("integer", "이 키워드 감시(list_watches의 id)에 걸린 문서만"),
                                "limit", prop("integer", "최대 개수 (기본 10, 최대 20)"),
                                "within_previous", prop("boolean", "true면 새로 찾지 않고 바로 앞 답의 근거 목록 안에서만 거른다 "
                                        + "(앞 답의 순서대로, 번호는 새로 매긴다). 앞 목록을 가리키는 말('그중', '이 중', '위 기사 중')에 쓴다"),
                                "previous_refs", Map.of("type", "array", "items", Map.of("type", "integer"),
                                        "description", "앞 답의 근거 번호 중 이것만 (within_previous와 함께). 이슈 번호면 그 이슈에 묶인 기사들")),
                                List.of())),
                new LlmClient.Tool(LIST_ISSUES,
                        "여러 언론사가 같은 사건을 보도한 기사 묶음(이슈)을 보도한 언론사가 많은 순으로 준다. "
                                + "'오늘 주요 이슈', '요즘 많이 보도된 일' 같은 질문에 쓴다. 각 이슈의 ref 번호를 답에 붙인다.",
                        object(Map.of(
                                "days", prop("integer", "마지막 기사가 최근 며칠 안인 이슈 (기본 3, 1~30)"),
                                "min_sources", prop("integer", "보도한 언론사 수 하한 (기본 2)"),
                                "query", prop("string", "이슈 제목·핵심 낱말 검색어"),
                                "limit", prop("integer", "최대 개수 (기본 10, 최대 20)")),
                                List.of())),
                new LlmClient.Tool(LIST_WATCHES,
                        "키워드 감시 목록 (공용 감시 + 내 감시, 관리자는 전체. 제외된 감시는 뺌): id, 이름, 키워드, 상태, 걸린 문서 수. "
                                + "감시 문서를 찾을 때 search_documents의 watch_id로 쓴다.",
                        object(Map.of(), List.of())),
                new LlmClient.Tool(CREATE_WATCH,
                        "키워드 감시를 새로 만든다. 사용자가 감시(모니터링)를 만들어 달라고 분명히 요청했고 이름과 키워드가 정해졌을 때만 부른다. "
                                + "등록 규칙은 화면과 같다 (일반 사용자는 관리자 승인 대기).",
                        object(Map.of(
                                "name", prop("string", "감시 이름 (예: '자사 보도')"),
                                "keywords", Map.of("type", "array", "items", Map.of("type", "string"),
                                        "description", "찾을 키워드들 (하나라도 들어간 기사가 걸린다)"),
                                "exclude", Map.of("type", "array", "items", Map.of("type", "string"),
                                        "description", "제외할 낱말들 (선택)")),
                                List.of("name", "keywords"))),
                new LlmClient.Tool(REGISTER_SOURCE,
                        "사이트(언론사·공공기관 등) 주소를 수집 대상으로 등록한다. 사용자가 등록·수집을 분명히 요청했을 때만 부른다. "
                                + "중복·권한은 시스템이 판정하고 점검·첫 수집은 자동으로 진행된다 (수십 초~수 분).",
                        object(Map.of("url", prop("string", "사이트 또는 기사 주소 (도메인 단위로 등록된다)")),
                                List.of("url"))),
                new LlmClient.Tool(GET_SOURCE_STATUS,
                        "사이트 등록·키워드 감시 요청의 진행 상태를 본다. registration_id나 url을 주면 그것을, 둘 다 비우면 "
                                + "내 최근 요청 5건을 준다.",
                        object(Map.of(
                                "registration_id", prop("integer", "등록 요청 번호"),
                                "url", prop("string", "사이트 주소 또는 도메인")),
                                List.of())));
    }

    private static Map<String, Object> object(Map<String, Object> properties, List<String> required) {
        Map<String, Object> schema = new LinkedHashMap<>();
        schema.put("type", "object");
        schema.put("properties", properties);
        if (!required.isEmpty()) {
            schema.put("required", required);
        }
        return schema;
    }

    private static Map<String, Object> prop(String type, String description) {
        return Map.of("type", type, "description", description);
    }

    private static Map<String, Object> enumProp(String description, List<String> values) {
        return Map.of("type", "string", "description", description, "enum", values);
    }

    // ------------------------------------------------------------------ 실행

    public Outcome execute(CurrentUser user, LlmClient.ToolCall call, Refs refs) {
        return execute(user, call, refs, Context.NONE);
    }

    public Outcome execute(CurrentUser user, LlmClient.ToolCall call, Refs refs, Context context) {
        Map<String, Object> args = call.arguments() == null ? Map.of() : call.arguments();
        try {
            return switch (call.name()) {
                case SEARCH_DOCUMENTS -> searchDocuments(user, args, refs, context == null ? Context.NONE : context);
                case LIST_ISSUES -> listIssues(user, args, refs);
                case LIST_WATCHES -> listWatches(user);
                case CREATE_WATCH -> createWatch(user, args);
                case REGISTER_SOURCE -> registerSource(user, args);
                case GET_SOURCE_STATUS -> sourceStatus(user, args);
                default -> failure("알 수 없는 도구입니다: " + call.name());
            };
        } catch (ApiException e) {
            return failure(e.getMessage());
        } catch (RuntimeException e) {
            log.error("chat tool {} failed", call.name(), e);
            return failure("도구를 실행하다 오류가 났습니다. 잠시 후 다시 시도해 주세요.");
        }
    }

    private Outcome searchDocuments(CurrentUser user, Map<String, Object> args, Refs refs, Context context) {
        String query = str(args, "query");
        String since = str(args, "since");
        Integer days = integer(args, "days");
        String topic = str(args, "topic");
        Long watchId = longValue(args, "watch_id");
        String siteArg = str(args, "site");
        List<String> askedSections = strings(args, "section");
        List<Integer> previousRefs = integers(args, "previous_refs");
        boolean within = bool(args, "within_previous") || !previousRefs.isEmpty();
        int limit = clamp(integer(args, "limit"), DEFAULT_DOCUMENTS, 1, MAX_DOCUMENTS);
        List<String> notes = new ArrayList<>();
        if (topic != null && !DigestComposer.TOPIC_ORDER.contains(topic)) {
            notes.add("topic '" + topic + "'은(는) 분류 체계에 없어 빼고 찾았습니다.");
            topic = null;
        }
        Integer period = since == null && days != null ? clamp(days, 7, 1, 90) : null;
        String from = since != null ? since
                : period != null ? OffsetDateTime.now(DigestComposer.KST).minusDays(period).toString() : null;
        String topicArg = topic;
        BiFunction<List<DocFacts>, Integer, SearchFacts> facts = (docs, previousCount) ->
                new SearchFacts(query, siteArg, askedSections, topicArg, period, watchId, within, previousCount, docs);

        // 앞 답의 근거 안에서 다시 거르기: 문서 id는 서버가 앞 답의 chat_citation에서 꺼낸다 (LLM은 표시만 준다)
        List<Long> base = null;
        if (within) {
            List<Ref> previous = context.previous();
            List<Ref> picked = previousRefs.isEmpty() ? previous
                    : previous.stream().filter(r -> previousRefs.contains(r.no())).toList();
            String problem = previous.isEmpty()
                    ? "앞 답에 근거 목록이 없어 그 안에서 거를 수 없습니다. 새로 찾으려면 within_previous 없이 다시 찾으세요."
                    : picked.isEmpty() ? "앞 답의 근거 번호는 " + previous.stream().map(r -> String.valueOf(r.no()))
                            .collect(Collectors.joining(", ")) + "입니다. previous_refs를 그 안에서 고르세요."
                    : null;
            base = problem == null ? previousDocuments(picked) : List.of();
            if (problem == null && base.isEmpty()) {
                problem = "앞 답의 근거에 기사·문서가 없습니다. 새로 찾으려면 within_previous 없이 다시 찾으세요.";
            }
            if (problem != null) {
                Map<String, Object> result = new LinkedHashMap<>();
                result.put("ok", true);
                result.put("within_previous", "앞 답의 근거 " + previous.size() + "건");
                result.put("count", 0);
                result.put("documents", List.of());
                result.put("note", problem);
                return success(result, null, null, facts.apply(List.of(), previous.size()));
            }
        }

        // 사이트·기사 종류: 이름을 실제 값으로 맞추고, 못 맞추면 고를 수 있는 이름을 돌려준다 (LLM이 이름을 정확히 모를 수 있다)
        Map<String, Object> scope = new LinkedHashMap<>();
        if (within) {
            scope.put("within_previous", "앞 답의 근거 " + base.size() + "건 안에서 (앞 답의 순서)");
        }
        Map<String, Long> siteSections = Map.of();
        if (siteArg != null) {
            List<SiteMatch> matched = documents.matchSites(siteArg);
            if (matched.isEmpty()) {
                return noMatch("'" + siteArg + "'과(와) 맞는 수집 사이트가 없습니다. sites의 도메인으로 다시 찾으세요.",
                        "sites", siteLabels(documents.sitesWithDocuments()));
            }
            scope.put("sites", siteLabels(matched));
            siteSections = documents.sectionCounts(user, siteOnly(siteArg));
        }
        List<String> sections = List.of();
        if (!askedSections.isEmpty()) {
            Map<String, Long> known = siteArg != null ? siteSections : documents.sectionCounts(user, Filter.NONE);
            sections = matchSections(askedSections, known.keySet());
            if (sections.isEmpty()) {
                return noMatch("기사 종류 '" + String.join("', '", askedSections) + "'이(가) " + (siteArg != null ? "이 사이트에 " : "")
                        + "없습니다. site_sections의 이름으로 다시 찾으세요.", "site_sections", top(known, SECTION_HINTS));
            }
            scope.put("sections", sections);
            if (!sections.equals(askedSections)) {
                notes.add("기사 종류 '" + String.join("', '", askedSections) + "'을(를) '" + String.join("', '", sections)
                        + "'(으)로 찾았습니다.");
            }
        }

        Filter filter = new Filter(from, null, topic, watchId, null, sections, siteArg, within ? base : List.of());
        HybridSearch.Result found = within ? searchWithin(user, filter, query, base, limit) : search.search(user, filter, query, limit);
        boolean hybrid = found.mode() == HybridSearch.Mode.HYBRID;
        Map<Long, SiteInfo> sites = sites(found.hits().stream().map(h -> h.row().sourceId()).distinct().toList());
        Map<Long, List<String>> companies = companies(found.hits().stream().map(h -> h.row().id()).toList());
        List<Map<String, Object>> out = new ArrayList<>();
        List<DocFacts> docFacts = new ArrayList<>();
        boolean meaningOnly = false;
        for (HybridSearch.Hit hit : found.hits()) {
            DocumentRow d = hit.row();
            SiteInfo site = sites.getOrDefault(d.sourceId(), new SiteInfo("UNKNOWN", null, false));
            String source = site.name() != null && !site.name().isBlank() ? site.name() : d.domain();
            int ref = refs.document(d.id(), d.title(), d.url(), source, d.publishedAt());
            List<String> linked = companies.getOrDefault(d.id(), List.of());
            docFacts.add(new DocFacts(d.id(), source, d.section(), d.topics(), d.tags(), linked));
            Map<String, Object> doc = new LinkedHashMap<>();
            doc.put("ref", ref);
            if (hybrid) {
                doc.put("match", hit.words() && hit.meaning() ? "낱말+의미" : hit.words() ? "낱말" : "의미");
                meaningOnly |= !hit.words();
            }
            doc.put("type", typeLabel(site.siteType()));
            doc.put("source", source);
            if (d.section() != null) {
                doc.put("section", d.section());
            }
            doc.put("title", d.title());
            doc.put("published", kst(d.publishedAt() != null ? d.publishedAt() : d.collectedAt()));
            if (!d.topics().isEmpty()) {
                doc.put("topics", d.topics());
            }
            if (!d.tags().isEmpty()) {
                doc.put("tags", d.tags());
            }
            if (!linked.isEmpty()) {
                doc.put("companies", linked);
            }
            if (!d.keywords().isEmpty()) {
                doc.put("watch_keywords", d.keywords());
            }
            if (d.issueId() != null && d.issueDocCount() != null && d.issueDocCount() > 1) {
                doc.put("issue_id", d.issueId());
                doc.put("issue_articles", d.issueDocCount());
            }
            boolean titleOnly = "NEWS".equals(site.siteType()) || site.aiRestricted();
            if (!titleOnly && d.summary() != null && !d.summary().isBlank()) {
                doc.put("summary", cut(d.summary(), SUMMARY_CHARS));
            } else if (site.aiRestricted() && !"NEWS".equals(site.siteType())) {
                doc.put("content", "이 사이트는 AI 이용을 막아 제목만 보여 줍니다");
            }
            out.add(doc);
        }
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("ok", true);
        result.putAll(scope);
        result.put("count", out.size());
        result.put("order", within ? "앞 답의 근거 순서" + (query != null ? " (검색어 낱말·태그·회사·사이트 이름이 맞는 문서)" : "")
                : switch (found.mode()) {
                    case HYBRID -> "관련도순 (낱말 일치와 의미 유사도를 합친 순위)";
                    case FULLTEXT -> "최신순 (검색어 낱말을 모두 포함한 문서)";
                    case LATEST -> "최신순";
                });
        if (out.size() == limit) {
            result.put("more", "더 있을 수 있습니다 (최대 " + limit + "건만 보냄)");
        }
        result.put("documents", out);
        if (!siteSections.isEmpty()) {
            result.put("site_sections", top(siteSections, SECTION_HINTS));
            if (sections.isEmpty()) {   // 실측: '브리핑 자료'를 묻고 site만 주어 종류와 상관없는 최신 문서로 답했다
                notes.add("종류를 거르지 않은 최신 문서입니다. 질문이 기사 종류(site_sections)를 말했다면 그 이름을 section에 주어 다시 찾으세요.");
            }
        }
        if (out.isEmpty()) {
            notes.add(within ? "앞 답의 근거 " + base.size() + "건 중 조건에 맞는 문서가 없습니다. 앞 목록에는 없다고 말하고, 새로 찾아볼지 "
                    + "물어봅니다 (새로 찾을 때는 within_previous 없이)." : "조건에 맞는 문서가 없습니다.");
        }
        if (query != null && found.hits().stream().noneMatch(HybridSearch.Hit::words)) {
            String hint = namesInQuery(user, query, siteArg, sections, siteSections);
            if (hint != null) {
                notes.add(hint);
            }
        }
        if (meaningOnly) {
            notes.add("match가 '의미'인 문서는 검색어 낱말은 없고 뜻만 가까운 문서입니다. 질문과 실제로 관련 있는 것만 답에 쓰세요.");
        }
        if (!notes.isEmpty()) {
            result.put("note", String.join(" ", notes));
        }
        return success(result, null, null, facts.apply(docFacts, within ? base.size() : 0));
    }

    /**
     * 앞 답의 근거 문서(base, 앞 답의 순서) 안에서 거른다. 의미 검색은 하지 않는다 (좁히기는 글자 그대로가 기대에 맞다).
     * 검색어가 있으면 낱말(제목·본문·요약 — 목록 검색과 같다)이나 태그·분류·연결된 회사·사이트 이름에 검색어 낱말이 모두 든 문서만.
     * 다른 조건(기간·분류·사이트·종류·감시)은 filter로 건다.
     */
    private HybridSearch.Result searchWithin(CurrentUser user, Filter filter, String query, List<Long> base, int limit) {
        List<DocumentRow> allowed = documents.list(user, filter, null, base.size());
        Set<Long> matched = null;
        if (query != null) {
            matched = new HashSet<>();
            for (DocumentRow r : documents.list(user, filter, query, base.size())) {
                matched.add(r.id());
            }
            matched.addAll(metaMatches(base, query));
        }
        Map<Long, Integer> order = new HashMap<>();
        for (int i = 0; i < base.size(); i++) {
            order.putIfAbsent(base.get(i), i);
        }
        Set<Long> keep = matched;
        List<HybridSearch.Hit> hits = allowed.stream().filter(r -> keep == null || keep.contains(r.id()))
                .sorted(Comparator.comparingInt(r -> order.getOrDefault(r.id(), Integer.MAX_VALUE)))
                .limit(limit).map(r -> new HybridSearch.Hit(r, query != null, false)).toList();
        return new HybridSearch.Result(hits, query != null ? HybridSearch.Mode.FULLTEXT : HybridSearch.Mode.LATEST,
                "within previous");
    }

    /** base 중 검색어 낱말이 모두 태그·분류·연결된 회사(이름·별칭)·사이트 이름·도메인에 든 문서 (제목에 없는 회사로도 거를 수 있게). */
    private List<Long> metaMatches(List<Long> base, String query) {
        List<String> terms = SearchTerms.terms(query);
        if (terms.isEmpty()) {
            terms = List.of(query.strip().toLowerCase(Locale.ROOT));
        }
        StringBuilder sql = new StringBuilder("""
                SELECT d.id FROM document d
                JOIN source s ON s.id = d.source_id
                JOIN site t ON t.id = s.site_id
                LEFT JOIN document_enrichment e ON e.document_id = d.id
                WHERE d.id IN (:ids)
                """);
        Map<String, Object> params = new HashMap<>();
        params.put("ids", base);
        for (int i = 0; i < terms.size(); i++) {
            String p = "t" + i;
            sql.append(" AND (COALESCE(e.tags::text, '') ILIKE :").append(p)
                    .append(" OR COALESCE(e.topics::text, '') ILIKE :").append(p)
                    .append(" OR COALESCE(t.name, '') ILIKE :").append(p)
                    .append(" OR t.domain ILIKE :").append(p)
                    .append(" OR EXISTS (SELECT 1 FROM document_company dc JOIN company c ON c.id = dc.company_id")
                    .append(" WHERE dc.document_id = d.id AND (c.name ILIKE :").append(p)
                    .append(" OR c.aliases::text ILIKE :").append(p).append(")))");
            params.put(p, SearchTerms.likePattern(terms.get(i)));
        }
        return jdbc.sql(sql.toString()).params(params).query(Long.class).list();
    }

    /** 앞 답의 근거 → 문서 id (근거 순서). 문서 근거는 그 문서, 이슈 근거는 그 이슈에 묶인 문서 (발행 순, 이슈마다 최대 {@link #MAX_ISSUE_DOCUMENTS}건). */
    private List<Long> previousDocuments(List<Ref> refs) {
        List<Long> out = new ArrayList<>();
        for (Ref r : refs) {
            if (r.documentId() != null) {
                out.add(r.documentId());
            } else if (r.issueId() != null) {
                out.addAll(jdbc.sql("""
                        SELECT idoc.document_id FROM issue_document idoc JOIN document d ON d.id = idoc.document_id
                        WHERE idoc.issue_id = ? ORDER BY d.published_at ASC NULLS LAST, d.id LIMIT ?
                        """).params(r.issueId(), MAX_ISSUE_DOCUMENTS).query(Long.class).list());
            }
        }
        return out.stream().distinct().limit(MAX_WITHIN_BASE).toList();
    }

    /** 문서마다 연결된 회사 이름 (document_company, 보관된 회사는 뺀다). */
    private Map<Long, List<String>> companies(List<Long> documentIds) {
        Map<Long, List<String>> out = new HashMap<>();
        if (documentIds.isEmpty()) {
            return out;
        }
        jdbc.sql("""
                SELECT dc.document_id, c.name FROM document_company dc JOIN company c ON c.id = dc.company_id
                WHERE dc.document_id IN (:ids) AND c.status = 'ACTIVE' ORDER BY dc.document_id, c.name
                """).param("ids", documentIds).query(rs -> {
                    out.computeIfAbsent(rs.getLong(1), k -> new ArrayList<>()).add(rs.getString(2));
                });
        return out;
    }

    /** 키워드 바로 검색 결과. words: 검색어 낱말이 든 문서 (false면 뜻만 가까운 문서). days: 실제로 찾은 기간. */
    public record QuickHit(Ref ref, boolean words) {
    }

    /** facts: 추천 후속 질문용 (검색어·기간·문서별 언론사·분류·태그·회사). */
    public record QuickResult(List<QuickHit> hits, int days, HybridSearch.Mode mode, ChatFollowUps.SearchFacts facts) {
    }

    /**
     * 키워드만 보낸 메시지를 LLM 없이 찾는다 (ChatService). search_documents와 같은 검색(하이브리드, 의미 검색이 없으면 최신순)을
     * 최근 {@link #QUICK_DAYS}[0]일에서 하고, 낱말이 든 문서가 없으면 {@link #QUICK_DAYS}[1]일로 넓힌다. LLM이 걸러 주지 않으므로
     * 뜻만 가까운 문서는 낱말이 든 문서 뒤로 보낸다. 근거 번호는 refs에 붙인다.
     */
    public QuickResult quickSearch(CurrentUser user, String query, Refs refs) {
        HybridSearch.Result found = null;
        int days = QUICK_DAYS[0];
        for (int d : QUICK_DAYS) {
            days = d;
            String from = OffsetDateTime.now(DigestComposer.KST).minusDays(d).toString();
            found = search.search(user, new Filter(from, null, null, null, null, List.of(), null), query, DEFAULT_DOCUMENTS);
            if (found.hits().stream().anyMatch(HybridSearch.Hit::words)) {
                break;
            }
        }
        List<HybridSearch.Hit> ordered = new ArrayList<>(found.hits().stream().filter(HybridSearch.Hit::words).toList());
        found.hits().stream().filter(h -> !h.words()).forEach(ordered::add);
        Map<Long, SiteInfo> sites = sites(ordered.stream().map(h -> h.row().sourceId()).distinct().toList());
        Map<Long, List<String>> companies = companies(ordered.stream().map(h -> h.row().id()).toList());
        List<QuickHit> hits = new ArrayList<>();
        List<DocFacts> docs = new ArrayList<>();
        for (HybridSearch.Hit hit : ordered) {
            DocumentRow d = hit.row();
            SiteInfo site = sites.getOrDefault(d.sourceId(), new SiteInfo("UNKNOWN", null, false));
            String source = site.name() != null && !site.name().isBlank() ? site.name() : d.domain();
            int no = refs.document(d.id(), d.title(), d.url(), source, d.publishedAt());
            hits.add(new QuickHit(refs.get(no), hit.words()));
            docs.add(new DocFacts(d.id(), source, d.section(), d.topics(), d.tags(), companies.getOrDefault(d.id(), List.of())));
        }
        return new QuickResult(hits, days, found.mode(),
                new SearchFacts(query, null, List.of(), null, days, null, false, 0, docs));
    }

    /**
     * 검색어 낱말과 맞는 문서가 없을 때, 검색어에 기사 종류·사이트 이름이 들어 있으면 site·section으로 거르라고 알려 준다.
     * "정책브리핑 연설문"을 query로 찾으면 그 낱말이 모두 든 문서만 찾으므로 대개 0건이다 (종류 이름은 본문 낱말이 아니다).
     */
    private String namesInQuery(CurrentUser user, String query, String siteArg, List<String> sections,
                                Map<String, Long> siteSections) {
        String q = compact(query);
        List<String> hints = new ArrayList<>();
        if (sections.isEmpty()) {
            Map<String, Long> known = siteArg != null ? siteSections : documents.sectionCounts(user, Filter.NONE);
            List<String> names = known.keySet().stream().filter(s -> compact(s).length() >= 2 && q.contains(compact(s)))
                    .limit(3).toList();
            if (!names.isEmpty()) {
                hints.add("'" + String.join("', '", names) + "'은(는) 기사 종류 이름입니다: query에서 빼고 section으로 거르세요.");
            }
        }
        if (siteArg == null) {
            List<String> names = siteLabels(documents.sitesWithDocuments().stream()
                    .filter(m -> q.contains(m.domain()) || compact(m.name()).length() >= 2 && q.contains(compact(m.name())))
                    .limit(3).toList());
            if (!names.isEmpty()) {
                hints.add("'" + String.join("', '", names) + "'은(는) 사이트입니다: query에서 빼고 site로 거르세요.");
            }
        }
        return hints.isEmpty() ? null : String.join(" ", hints);
    }

    /** 기사 종류·사이트를 찾지 못했을 때: 문서 없이 고를 수 있는 이름(choices)을 준다. */
    private Outcome noMatch(String note, String key, List<String> choices) {
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("ok", true);
        result.put("count", 0);
        result.put("documents", List.of());
        result.put(key, choices);
        result.put("note", note);
        return success(result, null, null);
    }

    /**
     * 요청한 기사 종류 이름을 실제 이름으로 맞춘다: 공백·대소문자를 무시하고 같은 이름, 없으면 그 이름을 포함하는 이름 모두
     * ('연설' → '연설문', '브리핑' → '부처 브리핑'·'청와대 브리핑'). 한 글자는 같은 이름만 본다.
     */
    static List<String> matchSections(List<String> asked, Collection<String> known) {
        List<String> out = new ArrayList<>();
        for (String a : asked) {
            String key = compact(a);
            List<String> same = known.stream().filter(k -> compact(k).equals(key)).toList();
            List<String> hits = !same.isEmpty() || key.length() < 2 ? same
                    : known.stream().filter(k -> compact(k).contains(key)).toList();
            hits.stream().filter(h -> !out.contains(h)).forEach(out::add);
        }
        return out;
    }

    private static Filter siteOnly(String site) {
        return new Filter(null, null, null, null, null, List.of(), site);
    }

    /** '중부매일(jbnews.com)', 이름이 없으면 도메인. 소스가 여럿인 사이트는 한 번. */
    private static List<String> siteLabels(List<SiteMatch> sites) {
        return sites.stream().map(m -> m.name() == null || m.name().isBlank() ? m.domain() : m.name() + "(" + m.domain() + ")")
                .distinct().toList();
    }

    private static List<String> top(Map<String, Long> counts, int n) {
        return counts.keySet().stream().limit(n).toList();
    }

    private static String compact(String s) {
        return s == null ? "" : s.replaceAll("\\s+", "").toLowerCase(Locale.ROOT);
    }

    private Outcome listIssues(CurrentUser user, Map<String, Object> args, Refs refs) {
        int days = clamp(integer(args, "days"), 3, 1, 30);
        Integer minSources = integer(args, "min_sources");
        int limit = clamp(integer(args, "limit"), DEFAULT_ISSUES, 1, MAX_ISSUES);
        String since = OffsetDateTime.now(DigestComposer.KST).minusDays(days).toString();
        List<IssueRow> rows = issues.list(user, since, 2, minSources == null ? 2 : Math.max(1, minSources),
                str(args, "query"), limit);
        List<Map<String, Object>> out = new ArrayList<>();
        List<ChatFollowUps.IssueItem> items = new ArrayList<>();
        for (IssueRow i : rows) {
            int ref = refs.issue(i.id(), i.title(), "언론사 " + i.sourceCount() + "곳 · 기사 " + i.docCount() + "건",
                    i.lastSeenAt());
            items.add(new ChatFollowUps.IssueItem(ref, i.title(), i.topTerms()));
            Map<String, Object> issue = new LinkedHashMap<>();
            issue.put("ref", ref);
            issue.put("title", i.title());
            issue.put("outlets", i.sourceCount());
            issue.put("articles", i.docCount());
            issue.put("first_article", kst(i.firstSeenAt()));
            issue.put("last_article", kst(i.lastSeenAt()));
            issue.put("terms", i.topTerms().stream().limit(8).toList());
            out.add(issue);
        }
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("ok", true);
        result.put("period", "마지막 기사가 최근 " + days + "일 안");
        result.put("count", out.size());
        result.put("issues", out);
        if (out.isEmpty()) {
            result.put("note", "조건에 맞는 이슈가 없습니다. 기간을 늘리거나 언론사 수 조건을 낮춰 볼 수 있습니다.");
        }
        return success(result, "#/issues", "이슈 화면", new ChatFollowUps.IssueFacts(days, items));
    }

    private Outcome listWatches(CurrentUser user) {
        List<WatchView> list = watches.list(user);   // 제외(EXCLUDED)된 감시는 빠져 있다
        List<Map<String, Object>> out = new ArrayList<>();
        for (WatchView w : list.stream().limit(MAX_WATCHES).toList()) {
            out.add(watchJson(w));
        }
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("ok", true);
        result.put("scope", user.role().atLeast(Role.ADMIN) ? "전체 감시 (관리자)" : "공용 감시(관리자가 만든 것) + 내가 만든 감시");
        result.put("count", list.size());
        result.put("watches", out);
        if (out.isEmpty()) {
            result.put("note", "키워드 감시가 없습니다. 감시 이름과 키워드를 알려 주면 만들 수 있다고 안내합니다.");
        }
        return success(result, "#/watches", "키워드 감시 화면", new ChatFollowUps.WatchFacts(list.stream().limit(MAX_WATCHES)
                .map(w -> new ChatFollowUps.WatchItem(w.name(), w.documentCount())).toList()));
    }

    private Outcome createWatch(CurrentUser user, Map<String, Object> args) {
        String name = str(args, "name");
        List<String> keywords = strings(args, "keywords");
        if (name == null) {
            return failure("감시 이름(name)이 필요합니다. 사용자에게 물어보세요.");
        }
        if (keywords.isEmpty()) {
            return failure("키워드(keywords)가 하나 이상 필요합니다. 사용자에게 물어보세요.");
        }
        WatchView w = watches.create(user, name, keywords, strings(args, "exclude"), RegistrationService.Channel.CHAT);
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("ok", true);
        result.put("watch", watchJson(w));
        result.put("message", w.registrationMessage());
        return success(result, "#/watches", "키워드 감시 화면", new ChatFollowUps.ActionFacts(CREATE_WATCH));
    }

    private Map<String, Object> watchJson(WatchView w) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("id", w.id());
        m.put("name", w.name());
        if (w.shared()) {
            m.put("shared", true);
        }
        m.put("keywords", w.keywords());
        if (!w.exclude().isEmpty()) {
            m.put("exclude", w.exclude());
        }
        m.put("status", statusLabel(w.status()));
        if ("ACTIVE".equals(w.status())) {
            m.put("mode", w.naver() ? "수집 기사 대조 + 네이버 뉴스 검색" : "수집 기사 대조");
        }
        m.put("documents", w.documentCount());
        if (w.registrationStatus() != null && !"COMPLETED".equals(w.registrationStatus())) {
            m.put("request", statusLabel(w.registrationStatus()));
        }
        return m;
    }

    private Outcome registerSource(CurrentUser user, Map<String, Object> args) {
        String url = str(args, "url");
        if (url == null) {
            return failure("등록할 주소(url)가 필요합니다. 사용자에게 물어보세요.");
        }
        Registration r = registrations.register(user, url, RegistrationService.Channel.CHAT);
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("ok", true);
        result.put("registration", registrationJson(r));
        if ("PROCESSING".equals(r.status()) || "RECEIVED".equals(r.status())) {
            result.put("note", "점검·자동 탐색·첫 수집은 시스템이 자동으로 진행하며 수십 초~수 분 걸립니다. "
                    + "결과는 아직 모르므로 약속하지 말고, 나중에 진행 상태를 물어보면 알려 줄 수 있다고 안내합니다.");
        }
        return success(result, "#/registrations", "등록·내 요청 화면", new ChatFollowUps.ActionFacts(REGISTER_SOURCE));
    }

    private Outcome sourceStatus(CurrentUser user, Map<String, Object> args) {
        Long id = longValue(args, "registration_id");
        String url = str(args, "url");
        List<Registration> list;
        String domain = null;
        if (id != null) {
            Registration r = registrations.get(id);
            if (!user.role().atLeast(Role.ADMIN) && registrations.mine(user).stream().noneMatch(m -> m.id() == id)) {
                return failure("등록 요청 #" + id + "을(를) 찾을 수 없습니다 (내 요청만 볼 수 있습니다).");
            }
            list = List.of(r);
        } else if (url != null) {
            domain = UrlNormalizer.normalize(url).domain();
            String d = domain;
            list = registrations.mine(user).stream().filter(r -> d.equals(r.domain())).limit(3).toList();
        } else {
            list = registrations.mine(user).stream().limit(5).toList();
        }
        List<Map<String, Object>> out = new ArrayList<>();
        boolean keyNeeded = false;
        for (Registration r : list) {
            Map<String, Object> item = registrationJson(r);
            if (r.sourceId() != null) {
                Map<String, Object> source = sourceJson(r.sourceId());
                if (source != null) {
                    item.put("source", source);
                    keyNeeded |= source.containsKey("api_key");
                }
            }
            out.add(item);
        }
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("ok", true);
        result.put("requests", out);
        if (domain != null && out.isEmpty()) {
            // 내 요청은 없지만 이미 수집 중인 사이트인지는 알려 준다 (중복 등록 시에도 같은 정보가 보인다)
            Long sourceId = jdbc.sql("""
                    SELECT s.id FROM source s JOIN site t ON t.id = s.site_id
                    WHERE t.domain = ? AND s.kind = 'SITE' ORDER BY (s.status = 'EXCLUDED'), s.id LIMIT 1
                    """).param(domain).query(Long.class).optional().orElse(null);
            Map<String, Object> source = sourceId == null ? null : sourceJson(sourceId);
            if (source != null) {
                source.put("domain", domain);
                result.put("site", source);
                keyNeeded |= source.containsKey("api_key");
            } else {
                result.put("note", domain + "은(는) 등록된 사이트가 아닙니다. 등록해 달라고 하면 등록할 수 있습니다.");
            }
        } else if (out.isEmpty()) {
            result.put("note", "등록 요청이 없습니다.");
        }
        return keyNeeded ? success(result, CREDENTIALS_PATH, "API 키 화면(관리자)")
                : success(result, "#/registrations", "등록·내 요청 화면");
    }

    private Map<String, Object> registrationJson(Registration r) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("registration_id", r.id());
        m.put("input", r.inputUrl());
        if (r.inputUrl() == null || !r.inputUrl().startsWith("watch:")) {
            m.put("domain", r.domain());
        }
        m.put("status", statusLabel(r.status()));
        if (r.step() != null) {
            m.put("step", STEP_LABELS.getOrDefault(r.step(), r.step()));
        }
        if (r.resultMessage() != null) {
            m.put("message", r.resultMessage());
        }
        m.put("requested_at", kst(r.createdAt()));
        if (r.batchId() != null) {
            m.put("batch", r.batchId());
        }
        return m;
    }

    /** 소스 상태 (관리자 화면의 소스 상세는 ADMIN 전용이라 요청자에게 필요한 만큼만 직접 읽는다). */
    private Map<String, Object> sourceJson(long sourceId) {
        return jdbc.sql("""
                SELECT s.status, s.status_reason, s.last_success_at, t.risk_flags->>'api_provider' AS provider,
                       (SELECT count(*) FROM document d WHERE d.source_id = s.id) AS documents
                FROM source s JOIN site t ON t.id = s.site_id WHERE s.id = ?
                """).param(sourceId).query((rs, n) -> {
                    Map<String, Object> m = new LinkedHashMap<>();
                    String status = rs.getString("status");
                    String reason = rs.getString("status_reason");
                    m.put("status", statusLabel(status));
                    if (reason != null) {
                        m.put("reason", reason);
                    }
                    OffsetDateTime last = rs.getObject("last_success_at", OffsetDateTime.class);
                    if (last != null) {
                        m.put("last_collected", kst(last));
                    }
                    m.put("documents", rs.getLong("documents"));
                    if ("REVIEW".equals(status) && ("API_KEY_REQUIRED".equals(reason) || "API_KEY_INVALID".equals(reason))) {
                        Map<String, Object> key = new LinkedHashMap<>();
                        key.put("provider", rs.getString("provider"));
                        key.put("state", "API_KEY_INVALID".equals(reason) ? "등록된 키가 틀렸습니다" : "키가 없습니다");
                        key.put("guide", CREDENTIALS_GUIDE);
                        m.put("api_key", key);
                    }
                    return m;
                }).optional().orElse(null);
    }

    // ------------------------------------------------------------------ 도우미

    private record SiteInfo(String siteType, String name, boolean aiRestricted) {
    }

    private Map<Long, SiteInfo> sites(List<Long> sourceIds) {
        Map<Long, SiteInfo> out = new HashMap<>();
        if (sourceIds.isEmpty()) {
            return out;
        }
        jdbc.sql("""
                SELECT s.id, t.site_type, t.name, t.ai_restricted FROM source s JOIN site t ON t.id = s.site_id
                WHERE s.id IN (:ids)
                """).param("ids", sourceIds).query(rs -> {
                    out.put(rs.getLong("id"), new SiteInfo(rs.getString("site_type"), rs.getString("name"),
                            rs.getBoolean("ai_restricted")));
                });
        return out;
    }

    static String typeLabel(String siteType) {
        if ("NEWS".equals(siteType)) {
            return "언론";
        }
        if ("PUBLIC".equals(siteType)) {
            return "공공기관";
        }
        return "기타";
    }

    static String statusLabel(String status) {
        return status == null ? null : STATUS_LABELS.getOrDefault(status, status);
    }

    static String kst(OffsetDateTime t) {
        return t == null ? null : t.atZoneSameInstant(DigestComposer.KST).format(KST_TIME);
    }

    private static String cut(String s, int max) {
        String flat = s.replaceAll("\\s+", " ").strip();
        return flat.length() > max ? flat.substring(0, max) + "…" : flat;
    }

    private Outcome success(Map<String, Object> result, String link, String linkLabel) {
        return success(result, link, linkLabel, null);
    }

    private Outcome success(Map<String, Object> result, String link, String linkLabel, ChatFollowUps.Facts facts) {
        return new Outcome(toJson(result), true, link, linkLabel, facts);
    }

    private Outcome failure(String message) {
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("ok", false);
        result.put("error", message);
        return new Outcome(toJson(result), false, null, null);
    }

    private String toJson(Object value) {
        try {
            return json.writeValueAsString(value);
        } catch (JsonProcessingException e) {
            throw new IllegalStateException(e);
        }
    }

    static String str(Map<String, Object> args, String key) {
        Object v = args.get(key);
        if (v == null) {
            return null;
        }
        String s = String.valueOf(v).strip();
        return s.isEmpty() ? null : s;
    }

    static Integer integer(Map<String, Object> args, String key) {
        Long v = longValue(args, key);
        return v == null ? null : (int) Math.max(Integer.MIN_VALUE, Math.min(Integer.MAX_VALUE, v));
    }

    static Long longValue(Map<String, Object> args, String key) {
        Object v = args.get(key);
        if (v instanceof Number n) {
            return n.longValue();
        }
        String s = str(args, key);
        if (s == null) {
            return null;
        }
        try {
            return (long) Double.parseDouble(s);
        } catch (NumberFormatException e) {
            return null;
        }
    }

    /** true·"true"·1만 참. */
    static boolean bool(Map<String, Object> args, String key) {
        Object v = args.get(key);
        if (v instanceof Boolean b) {
            return b;
        }
        if (v instanceof Number n) {
            return n.intValue() == 1;
        }
        return v != null && "true".equalsIgnoreCase(String.valueOf(v).strip());
    }

    /** 정수 배열, 쉼표로 이은 문자열, 또는 정수 하나. 정수가 아닌 값은 뺀다. */
    static List<Integer> integers(Map<String, Object> args, String key) {
        List<Integer> out = new ArrayList<>();
        Object v = args.get(key);
        if (v instanceof Number n) {
            out.add(n.intValue());
            return out;
        }
        for (String s : strings(args, key)) {
            try {
                out.add((int) Double.parseDouble(s));
            } catch (NumberFormatException ignored) {
                // 번호가 아니다
            }
        }
        return out;
    }

    /** 배열 또는 쉼표로 이은 문자열. */
    static List<String> strings(Map<String, Object> args, String key) {
        Object v = args.get(key);
        List<String> out = new ArrayList<>();
        if (v instanceof List<?> list) {
            for (Object o : list) {
                if (o != null && !String.valueOf(o).isBlank()) {
                    out.add(String.valueOf(o).strip());
                }
            }
        } else if (v != null) {
            for (String part : String.valueOf(v).split(",")) {
                if (!part.isBlank()) {
                    out.add(part.strip());
                }
            }
        }
        return out;
    }

    static int clamp(Integer value, int fallback, int min, int max) {
        int v = value == null ? fallback : value;
        return Math.max(min, Math.min(max, v));
    }
}
