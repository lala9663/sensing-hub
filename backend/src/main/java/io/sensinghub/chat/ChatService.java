package io.sensinghub.chat;

import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.time.OffsetDateTime;
import java.time.ZonedDateTime;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.Optional;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
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
import io.sensinghub.chat.ChatFollowUps.FollowUp;
import io.sensinghub.chat.ChatTools.Outcome;
import io.sensinghub.chat.ChatTools.Ref;
import io.sensinghub.chat.ChatTools.Refs;
import io.sensinghub.common.ApiException;
import io.sensinghub.document.HybridSearch;
import io.sensinghub.keyword.FeaturedKeywordService;
import io.sensinghub.keyword.FeaturedKeywordService.FeaturedKeyword;
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
 *   <li>키워드만 보낸 메시지({@link ChatShortcuts#keyword}: "반도체", "삼성전자 DS")는 LLM을 부르지 않고 바로 찾아 답한다
 *       (USER와 답 두 줄, 답 meta.mode KEYWORD·llmCalls 0). 하루 호출 한도를 아끼고 답이 바로 나온다.</li>
 *   <li>"네이버 - 검색어" 형식({@link ChatShortcuts#naverQuery})은 화면이 네이버 결과를 직접 보여 준다 (표시 전용).
 *       이 경로로 오면 저장·LLM 호출 없이 400 NAVER_DISPLAY_ONLY.</li>
 *   <li>스트리밍({@link #begin} → {@link #run} with stream): 같은 턴을 돌리며 {@link TurnEvents}로 단계(질문을 살펴보는 중 → 도구별
 *       "수집 기사 검색 중…" → 근거를 정리하는 중)와 답 글 조각을 흘려보낸다. 저장·근거·답은 한 번에 받기와 똑같다 (끝나면 화면이 최종 답으로 바꾼다).
 *       도구 호출로 끝난 LLM 응답에 섞여 온 글은 reset으로 버린다. 키워드 바로 검색·오류 안내는 조각 없이 바로 답한다.</li>
 *   <li>답마다 추천 후속 질문 칩({@link ChatFollowUps}, LLM 호출 없이 규칙)을 meta.followUps에 저장한다. 오류 답에는 없다.</li>
 *   <li>도구에는 앞 답의 근거({@link #previousCitations})를 넘겨, "그중 ~만"이 새로 찾지 않고 그 안에서 거르게 한다
 *       (search_documents within_previous).</li>
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
    static final String MODE_KEYWORD = "KEYWORD";
    static final String NAVER_TRY = "네이버에서 찾아보기: 네이버 - ";
    static final String NAVER_ALSO = "네이버 뉴스 결과도 보기: 네이버 - ";
    static final String NAVER_DISPLAY_ONLY = "‘네이버 - 검색어’는 네이버 뉴스 결과를 화면에만 보여 주는 형식이라 대화로 보내지 않습니다 "
            + "(저장·AI 입력 안 함). 화면을 새로 고친 뒤 다시 입력해 주세요.";

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

    /**
     * role USER | ASSISTANT. error: LLM 오류 안내로 끝난 답. mode: null이면 LLM 답, KEYWORD면 LLM 없이 키워드로 바로 찾은 답
     * (content의 "[n] 제목 — 언론사 · 시각" 줄을 화면이 근거 줄로 그린다). errorKind: 오류 답의 종류(meta.error — LlmException.Kind 이름
     * 등, 오류가 아니면 null). 화면은 안내 문구가 아니라 이 값으로 오류 카드의 제목·도움말을 고른다 (문구는 바뀔 수 있다).
     */
    public record MessageView(long id, String role, String content, OffsetDateTime createdAt, boolean error,
                              List<CitationView> citations, List<ToolUseView> tools, String mode, String errorKind,
                              List<FollowUp> followUps) {
    }

    /**
     * 스트리밍 턴의 중간 알림 ({@link #run}). stage: READING(질문을 살펴보는 중) | TOOL(도구 실행 — tool에 이름) | COMPOSING(근거를 정리하는 중).
     * delta: 답 글 조각. reset: 지금까지 보낸 조각을 버린다 (도구 호출로 끝났거나 스트림이 끊겨 한 번에 받기로 다시 받는다).
     */
    public interface TurnEvents {

        TurnEvents NONE = new TurnEvents() { };

        default void stage(String stage, String tool, String label) {
        }

        default void delta(String text) {
        }

        default void reset() {
        }
    }

    /** 검사를 마치고 대화를 잡은(같은 대화에 다른 질문을 받지 않는) 질문. {@link #run} 또는 {@link #cancel}로 놓는다. */
    public record Pending(CurrentUser user, SessionView session, String text) {
    }

    public record SessionDetail(SessionView session, List<MessageView> messages) {
    }

    /** notice: 사용자 글에서 키처럼 보이는 값을 지웠을 때 등, 답과 별도로 화면에 보일 안내. */
    public record TurnResult(SessionView session, MessageView user, MessageView assistant, String notice) {
    }

    /** search: 문서 검색 방식 ("낱말+의미 (gemini-embedding-001/1024)" 또는 "낱말"). */
    public record Status(boolean configured, String provider, int maxToolRounds, String search) {
    }

    private final JdbcClient jdbc;
    private final ObjectMapper json;
    private final LlmClient llm;
    private final ChatTools tools;
    private final FeaturedKeywordService featuredKeywords;
    private final int maxToolRounds;
    private final int historyTurns;
    private final boolean streaming;
    /** 답하는 중인 대화 (스트리밍은 받은 스레드와 답하는 스레드가 달라 스레드에 묶인 잠금 대신 집합으로 잡는다). */
    private final Set<Long> busy = ConcurrentHashMap.newKeySet();

    public ChatService(JdbcClient jdbc, ObjectMapper json, LlmClient llm, ChatTools tools,
                       FeaturedKeywordService featuredKeywords,
                       @Value("${sensing.chat.max-tool-rounds:4}") int maxToolRounds,
                       @Value("${sensing.chat.history-turns:6}") int historyTurns,
                       @Value("${sensing.chat.stream:true}") boolean streaming) {
        this.jdbc = jdbc;
        this.json = json;
        this.llm = llm;
        this.tools = tools;
        this.featuredKeywords = featuredKeywords;
        this.maxToolRounds = Math.max(1, maxToolRounds);
        this.historyTurns = Math.max(0, historyTurns);
        this.streaming = streaming;
    }

    public Status status(CurrentUser user) {
        user.require(Role.VIEWER);
        return new Status(llm.configured(), llm.describe(), maxToolRounds, tools.searchMode());
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

    /** 질문 하나 → 답 하나 (한 번에 받기, POST /messages). */
    public TurnResult send(CurrentUser user, long sessionId, String content) {
        return run(begin(user, sessionId, content), TurnEvents.NONE, false);
    }

    /**
     * 보내기 전 검사(세션 404, 빈 글·길이·네이버 형식 400)를 하고 대화를 잡는다 (같은 대화에 답하는 중이면 409).
     * 스트리밍은 이 검사를 요청 스레드에서 끝내 오류를 보통 JSON 응답으로 돌려준 뒤 답은 다른 스레드에서 {@link #run}한다.
     */
    public Pending begin(CurrentUser user, long sessionId, String content) {
        SessionView session = session(user, sessionId);
        String text = content == null ? "" : content.strip();
        if (text.isEmpty()) {
            throw new ApiException(HttpStatus.BAD_REQUEST, "BAD_REQUEST", "메시지를 입력해 주세요.");
        }
        if (text.length() > MAX_CONTENT) {
            throw new ApiException(HttpStatus.BAD_REQUEST, "BAD_REQUEST", "메시지는 " + MAX_CONTENT + "자 이하여야 합니다.");
        }
        // 네이버 결과는 표시 전용: 화면이 직접 불러 보여 준다. 이 경로로 오면(옛 화면 등) 저장도 LLM 호출도 하지 않고 돌려보낸다
        if (ChatShortcuts.naverQuery(text).isPresent()) {
            throw new ApiException(HttpStatus.BAD_REQUEST, "NAVER_DISPLAY_ONLY", NAVER_DISPLAY_ONLY);
        }
        if (!busy.add(sessionId)) {
            throw ApiException.conflict("앞 질문에 답하는 중입니다. 답이 온 뒤에 다시 보내 주세요.");
        }
        return new Pending(user, session, text);
    }

    /**
     * 잡아 둔 질문에 답하고 대화를 놓는다.
     * @param stream true면 LLM 답 글을 스트리밍으로 받아 events.delta로 흘려보낸다 (설정 sensing.chat.stream=false면 한 번에 받는다)
     */
    public TurnResult run(Pending pending, TurnEvents events, boolean stream) {
        try {
            return turn(pending.user(), pending.session(), pending.text(), events == null ? TurnEvents.NONE : events,
                    stream && streaming);
        } finally {
            busy.remove(pending.session().id());
        }
    }

    /** 잡아 둔 질문을 답하지 않고 놓는다 (스트리밍을 시작하지 못했을 때). */
    public void cancel(Pending pending) {
        busy.remove(pending.session().id());
    }

    /** 한 턴의 답. mode: null(LLM 답) 또는 {@link #MODE_KEYWORD}. meta.error가 있으면 오류 안내 답이다. */
    private record Answer(String content, List<Ref> cited, List<ToolUseView> uses, Map<String, Object> meta,
                          String mode, List<FollowUp> followUps) {
    }

    private TurnResult turn(CurrentUser user, SessionView session, String text, TurnEvents events, boolean stream) {
        long sessionId = session.id();
        SecretMasker.Result masked = SecretMasker.mask(text);
        // 키로 보이는 값을 지운 글은 LLM 경로로 보낸다 (저장하지 않았다고 안내한다)
        Optional<String> keyword = masked.masked() > 0 ? Optional.empty() : ChatShortcuts.keyword(masked.text());
        List<Message> history = keyword.isPresent() ? List.of() : history(sessionId);
        Map<String, Object> userMeta = new LinkedHashMap<>();
        if (masked.masked() > 0) {
            userMeta.put("maskedSecrets", masked.masked());
        }
        long userMessageId = insert(sessionId, "USER", masked.text(), null, userMeta);
        if (session.title() == null) {
            jdbc.sql("UPDATE chat_session SET title = ? WHERE id = ? AND title IS NULL")
                    .params(shorten(masked.text().replaceAll("\\s+", " "), TITLE_CHARS), sessionId).update();
        }

        Answer answer = keyword.isPresent()
                ? keywordAnswer(user, masked.text(), keyword.get())
                : llmAnswer(user, sessionId, history, masked.text(), events, stream);
        long answerId = insert(sessionId, "ASSISTANT", answer.content(), null, answer.meta());
        for (Ref ref : answer.cited()) {
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
        MessageView userView = new MessageView(userMessageId, "USER", masked.text(), now, false, List.of(), List.of(),
                null, null, List.of());
        String errorKind = errorKind(answer.meta());
        MessageView answerView = new MessageView(answerId, "ASSISTANT", answer.content(), now, errorKind != null,
                answer.cited().stream().map(ChatService::citationView).toList(), answer.uses(), answer.mode(), errorKind,
                answer.followUps());
        return new TurnResult(session(user, sessionId), userView, answerView,
                masked.masked() > 0 ? SECRET_NOTICE : null);
    }

    /**
     * 키워드만 보낸 메시지: LLM 없이 수집 기사에서 바로 찾아 근거 번호와 함께 답한다 ({@link ChatTools#quickSearch}).
     * 칩 이름을 그대로 쳤으면(대소문자 무시) 그 칩의 검색어로 찾는다 ('DS' → 관리자가 정한 '삼성전자 DS').
     * 답 글에는 목록도 [n] 줄로 넣는다: 화면은 이 줄을 근거 목록으로 그리고, 다음 턴에 LLM이 지난 대화로 읽는다.
     */
    private Answer keywordAnswer(CurrentUser user, String typed, String keyword) {
        String flat = ChatShortcuts.flat(typed);
        Optional<FeaturedKeyword> featured = featuredKeywords.byLabel(flat);
        String query = featured.map(FeaturedKeyword::query).orElse(keyword);
        String shown = featured.map(FeaturedKeyword::label).orElse(keyword);
        Refs refs = new Refs();
        ChatTools.QuickResult found = tools.quickSearch(user, query, refs);
        List<ChatTools.QuickHit> words = found.hits().stream().filter(ChatTools.QuickHit::words).toList();
        List<ChatTools.QuickHit> meaning = found.hits().stream().filter(h -> !h.words()).toList();

        String subject = "‘" + shown + "’" + (query.equals(shown) ? "" : "(검색어 ‘" + query + "’)");
        StringBuilder out = new StringBuilder();
        if (words.isEmpty() && meaning.isEmpty()) {
            out.append(subject).append(" 관련 기사를 최근 ").append(found.days()).append("일 수집한 기사에서 찾지 못했습니다.\n");
            out.append(NAVER_TRY).append(query);
        } else {
            if (!words.isEmpty()) {
                out.append(subject).append(" 관련 최근 ").append(found.days()).append("일 기사 ").append(words.size())
                        .append("건 (").append(found.mode() == HybridSearch.Mode.HYBRID ? "관련도순" : "최신순")
                        .append(" · 수집한 기사에서 바로 찾음)\n");
                words.forEach(h -> out.append(keywordLine(h.ref())).append('\n'));
            } else {
                out.append(subject).append(" 낱말이 든 기사는 최근 ").append(found.days()).append("일 수집한 기사에 없습니다.\n");
            }
            if (!meaning.isEmpty()) {
                out.append("뜻이 가까운 기사 ").append(meaning.size()).append("건 (검색어 낱말은 없음)\n");
                meaning.forEach(h -> out.append(keywordLine(h.ref())).append('\n'));
            }
            out.append(NAVER_ALSO).append(query);
        }

        Map<String, Object> meta = new LinkedHashMap<>();
        meta.put("mode", MODE_KEYWORD);
        meta.put("query", query);
        meta.put("days", found.days());
        meta.put("count", found.hits().size());
        meta.put("search", found.mode().name());
        meta.put("llmCalls", 0);
        meta.put("link", "#/documents?q=" + URLEncoder.encode(query, StandardCharsets.UTF_8)
                .replace("+", "%20") + "&period=" + (found.days() <= 7 ? "7d" : "all"));
        meta.put("linkLabel", "문서 화면에서 더 보기");
        List<ToolUseView> uses = List.of(new ToolUseView(ChatTools.SEARCH_DOCUMENTS,
                ChatTools.label(ChatTools.SEARCH_DOCUMENTS), true, (String) meta.get("link"), (String) meta.get("linkLabel")));
        List<Ref> cited = found.hits().stream().map(ChatTools.QuickHit::ref).toList();
        List<FollowUp> followUps = ChatFollowUps.suggest(List.of(found.facts()), cited);
        putFollowUps(meta, followUps);
        return new Answer(out.toString(), cited, uses, meta, MODE_KEYWORD, followUps);
    }

    /** meta.followUps: [{text, url?}] (화면이 대화를 다시 열 때 그린다). */
    private static void putFollowUps(Map<String, Object> meta, List<FollowUp> followUps) {
        if (followUps.isEmpty()) {
            return;
        }
        List<Map<String, Object>> out = new ArrayList<>();
        for (FollowUp f : followUps) {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("text", f.text());
            if (f.url() != null) {
                m.put("url", f.url());
            }
            out.add(m);
        }
        meta.put("followUps", out);
    }

    private static List<FollowUp> followUps(Map<String, Object> meta) {
        if (!(meta.get("followUps") instanceof List<?> list)) {
            return List.of();
        }
        List<FollowUp> out = new ArrayList<>();
        for (Object o : list) {
            if (o instanceof Map<?, ?> m && m.get("text") instanceof String text && !text.isBlank()) {
                out.add(new FollowUp(text, m.get("url") instanceof String url ? url : null));
            }
        }
        return out;
    }

    /** "[1] 제목 — 언론사 · 2026-10-16 09:00" (화면은 이 줄을 원문 링크가 달린 근거 줄로 바꿔 그린다). */
    private static String keywordLine(Ref r) {
        StringBuilder line = new StringBuilder("[").append(r.no()).append("] ").append(r.title() == null ? "(제목 없음)" : r.title());
        List<String> meta = new ArrayList<>();
        if (r.sourceLabel() != null && !r.sourceLabel().isBlank()) {
            meta.add(r.sourceLabel());
        }
        if (r.publishedAt() != null) {
            meta.add(ChatTools.kst(r.publishedAt()));
        }
        if (!meta.isEmpty()) {
            line.append(" — ").append(String.join(" · ", meta));
        }
        return line.toString();
    }

    static final String STAGE_READING = "READING";
    static final String STAGE_TOOL = "TOOL";
    static final String STAGE_COMPOSING = "COMPOSING";

    /** 스트림 조각을 TurnEvents로 넘긴다. 조각을 보낸 뒤에만 reset을 보낸다. */
    private static final class Relay implements LlmClient.StreamListener {

        private final TurnEvents events;
        private boolean sent;

        Relay(TurnEvents events) {
            this.events = events;
        }

        @Override
        public void text(String delta) {
            if (delta != null && !delta.isEmpty()) {
                sent = true;
                events.delta(delta);
            }
        }

        @Override
        public void reset() {
            if (sent) {
                sent = false;
                events.reset();
            }
        }
    }

    /**
     * LLM 경로: (도구 호출 → 실행)을 최대 maxToolRounds번 한 뒤 답. 중간 단계(도구 호출·결과)는 바로 저장한다.
     * stream이면 LLM을 스트리밍으로 부르고 단계·글 조각을 events로 보낸다 (스트리밍이 안 되면 LlmClient가 한 번에 받기로 바꾼다).
     */
    private Answer llmAnswer(CurrentUser user, long sessionId, List<Message> history, String text, TurnEvents events,
                             boolean stream) {
        List<Message> convo = new ArrayList<>(history);
        convo.add(Message.user(text));

        String system = ChatPrompt.system(user, ZonedDateTime.now());
        List<LlmClient.Tool> definitions = tools.definitions();
        ChatTools.Context context = new ChatTools.Context(previousCitations(sessionId));
        Refs refs = new Refs();
        List<ToolUseView> uses = new ArrayList<>();
        List<ChatFollowUps.Facts> facts = new ArrayList<>();
        Relay relay = new Relay(events);
        int inputTokens = 0;
        int outputTokens = 0;
        int calls = 0;
        int rounds = 0;
        String answer;
        String error = null;
        if (stream) {
            events.stage(STAGE_READING, null, "질문을 살펴보는 중…");
        }
        try {
            while (true) {
                boolean allowTools = rounds < maxToolRounds;
                LlmClient.Response r = stream ? llm.chatStreaming(system, convo, definitions, allowTools, relay)
                        : llm.chat(system, convo, definitions, allowTools);
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
                relay.reset();   // 도구 호출과 함께 온 글은 답이 아니다
                rounds++;
                List<ToolCall> run = requested.size() > MAX_CALLS_PER_ROUND
                        ? requested.subList(0, MAX_CALLS_PER_ROUND) : requested;
                Map<String, Object> roundMeta = new LinkedHashMap<>();
                roundMeta.put("round", rounds);
                insert(sessionId, "ASSISTANT", r.text() == null ? "" : r.text(), toolCallsJson(run), roundMeta);
                convo.add(Message.assistant(r.text(), run));
                for (ToolCall call : run) {
                    if (stream) {
                        events.stage(STAGE_TOOL, call.name(), ChatTools.stageLabel(call));
                    }
                    Outcome outcome = tools.execute(user, call, refs, context);
                    if (outcome.facts() != null) {
                        facts.add(outcome.facts());
                    }
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
                if (stream) {
                    events.stage(STAGE_COMPOSING, null, "근거를 정리하는 중…");
                }
            }
        } catch (LlmException e) {
            log.warn("chat LLM call failed: {} (HTTP {}) {}", e.kind(), e.httpStatus(), e.detail());
            answer = e.getMessage();
            error = e.kind().name();
        }

        // 도구를 하나도 부르지 않은 후속 답만 앞 답의 번호를 이어 쓴다 (등록·상태 답에 붙은 엉뚱한 [1]이 옛 기사로 이어지지 않게)
        List<Ref> cited = error != null ? List.of()
                : rounds == 0 ? carriedCitations(context.previous(), answer) : citations(answer, refs);
        List<FollowUp> followUps = error != null ? List.of() : ChatFollowUps.suggest(facts, cited);
        Map<String, Object> meta = new LinkedHashMap<>();
        meta.put("model", llm.describe());
        meta.put("llmCalls", calls);
        meta.put("toolRounds", rounds);
        meta.put("usage", Map.of("inputTokens", inputTokens, "outputTokens", outputTokens));
        if (stream) {
            meta.put("streamed", true);
        }
        if (error != null) {
            meta.put("error", error);
        }
        putFollowUps(meta, followUps);
        return new Answer(answer, cited, uses, meta, null, followUps);
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
    private static List<Ref> carriedCitations(List<Ref> previous, String answer) {
        Set<Integer> used = refNumbers(answer);
        if (used.isEmpty() || previous.isEmpty()) {
            return List.of();
        }
        Map<Integer, Ref> byNo = new LinkedHashMap<>();
        previous.forEach(r -> byNo.put(r.no(), r));
        return used.stream().map(byNo::get).filter(Objects::nonNull).toList();
    }

    /**
     * 앞 답의 근거: 이 대화에서 근거가 있는 가장 최근 답의 chat_citation (번호 순). 키워드 바로 검색 답도 근거를 남기므로 포함된다.
     * 도구 없이 앞 번호를 이어 쓴 답("1번은 어디 기사야?" → [1]만)은 건너뛴다: 그 답이 가리킨 목록은 그 앞 답의 것이다.
     * 후속 답의 [n] 이어 쓰기와 "그중 ~만"(search_documents within_previous)이 쓴다.
     */
    List<Ref> previousCitations(long sessionId) {
        Long messageId = jdbc.sql("""
                SELECT c.message_id FROM chat_citation c JOIN chat_message m ON m.id = c.message_id
                WHERE m.session_id = ?
                  AND (m.meta->>'mode' IS NOT NULL OR COALESCE((m.meta->>'toolRounds')::int, 1) > 0)
                ORDER BY c.message_id DESC LIMIT 1
                """).param(sessionId).query(Long.class).optional().orElse(null);
        if (messageId == null) {
            return List.of();
        }
        return jdbc.sql("""
                SELECT ref_no, kind, document_id, issue_id, title_snapshot, url_snapshot, source_label, published_at
                FROM chat_citation WHERE message_id = ? ORDER BY ref_no
                """).param(messageId).query((rs, n) -> new Ref(rs.getInt("ref_no"), rs.getString("kind"),
                        rs.getObject("document_id", Long.class), rs.getObject("issue_id", Long.class),
                        rs.getString("title_snapshot"), rs.getString("url_snapshot"), rs.getString("source_label"),
                        rs.getObject("published_at", OffsetDateTime.class))).list();
    }

    /** 답 meta.error (오류 종류 이름, 없으면 null). 문자열이 아닌 값이 들어 있어도 종류를 모르는 오류로 본다. */
    private static String errorKind(Map<String, Object> meta) {
        Object error = meta == null ? null : meta.get("error");
        return error == null ? null : String.valueOf(error);
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
                    out.add(new MessageView(r.id(), "USER", r.content(), r.createdAt(), false, List.of(), List.of(), null,
                            null, List.of()));
                }
                case "TOOL" -> {
                    String name = String.valueOf(r.meta().get("name"));
                    pending.add(new ToolUseView(name, ChatTools.label(name), Boolean.TRUE.equals(r.meta().get("ok")),
                            (String) r.meta().get("link"), (String) r.meta().get("linkLabel")));
                }
                default -> {
                    if (r.finalAnswer()) {
                        String mode = r.meta().get("mode") instanceof String m ? m : null;
                        if (mode != null && r.meta().get("link") instanceof String link) {   // 키워드 바로 검색: 도구 행 없이 meta에
                            pending.add(new ToolUseView(ChatTools.SEARCH_DOCUMENTS, ChatTools.label(ChatTools.SEARCH_DOCUMENTS),
                                    true, link, (String) r.meta().get("linkLabel")));
                        }
                        String errorKind = errorKind(r.meta());
                        out.add(new MessageView(r.id(), "ASSISTANT", r.content(), r.createdAt(), errorKind != null,
                                citations.getOrDefault(r.id(), List.of()), List.copyOf(pending), mode, errorKind,
                                followUps(r.meta())));
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
