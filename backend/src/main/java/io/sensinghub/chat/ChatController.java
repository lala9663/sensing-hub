package io.sensinghub.chat;

import java.util.List;

import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.ResponseStatus;
import org.springframework.web.bind.annotation.RestController;

import io.sensinghub.auth.CurrentUser;
import io.sensinghub.chat.ChatService.MessageView;
import io.sensinghub.chat.ChatService.SessionDetail;
import io.sensinghub.chat.ChatService.SessionView;
import io.sensinghub.chat.ChatService.Status;
import io.sensinghub.chat.ChatService.TurnResult;
import jakarta.validation.Valid;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;

/**
 * 챗봇 (VIEWER 이상, 세션은 만든 사람만 본다). 메시지 보내기는 LLM·도구 호출이 끝날 때까지 기다렸다가 답을 돌려준다
 * (보통 수 초, 도구 왕복이 많으면 수십 초). LLM 오류는 HTTP 오류가 아니라 error=true인 답으로 온다.
 */
@RestController
@RequestMapping("/api/v1/chat")
public class ChatController {

    private final ChatService service;

    public ChatController(ChatService service) {
        this.service = service;
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

    /** 대화와 메시지 (사용자 글과 답, 답마다 근거·사용한 도구). */
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

    /** 질문 하나 → 답 하나. 같은 대화에서 앞 답을 기다리는 중이면 409. */
    @PostMapping("/sessions/{id}/messages")
    public TurnResult send(CurrentUser user, @PathVariable long id, @Valid @RequestBody SendRequest body) {
        return service.send(user, id, body.content());
    }
}
