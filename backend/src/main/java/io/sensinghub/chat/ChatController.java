package io.sensinghub.chat;

import java.io.IOException;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.RejectedExecutionException;
import java.util.concurrent.SynchronousQueue;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.ResponseStatus;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.servlet.mvc.method.annotation.SseEmitter;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;

import io.sensinghub.auth.CurrentUser;
import io.sensinghub.chat.ChatService.MessageView;
import io.sensinghub.chat.ChatService.Pending;
import io.sensinghub.chat.ChatService.SessionDetail;
import io.sensinghub.chat.ChatService.SessionView;
import io.sensinghub.chat.ChatService.Status;
import io.sensinghub.chat.ChatService.TurnEvents;
import io.sensinghub.chat.ChatService.TurnResult;
import io.sensinghub.common.ApiException;
import jakarta.annotation.PreDestroy;
import jakarta.validation.Valid;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;

/**
 * 챗봇 (VIEWER 이상, 세션은 만든 사람만 본다). 메시지 보내기는 두 가지다.
 * <ul>
 *   <li>POST /sessions/{id}/messages: LLM·도구 호출이 끝날 때까지 기다렸다가 답을 한 번에 돌려준다 (보통 수 초, 도구 왕복이 많으면 수십 초).</li>
 *   <li>POST /sessions/{id}/messages/stream: 같은 턴을 SSE(text/event-stream)로 흘려보낸다. 이벤트는
 *       {@code stage}({stage, tool, label} — 질문을 살펴보는 중 → 도구별 단계 → 근거를 정리하는 중), {@code delta}({text} — 답 글 조각),
 *       {@code reset}(지금까지의 조각을 버린다), {@code done}(POST와 같은 TurnResult — 저장된 최종 답·근거·추천 칩), 처리하지 못한 서버 오류면
 *       {@code error}({error, message}). 검사 오류(빈 글·길이·네이버 형식 400, 남의 대화 404, 답하는 중 409)는 스트림을 열기 전에
 *       보통 JSON 오류로 온다. 키워드 바로 검색·한도 초과 안내는 단계·조각 없이 done만 온다. 화면이 연결을 끊어도 답은 끝까지 만들어 저장한다.</li>
 * </ul>
 * LLM 오류는 HTTP 오류가 아니라 error=true인 답으로 온다.
 */
@RestController
@RequestMapping("/api/v1/chat")
public class ChatController {

    /** 한 턴의 상한 (도구 왕복 + LLM 제한 시간 여러 번). 넘으면 스트림만 닫힌다 (답은 저장된다). */
    static final long STREAM_TIMEOUT_MS = 10 * 60 * 1000L;
    static final int MAX_STREAMS = 32;

    private static final Logger log = LoggerFactory.getLogger(ChatController.class);

    private final ChatService service;
    private final ObjectMapper json;
    private final ThreadPoolExecutor streams;

    public ChatController(ChatService service, ObjectMapper json) {
        this.service = service;
        this.json = json;
        AtomicInteger n = new AtomicInteger();
        this.streams = new ThreadPoolExecutor(0, MAX_STREAMS, 60, TimeUnit.SECONDS, new SynchronousQueue<>(), r -> {
            Thread t = new Thread(r, "chat-stream-" + n.incrementAndGet());
            t.setDaemon(true);
            return t;
        });
    }

    @PreDestroy
    void stop() {
        streams.shutdown();
    }

    /** LLM이 설정돼 있는지와 제공자·모델 (화면 안내용, 비밀 없음). */
    @GetMapping("/status")
    public Status status(CurrentUser user) {
        return service.status(user);
    }

    /** 내 대화 목록 (마지막 메시지 순, 최대 50개). */
    @GetMapping("/sessions")
    public List<SessionView> sessions(CurrentUser user) {
        return service.list(user);
    }

    public record CreateRequest(@Size(max = 200) String title) {
    }

    @PostMapping("/sessions")
    @ResponseStatus(HttpStatus.CREATED)
    public SessionView create(CurrentUser user, @Valid @RequestBody(required = false) CreateRequest body) {
        return service.create(user, body == null ? null : body.title());
    }

    /** 대화와 메시지 (사용자 글과 답, 답마다 근거·사용한 도구·추천 칩). */
    @GetMapping("/sessions/{id}")
    public SessionDetail detail(CurrentUser user, @PathVariable long id) {
        return service.detail(user, id);
    }

    @DeleteMapping("/sessions/{id}")
    @ResponseStatus(HttpStatus.NO_CONTENT)
    public void delete(CurrentUser user, @PathVariable long id) {
        service.delete(user, id);
    }

    @GetMapping("/sessions/{id}/messages")
    public List<MessageView> messages(CurrentUser user, @PathVariable long id) {
        return service.messageList(user, id);
    }

    public record SendRequest(@NotBlank @Size(max = ChatService.MAX_CONTENT) String content) {
    }

    /** 질문 하나 → 답 하나 (한 번에 받기). 같은 대화에서 앞 답을 기다리는 중이면 409. */
    @PostMapping("/sessions/{id}/messages")
    public TurnResult send(CurrentUser user, @PathVariable long id, @Valid @RequestBody SendRequest body) {
        return service.send(user, id, body.content());
    }

    /** 질문 하나 → 답을 SSE로 흘려보낸다 (클래스 설명의 이벤트). */
    @PostMapping("/sessions/{id}/messages/stream")
    public SseEmitter stream(CurrentUser user, @PathVariable long id, @Valid @RequestBody SendRequest body) {
        Pending pending = service.begin(user, id, body.content());
        SseEmitter emitter = new SseEmitter(STREAM_TIMEOUT_MS);
        SseEvents events = new SseEvents(emitter);
        emitter.onTimeout(events::close);
        emitter.onError(e -> events.close());
        try {
            streams.execute(() -> {
                try {
                    TurnResult result = service.run(pending, events, true);
                    events.send("done", result);
                } catch (RuntimeException e) {
                    log.error("chat stream turn failed", e);
                    Map<String, String> error = new LinkedHashMap<>();
                    error.put("error", e instanceof ApiException a ? a.code() : "INTERNAL");
                    error.put("message", e instanceof ApiException ? e.getMessage()
                            : "답을 만들다 서버 오류가 났습니다. 잠시 후 다시 시도해 주세요.");
                    events.send("error", error);
                } finally {
                    events.complete();
                }
            });
        } catch (RejectedExecutionException e) {
            service.cancel(pending);
            throw new ApiException(HttpStatus.SERVICE_UNAVAILABLE, "BUSY", "지금 답하는 대화가 너무 많습니다. 잠시 후 다시 보내 주세요.");
        }
        return emitter;
    }

    /**
     * SSE 보내기. 화면이 연결을 끊으면(보내기 실패·시간 초과) 더 보내지 않을 뿐 턴은 끝까지 돌아 저장된다.
     * 데이터는 화면 API와 같은 ObjectMapper로 만든 JSON 한 줄이다.
     */
    private final class SseEvents implements TurnEvents {

        private final SseEmitter emitter;
        private volatile boolean closed;

        SseEvents(SseEmitter emitter) {
            this.emitter = emitter;
        }

        @Override
        public void stage(String stage, String tool, String label) {
            Map<String, Object> data = new LinkedHashMap<>();
            data.put("stage", stage);
            if (tool != null) {
                data.put("tool", tool);
            }
            data.put("label", label);
            send("stage", data);
        }

        @Override
        public void delta(String text) {
            send("delta", Map.of("text", text));
        }

        @Override
        public void reset() {
            send("reset", Map.of());
        }

        void send(String name, Object data) {
            if (closed) {
                return;
            }
            try {
                emitter.send(SseEmitter.event().name(name).data(json.writeValueAsString(data)));
            } catch (JsonProcessingException e) {
                throw new IllegalStateException(e);
            } catch (IOException | IllegalStateException e) {
                closed = true;   // 화면이 떠났다
                log.debug("chat stream closed by client: {}", e.toString());
            }
        }

        void close() {
            closed = true;
        }

        void complete() {
            if (!closed) {
                closed = true;
                try {
                    emitter.complete();
                } catch (RuntimeException ignored) {
                    // 이미 닫혔다
                }
            }
        }
    }
}
