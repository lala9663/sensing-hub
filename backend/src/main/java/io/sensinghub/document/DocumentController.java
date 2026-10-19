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
import io.sensinghub.document.DocumentService.Filter;
import jakarta.servlet.http.HttpServletRequest;

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
     * since: 수집 시각 기준 (ISO 시각 또는 날짜), source_id, topic: 분류 체계 값 포함 여부,
     * watch_id: 그 키워드 감시에 걸린 문서, keyword: 감시 검색어 일치 (watch_id와 함께면 그 감시에서 그 검색어로 걸린 문서),
     * section: 기사 종류 정확 일치 (여러 번 주면 그중 하나 — ?section=연설문&amp;section=보도자료. 값 안의 쉼표는 나누지 않는다),
     * site: 도메인 또는 사이트 이름 (DocumentService.matchSites),
     * q: 전문 검색 (낱말마다 접두 일치, 모든 낱말 포함, 조사는 뗀다 — DocumentService.appendSearch), limit: 기본 50, 최대 200.
     * 행에 issueId·issueDocCount(워커가 묶은 이슈)가 붙는다.
     */
    @GetMapping
    public List<DocumentRow> list(CurrentUser user, HttpServletRequest request,
                                  @RequestParam(required = false) String since,
                                  @RequestParam(name = "source_id", required = false) Long sourceId,
                                  @RequestParam(required = false) String topic,
                                  @RequestParam(name = "watch_id", required = false) Long watchId,
                                  @RequestParam(required = false) String keyword,
                                  @RequestParam(required = false) String site,
                                  @RequestParam(required = false) String q,
                                  @RequestParam(required = false) Integer limit) {
        return service.list(user, new Filter(since, sourceId, topic, watchId, keyword, sections(request), site), q, limit);
    }

    /** 토픽별 건수 (since는 수집 시각 기준). */
    @GetMapping("/topics")
    public Map<String, Long> topics(CurrentUser user, @RequestParam(required = false) String since) {
        return service.topicCounts(user, since);
    }

    /** 기사 종류(section)별 건수 (많은 순). 조건은 목록과 같다 (since·source_id·site·topic). */
    @GetMapping("/sections")
    public Map<String, Long> sections(CurrentUser user,
                                      @RequestParam(required = false) String since,
                                      @RequestParam(name = "source_id", required = false) Long sourceId,
                                      @RequestParam(required = false) String topic,
                                      @RequestParam(required = false) String site) {
        return service.sectionCounts(user, new Filter(since, sourceId, topic, null, null, List.of(), site));
    }

    /** @RequestParam List는 값 하나를 쉼표로 나누므로('민족,국제' 같은 종류 이름이 있다) 요청 값을 그대로 읽는다. */
    private static List<String> sections(HttpServletRequest request) {
        String[] values = request.getParameterValues("section");
        return values == null ? List.of() : List.of(values);
    }

    /** 목록 필드 + bodyText(있으면) + attachments. */
    @GetMapping("/{id}")
    public DocumentDetail detail(CurrentUser user, @PathVariable long id) {
        return service.detail(user, id);
    }
}
