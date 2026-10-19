package io.sensinghub.naver;

import org.springframework.http.CacheControl;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import io.sensinghub.auth.CurrentUser;
import io.sensinghub.naver.NaverNewsService.LiveResult;
import io.sensinghub.naver.NaverNewsService.Status;
import io.sensinghub.naver.NaverNewsService.VerifyResult;

/**
 * 네이버 뉴스 검색 (표시 전용 기본): 사용 방식 조회, 감시별 실시간 보기, 관리자 키 시험 호출.
 * 실시간 보기 응답은 브라우저·프록시도 저장하지 않게 {@code Cache-Control: no-store}다 (약관: 캐싱 금지).
 */
@RestController
public class NaverNewsController {

    private final NaverNewsService service;

    public NaverNewsController(NaverNewsService service) {
        this.service = service;
    }

    @GetMapping("/api/v1/naver/status")
    public Status status(CurrentUser user) {
        return service.status(user);
    }

    @GetMapping("/api/v1/watches/{id}/naver-news")
    public ResponseEntity<LiveResult> live(CurrentUser user, @PathVariable long id,
                                           @RequestParam(required = false) String keyword,
                                           @RequestParam(required = false) Integer display,
                                           @RequestParam(required = false) Integer start) {
        return ResponseEntity.ok().cacheControl(CacheControl.noStore()).body(service.live(user, id, keyword, display, start));
    }

    @PostMapping("/api/v1/api-credentials/naver_news/verify")
    public VerifyResult verify(CurrentUser user) {
        return service.verify(user);
    }
}
