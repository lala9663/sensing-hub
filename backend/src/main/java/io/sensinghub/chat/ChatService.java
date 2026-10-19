package io.sensinghub.chat;

import java.time.OffsetDateTime;
import java.time.ZonedDateTime;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.locks.ReentrantLock;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpStatus;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Service;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;

import io.sensinghub.auth.CurrentUser;
import io.sensinghub.auth.Role;
import io.sensinghub.chat.ChatTools.Outcome;
import io.sensinghub.chat.ChatTools.Ref;
import io.sensinghub.chat.ChatTools.Refs;
import io.sensinghub.common.ApiException;
import io.sensinghub.llm.LlmClient;
import io.sensinghub.llm.LlmClient.Message;
import io.sensinghub.llm.LlmClient.ToolCall;
import io.sensinghub.llm.LlmException;

/**
 * 챗봇 1단계: 도구 호출형 대화, 대화 저장, 근거(인용) 저장. 의미 검색(임베딩)은 2단계.
 * <ul>
 *   <li>한 턴 = 사용자 메시지 → (LLM 도구 호출 → 도구 실행)을 최대 {@code sensing.chat.max-tool-rounds}번 → LLM 답.
 *       한도에 닿으면 도구 없이(tool_choice none) 지금까지의 결과로 답하게 한다.</li>
 *   <li>모든 단계를 chat_message에 남긴다: USER, ASSISTANT(tool_calls), TOOL(결과 JSON, meta.toolCallId), ASSISTANT(답).
 *       답에 쓴 근거([n])는 chat_citation에 제목·링크 스냅숏으로 남긴다. 답이 번호를 하나도 쓰지 않았으면 이번 턴 도구 결과의 근거를 남기고,
 *       도구 없이 답한 후속 질문의 [n]은 앞 답의 근거를 이어 쓴다 (번호는 답마다 1부터).</li>
 *   <li>LLM 오류(미설정·429·5xx·시간 초과)는 예외가 아니라 안내 문장의 답(meta.error)으로 저장하고 돌려준다.
 *       오류로 끝난 턴은 다음 턴에 LLM에 다시 보여 주지 않아 대화가 꼬이지 않는다.</li>
 *   <li>세션은 만든 사람만 본다 (관리자도 남의 대화는 404). 같은 세션에 동시에 두 질문은 받지 않는다 (409).</li>
 *   <li>LLM 호출 동안 DB 트랜잭션을 잡지 않는다: 메시지는 한 줄씩 저장하고, 도구(감시 생성·등록)는 각 서비스의 트랜잭션을 쓴다.</li>
 * </ul>
 */
@Service
public class ChatService {

    static final int MAX_CONTENT = 2000;
    static final int MAX_CALLS_PER_ROUND = 5;
    static final int MAX_FALLBACK_CITATIONS = 10;
    static final int HISTORY_CHAR_BUDGET = 40_000;
    static final int OLD_TOOL_RESULT_CHARS = 4_000;
    static final int TITLE_CHARS = 40;
    static final String SECRET_NOTICE = "API 키로 보이는 값은 저장하지 않고 지웠습니다. 키는 관리자가 'API 키' 화면에서 등록합니다.";
    static final String EMPTY_ANSWER = "답을 만들지 못했습니다. 질문을 조금 바꿔 다시 시도해 주세요.";
    static final String ROUND_LIMIT_ANSWER = "도구를 여러 번 불러도 답을 정리하지 못했습니다. 질문을 좁혀 다시 물어봐 주세요.";

    private static final Logger log = LoggerFactory.getLogger(ChatService.class);
    private static final Pattern REF = Pattern.compile("\\[(\\d{1,3}(?:\\s*,\\s*\\d{1,3})*)\\]");
    private static final TypeReference<Map<String, Object>> MAP = new TypeReference<>() { };
    private static final TypeReference<List<Map<String, Object>>> LIST = new TypeReference<>() { };

    public record SessionView(long id, String title, OffsetDateTime createdAt, OffsetDateTime lastMessageAt) {
    }

    /** kind DOCUMENT(url은 원문) | ISSUE(url은 화면 주소 #/issues/{id}). */
    public record CitationView(int refNo, String kind, Long documentId, Long issueId, String title, String url,
                               String sourceLabel, OffsetDateTime publishedAt) {
    }

    /** 답을 만들며 부른 도구. link는 화면 바로 가기(해시 경로). */
    public record ToolUseView(String name, String label, boolean ok, String link, String linkLabel) {
    }

    /** role USER | ASSISTANT. error: LLM 오류 안내로 끝난 답. */
    public record MessageView(long id, String role, String content, OffsetDateTime createdAt, boolean error,
                              List<CitationView> citations, List<ToolUseView> tools) {
    }

    public record SessionDetail(SessionView session, List<MessageView> messages) {
    }

    /** notice: 사용자 글에서 키처럼 보이는 값을 지웠을 때 등, 답과 별도로 화면에 보일 안내. */
    public record TurnResult(SessionView session, MessageView user, MessageView assistant, String notice) {
    }

    public record Status(boolean configured, String provider, int maxToolRounds) {
    }

    private final JdbcClient jdbc;
    private final ObjectMapper json;
    private final LlmClient llm;
    private final ChatTools tools;
    private final int maxToolRounds;
    private final int historyTurns;
    private final Map<Long, ReentrantLock> locks = new ConcurrentHashMap<>();

    public ChatService(JdbcClient jdbc, ObjectMapper json, LlmClient llm, ChatTools tools,
                       @Value("${sensing.chat.max-tool-rounds:4}") int maxToolRounds,
                       @Value("${sensing.chat.history-turns:6}") int historyTurns) {
        this.jdbc = jdbc;
        this.json = json;
        this.llm = llm;
        this.tools = tools;
        this.maxToolRounds = Math.max(1, maxToolRounds);
        this.historyTurns = Math.max(0, historyTurns);
    }

    public Status status(CurrentUser user) {
        user.require(Role.VIEWER);
        return new Status(llm.configured(), llm.describe(), maxToolRounds);
    }

    // ------------------------------------------------------------------ 세션

    public SessionView create(CurrentUser user, String title) {
        user.require(Role.VIEWER);
        String t = title == null || title.isBlank() ? null : shorten(title.strip(), 200);
        long id = jdbc.sql("INSERT INTO chat_session (user_id, title) VALUES (?, ?) RETURNING id")
                .params(user.id(), t).query(Long.class).single();
        return session(user, id);
    }

    public List<SessionView> list(CurrentUser user) {
        user.require(Role.VIEWER);
        return jdbc.sql(SESSION_SQL + " WHERE user_id = ? AND status = 'ACTIVE' ORDER BY last_message_at DESC, id DESC LIMIT 50")
                .param(user.id()).query(this::mapSession).list();
    }

    public SessionDetail detail(CurrentUser user, long id) {
        return new SessionDetail(session(user, id), messages(user, id));
    }

    /** 목록에서 지운다 (행은 감사용으로 남기고 status DELETED). */
    public void delete(CurrentUser user, long id) {
        session(user, id);
        jdbc.sql("UPDATE chat_session SET status = 'DELETED' WHERE id = ?").param(id).update();
    }

    /** 세션 소유자만. 남의 세션·지운 세션은 없는 것과 같다 (404). */
    SessionView session(CurrentUser user, long id) {
        user.require(Role.VIEWER);
        return jdbc.sql(SESSION_SQL + " WHERE id = ? AND user_id = ? AND status = 'ACTIVE'")
                .params(id, user.id()).query(this::mapSession).optional()
                .orElseThrow(() -> ApiException.notFound("대화"));
    }

    // ------------------------------------------------------------------ 메시지 보내기

    public TurnResult send(CurrentUser user, long sessionId, String content) {
        SessionView session = session(user, sessionId);
        String text = content == null ? "" : content.strip();
        if (text.isEmpty()) {
            throw new ApiException(HttpStatus.BAD_REQUEST, "BAD_REQUEST", "메시지를 입력해 주세요.");
        }
        if (text.length() > MAX_CONTENT) {
            throw new ApiException(HttpStatus.BAD_REQUEST, "BAD_REQUEST", "메시지는 " + MAX_CONTENT + "자 이하여야 합니다.");
        }
        ReentrantLock lock = locks.computeIfAbsent(sessionId, k -> new ReentrantLock());
        if (!lock.tryLock()) {
            throw ApiException.conflict("앞 질문에 답하는 중입니다. 답이 온 뒤에 다시 보내 주세요.");
        }
        try {
            return turn(user, session, text);
        } finally {
            lock.unlock();
        }
    }

    private TurnResult turn(CurrentUser user, SessionView session, String text) {
        long sessionId = session.id();
        SecretMasker.Result masked = SecretMasker.mask(text);
        List<Message> convo = new ArrayList<>(history(sessionId));
        Map<String, Object> userMeta = new LinkedHashMap<>();
        if (masked.masked() > 0) {
            userMeta.put("maskedSecrets", masked.masked());
        }
        long userMessageId = insert(sessionId, "USER", masked.text(), null, userMeta);
        if (session.title() == null) {
            jdbc.sql("UPDATE chat_session SET title = ? WHERE id = ? AND title IS NULL")
                    .params(shorten(masked.text().replaceAll("\\s+", " "), TITLE_CHARS), sessionId).update();
        }
        convo.add(Message.user(masked.text()));

        String system = ChatPrompt.system(user, ZonedDateTime.now());
        List<LlmClient.Tool> definitions = tools.definitions();
        Refs refs = new Refs();
        List<ToolUseView> uses = new ArrayList<>();
        int inputTokens = 0;
        int outputTokens = 0;
        int calls = 0;
        int rounds = 0;
        String answer;
        String error = null;
        try {
            while (true) {
                boolean allowTools = rounds < maxToolRounds;
                LlmClient.Response r = llm.chat(system, convo, definitions, allowTools);
                calls++;
                inputTokens += number(r.usage(), "inputTokens");
                outputTokens += number(r.usage(), "outputTokens");
                List<ToolCall> requested = r.toolCalls() == null ? List.of() : r.toolCalls();
                if (requested.isEmpty() || !allowTools) {
                    answer = r.text() == null ? "" : r.text().strip();
                    if (answer.isEmpty()) {
                        answer = requested.isEmpty() ? EMPTY_ANSWER : ROUND_LIMIT_ANSWER;
                        error = LlmException.Kind.BAD_RESPONSE.name();
                    }
                    break;
                }
                rounds++;
                List<ToolCall> run = requested.size() > MAX_CALLS_PER_ROUND
                        ? requested.subList(0, MAX_CALLS_PER_ROUND) : requested;
                Map<String, Object> roundMeta = new LinkedHashMap<>();
                roundMeta.put("round", rounds);
                insert(sessionId, "ASSISTANT", r.text() == null ? "" : r.text(), toolCallsJson(run), roundMeta);
                convo.add(Message.assistant(r.text(), run));
                for (ToolCall call : run) {
                    Outcome outcome = tools.execute(user, call, refs);
                    Map<String, Object> meta = new LinkedHashMap<>();
                    meta.put("toolCallId", call.id());
                    meta.put("name", call.name());
                    meta.put("ok", outcome.ok());
                    if (outcome.link() != null) {
                        meta.put("link", outcome.link());
                        meta.put("linkLabel", outcome.linkLabel());
                    }
                    insert(sessionId, "TOOL", outcome.content(), null, meta);
                    convo.add(Message.tool(call.id(), call.name(), outcome.content()));
                    uses.add(new ToolUseView(call.name(), ChatTools.label(call.name()), outcome.ok(), outcome.link(),
                            outcome.linkLabel()));
                }
            }
        } catch (LlmException e) {
            log.warn("chat LLM call failed: {} (HTTP {}) {}", e.kind(), e.httpStatus(), e.detail());
            answer = e.getMessage();
            error = e.kind().name();
        }

        // 도구를 하나도 부르지 않은 후속 답만 앞 답의 번호를 이어 쓴다 (등록·상태 답에 붙은 엉뚱한 [1]이 옛 기사로 이어지지 않게)
        List<Ref> cited = error != null ? List.of()
                : rounds == 0 ? carriedCitations(sessionId, answer) : citations(answer, refs);
        Map<String, Object> meta = new LinkedHashMap<>();
        meta.put("model", llm.describe());
        meta.put("llmCalls", calls);
        meta.put("toolRounds", rounds);
        meta.put("usage", Map.of("inputTokens", inputTokens, "outputTokens", outputTokens));
        if (error != null) {
            meta.put("error", error);
        }
        long answerId = insert(sessionId, "ASSISTANT", answer, null, meta);
        for (Ref ref : cited) {
            jdbc.sql("""
                    INSERT INTO chat_citation (message_id, ref_no, kind, document_id, issue_id, title_snapshot, url_snapshot,
                                               source_label, published_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """)
                    .params(answerId, ref.no(), ref.kind(), ref.documentId(), ref.issueId(), shorten(ref.title(), 1000),
                            shorten(ref.url(), 2000), shorten(ref.sourceLabel(), 300), ref.publishedAt())
                    .update();
        }
        jdbc.sql("UPDATE chat_session SET last_message_at = now() WHERE id = ?").param(sessionId).update();

        OffsetDateTime now = OffsetDateTime.now();
        MessageView userView = new MessageView(userMessageId, "USER", masked.text(), now, false, List.of(), List.of());
        MessageView answerView = new MessageView(answerId, "ASSISTANT", answer, now, error != null,
                cited.stream().map(ChatService::citationView).toList(), uses);
        return new TurnResult(session(user, sessionId), userView, answerView,
                masked.masked() > 0 ? SECRET_NOTICE : null);
    }

    /**
     * 답이 쓴 [n] 번호의 근거 (처음 나온 순서). 번호를 하나도 쓰지 않았으면 이번 턴 도구 결과의 근거를 앞에서부터 남긴다
     * (답은 그 결과로 만든 것이므로 "참고한 자료"로 보여 준다).
     */
    static List<Ref> citations(String answer, Refs refs) {
        if (refs.isEmpty()) {
            return List.of();
        }
        List<Ref> used = refNumbers(answer).stream().map(refs::get).filter(Objects::nonNull).toList();
        return used.isEmpty() ? refs.all().stream().limit(MAX_FALLBACK_CITATIONS).toList() : used;
    }

    /** 답에 나온 [n]·[n, m] 번호 (처음 나온 순서). */
    static Set<Integer> refNumbers(String answer) {
        Set<Integer> out = new LinkedHashSet<>();
        Matcher m = REF.matcher(answer == null ? "" : answer);
        while (m.find()) {
            for (String part : m.group(1).split(",")) {
                out.add(Integer.parseInt(part.strip()));
            }
        }
        return out;
    }

    /**
     * 도구를 부르지 않은 후속 답("1번 기사는 어디 거야?")의 [n]은 화면에 보이던 앞 답의 번호다: 근거가 있는 가장 최근 답에서 같은 번호를 가져온다.
     * 번호를 쓰지 않았으면 근거 없음.
     */
    private List<Ref> carriedCitations(long sessionId, String answer) {
        Set<Integer> used = refNumbers(answer);
        if (used.isEmpty()) {
            return List.of();
        }
        Long messageId = jdbc.sql("""
                SELECT c.message_id FROM chat_citation c JOIN chat_message m ON m.id = c.message_id
                WHERE m.session_id = ? ORDER BY c.message_id DESC LIMIT 1
                """).param(sessionId).query(Long.class).optional().orElse(null);
        if (messageId == null) {
            return List.of();
        }
        Map<Integer, Ref> previous = new LinkedHashMap<>();
        jdbc.sql("""
                SELECT ref_no, kind, document_id, issue_id, title_snapshot, url_snapshot, source_label, published_at
                FROM chat_citation WHERE message_id = ?
                """).param(messageId).query(rs -> {
                    previous.put(rs.getInt("ref_no"), new Ref(rs.getInt("ref_no"), rs.getString("kind"),
                            rs.getObject("document_id", Long.class), rs.getObject("issue_id", Long.class),
                            rs.getString("title_snapshot"), rs.getString("url_snapshot"), rs.getString("source_label"),
                            rs.getObject("published_at", OffsetDateTime.class)));
                });
        return used.stream().map(previous::get).filter(Objects::nonNull).toList();
    }

    private static CitationView citationView(Ref r) {
        return new CitationView(r.no(), r.kind(), r.documentId(), r.issueId(), r.title(), r.url(), r.sourceLabel(),
                r.publishedAt());
    }

    // ------------------------------------------------------------------ 기록

    private record Row(long id, int seq, String role, String content, String toolCalls, Map<String, Object> meta,
                       OffsetDateTime createdAt) {

        boolean finalAnswer() {
            return "ASSISTANT".equals(role) && toolCalls == null;
        }

        boolean failed() {
            return meta.get("error") != null;
        }
    }

    private List<Row> rows(long sessionId) {
        return jdbc.sql("""
                SELECT id, seq, role, content, tool_calls::text AS tool_calls, meta::text AS meta, created_at
                FROM chat_message WHERE session_id = ? ORDER BY seq
                """).param(sessionId).query((rs, n) -> new Row(rs.getLong("id"), rs.getInt("seq"), rs.getString("role"),
                        rs.getString("content"), rs.getString("tool_calls"), parseMap(rs.getString("meta")),
                        rs.getObject("created_at", OffsetDateTime.class))).list();
    }

    /** USER에서 시작해 다음 USER 전까지. */
    private static List<List<Row>> turns(List<Row> rows) {
        List<List<Row>> turns = new ArrayList<>();
        for (Row r : rows) {
            if ("USER".equals(r.role()) || turns.isEmpty()) {
                turns.add(new ArrayList<>());
            }
            turns.get(turns.size() - 1).add(r);
        }
        return turns;
    }

    /**
     * LLM에 다시 보여 줄 지난 대화: 답으로 끝난 턴만(오류로 끝났거나 답이 없는 턴은 뺀다), 최근 historyTurns턴,
     * 글자 수 예산 안에서. 도구 호출과 결과는 짝째로 넣어 이전 검색 결과·등록 번호를 이어서 쓸 수 있게 한다.
     */
    List<Message> history(long sessionId) {
        List<List<Row>> complete = new ArrayList<>();
        for (List<Row> turn : turns(rows(sessionId))) {
            Row last = turn.get(turn.size() - 1);
            if ("USER".equals(turn.get(0).role()) && last.finalAnswer() && !last.failed()) {
                complete.add(turn);
            }
        }
        List<List<Message>> picked = new ArrayList<>();
        int chars = 0;
        for (int i = complete.size() - 1; i >= 0 && picked.size() < historyTurns; i--) {
            List<Message> messages = new ArrayList<>();
            for (Row r : complete.get(i)) {
                messages.add(message(r));
            }
            int size = messages.stream().mapToInt(m -> m.content() == null ? 0 : m.content().length()).sum();
            if (!picked.isEmpty() && chars + size > HISTORY_CHAR_BUDGET) {
                break;
            }
            chars += size;
            picked.add(0, messages);
        }
        List<Message> out = new ArrayList<>();
        picked.forEach(out::addAll);
        return out;
    }

    private Message message(Row r) {
        return switch (r.role()) {
            case "USER" -> Message.user(r.content());
            case "TOOL" -> Message.tool(String.valueOf(r.meta().get("toolCallId")), String.valueOf(r.meta().get("name")),
                    shorten(r.content(), OLD_TOOL_RESULT_CHARS));
            default -> Message.assistant(r.content(), parseToolCalls(r.toolCalls()));
        };
    }

    /** 화면용: USER와 답(ASSISTANT)만, 답에 그 턴의 도구 사용과 근거를 붙인다. */
    private List<MessageView> messages(CurrentUser user, long sessionId) {
        Map<Long, List<CitationView>> citations = new LinkedHashMap<>();
        jdbc.sql("""
                SELECT c.message_id, c.ref_no, c.kind, c.document_id, c.issue_id, c.title_snapshot, c.url_snapshot,
                       c.source_label, c.published_at
                FROM chat_citation c JOIN chat_message m ON m.id = c.message_id
                WHERE m.session_id = ? ORDER BY c.message_id, c.ref_no
                """).param(sessionId).query(rs -> {
                    citations.computeIfAbsent(rs.getLong("message_id"), k -> new ArrayList<>()).add(new CitationView(
                            rs.getInt("ref_no"), rs.getString("kind"), rs.getObject("document_id", Long.class),
                            rs.getObject("issue_id", Long.class), rs.getString("title_snapshot"),
                            rs.getString("url_snapshot"), rs.getString("source_label"),
                            rs.getObject("published_at", OffsetDateTime.class)));
                });
        List<MessageView> out = new ArrayList<>();
        List<ToolUseView> pending = new ArrayList<>();
        for (Row r : rows(sessionId)) {
            switch (r.role()) {
                case "USER" -> {
                    pending = new ArrayList<>();
                    out.add(new MessageView(r.id(), "USER", r.content(), r.createdAt(), false, List.of(), List.of()));
                }
                case "TOOL" -> {
                    String name = String.valueOf(r.meta().get("name"));
                    pending.add(new ToolUseView(name, ChatTools.label(name), Boolean.TRUE.equals(r.meta().get("ok")),
                            (String) r.meta().get("link"), (String) r.meta().get("linkLabel")));
                }
                default -> {
                    if (r.finalAnswer()) {
                        out.add(new MessageView(r.id(), "ASSISTANT", r.content(), r.createdAt(), r.failed(),
                                citations.getOrDefault(r.id(), List.of()), List.copyOf(pending)));
                        pending = new ArrayList<>();
                    }
                }
            }
        }
        return out;
    }

    public List<MessageView> messageList(CurrentUser user, long sessionId) {
        session(user, sessionId);
        return messages(user, sessionId);
    }

    // ------------------------------------------------------------------ 저장 도우미

    private long insert(long sessionId, String role, String content, String toolCallsJson, Map<String, Object> meta) {
        return jdbc.sql("""
                INSERT INTO chat_message (session_id, seq, role, content, tool_calls, meta)
                VALUES (?, (SELECT COALESCE(MAX(seq), 0) + 1 FROM chat_message WHERE session_id = ?), ?, ?,
                        CAST(? AS jsonb), CAST(? AS jsonb))
                RETURNING id
                """)
                .params(sessionId, sessionId, role, content == null ? "" : content, toolCallsJson,
                        meta == null || meta.isEmpty() ? null : toJson(meta))
                .query(Long.class).single();
    }

    private String toolCallsJson(List<ToolCall> calls) {
        List<Map<String, Object>> out = new ArrayList<>();
        for (ToolCall c : calls) {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("id", c.id());
            m.put("name", c.name());
            m.put("arguments", c.arguments() == null ? Map.of() : c.arguments());
            if (c.providerData() != null && !c.providerData().isEmpty()) {
                m.put("providerData", c.providerData());
            }
            out.add(m);
        }
        return toJson(out);
    }

    @SuppressWarnings("unchecked")
    private List<ToolCall> parseToolCalls(String text) {
        if (text == null || text.isBlank()) {
            return List.of();
        }
        try {
            List<ToolCall> out = new ArrayList<>();
            for (Map<String, Object> m : json.readValue(text, LIST)) {
                Object args = m.get("arguments");
                Object extra = m.get("providerData");
                out.add(new ToolCall(String.valueOf(m.get("id")), String.valueOf(m.get("name")),
                        args instanceof Map<?, ?> a ? (Map<String, Object>) a : Map.of(),
                        extra instanceof Map<?, ?> e ? (Map<String, Object>) e : Map.of()));
            }
            return out;
        } catch (JsonProcessingException e) {
            return List.of();
        }
    }

    private Map<String, Object> parseMap(String text) {
        if (text == null || text.isBlank()) {
            return Map.of();
        }
        try {
            return json.readValue(text, MAP);
        } catch (JsonProcessingException e) {
            return Map.of();
        }
    }

    private String toJson(Object value) {
        try {
            return json.writeValueAsString(value);
        } catch (JsonProcessingException e) {
            throw new IllegalStateException(e);
        }
    }

    private static int number(Map<String, Object> usage, String key) {
        return usage != null && usage.get(key) instanceof Number n ? n.intValue() : 0;
    }

    static String shorten(String s, int max) {
        if (s == null) {
            return null;
        }
        return s.length() > max ? s.substring(0, max - 1) + "…" : s;
    }

    private static final String SESSION_SQL = "SELECT id, title, created_at, last_message_at FROM chat_session";

    private SessionView mapSession(java.sql.ResultSet rs, int n) throws java.sql.SQLException {
        return new SessionView(rs.getLong("id"), rs.getString("title"), rs.getObject("created_at", OffsetDateTime.class),
                rs.getObject("last_message_at", OffsetDateTime.class));
    }
}
