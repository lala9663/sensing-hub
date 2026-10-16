package io.sensinghub.watch;

import java.util.List;

import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.PutMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.ResponseStatus;
import org.springframework.web.bind.annotation.RestController;

import io.sensinghub.auth.CurrentUser;
import io.sensinghub.watch.WatchService.WatchView;
import jakarta.validation.Valid;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotEmpty;
import jakarta.validation.constraints.Size;

/**
 * 키워드 감시: 묶음(이름 + 키워드 + 제외어)을 등록하면 네이버 뉴스 검색 API로 주기 수집한다.
 * 소스 운영 조치(일시정지·재점검·제외 등)는 그대로 /sources/{id}/actions/{action}을 쓴다.
 */
@RestController
@RequestMapping("/api/v1/watches")
public class WatchController {

    private final WatchService service;

    public WatchController(WatchService service) {
        this.service = service;
    }

    public record CreateRequest(@NotBlank @Size(max = 200) String name,
                                @NotEmpty @Size(max = 50) List<@Size(max = 200) String> keywords,
                                @Size(max = 50) List<@Size(max = 200) String> exclude) {
    }

    public record UpdateRequest(@Size(max = 200) String name,
                                @Size(max = 50) List<@Size(max = 200) String> keywords,
                                @Size(max = 50) List<@Size(max = 200) String> exclude) {
    }

    /** REGISTRAR 이상은 바로 진행, VIEWER는 승인 대기. 진행 상태는 /registrations/mine에도 보인다. */
    @PostMapping
    @ResponseStatus(HttpStatus.ACCEPTED)
    public WatchView create(CurrentUser user, @Valid @RequestBody CreateRequest body) {
        return service.create(user, body.name(), body.keywords(), body.exclude());
    }

    /** 내 감시 (ADMIN은 전체). */
    @GetMapping
    public List<WatchView> list(CurrentUser user) {
        return service.list(user);
    }

    @GetMapping("/{id}")
    public WatchView get(CurrentUser user, @PathVariable long id) {
        return service.get(user, id);
    }

    /** 키워드·제외어·이름 수정 = 설정 새 버전 활성화. 생략한 필드는 그대로 둔다. */
    @PutMapping("/{id}")
    public WatchView update(CurrentUser user, @PathVariable long id, @Valid @RequestBody UpdateRequest body) {
        return service.update(user, id, body.name(), body.keywords(), body.exclude());
    }
}
