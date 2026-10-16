package io.sensinghub.document;

import java.util.List;
import java.util.Map;

import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import io.sensinghub.auth.CurrentUser;
import io.sensinghub.document.DocumentService.DocumentDetail;
import io.sensinghub.document.DocumentService.DocumentRow;

/** 수집 문서 조회 (VIEWER 이상). 요약·분류는 워커의 보강 단계가 붙이며, 언론 기사는 제목·링크·분류만 있다. */
@RestController
@RequestMapping("/api/v1/documents")
public class DocumentController {

    private final DocumentService service;

    public DocumentController(DocumentService service) {
        this.service = service;
    }

    /**
     * 발행일 내림차순 목록.
     * since: 수집 시각 기준 (ISO 시각 또는 날짜), source_id, topic: 분류 체계 값 포함 여부, keyword: 감시 검색어 일치,
     * limit: 기본 50, 최대 200.
     */
    @GetMapping
    public List<DocumentRow> list(CurrentUser user,
                                  @RequestParam(required = false) String since,
                                  @RequestParam(name = "source_id", required = false) Long sourceId,
                                  @RequestParam(required = false) String topic,
                                  @RequestParam(required = false) String keyword,
                                  @RequestParam(required = false) Integer limit) {
        return service.list(user, since, sourceId, topic, keyword, limit);
    }

    /** 토픽별 건수 (since는 수집 시각 기준). */
    @GetMapping("/topics")
    public Map<String, Long> topics(CurrentUser user, @RequestParam(required = false) String since) {
        return service.topicCounts(user, since);
    }

    /** 목록 필드 + bodyText(있으면) + attachments. */
    @GetMapping("/{id}")
    public DocumentDetail detail(CurrentUser user, @PathVariable long id) {
        return service.detail(user, id);
    }
}
