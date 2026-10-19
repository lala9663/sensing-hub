package io.sensinghub.issue;

import java.util.List;

import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import io.sensinghub.auth.CurrentUser;
import io.sensinghub.issue.IssueService.IssueDetail;
import io.sensinghub.issue.IssueService.IssueRow;

/** 이슈(같은 사건을 다룬 기사 묶음) 조회 (VIEWER 이상). 워커가 색인할 때 만든다. */
@RestController
@RequestMapping("/api/v1/issues")
public class IssueController {

    private final IssueService service;

    public IssueController(IssueService service) {
        this.service = service;
    }

    /**
     * 언론사 수 → 기사 수 → 마지막 기사 시각 내림차순.
     * since: 마지막 기사 시각 기준 (ISO 시각 또는 날짜), min_docs: 기사 수 하한 (기본 2), min_sources: 언론사 수 하한 (기본 1),
     * q: 대표 제목·서명 낱말 검색, limit: 기본 50, 최대 200.
     */
    @GetMapping
    public List<IssueRow> list(CurrentUser user,
                               @RequestParam(required = false) String since,
                               @RequestParam(name = "min_docs", required = false) Integer minDocs,
                               @RequestParam(name = "min_sources", required = false) Integer minSources,
                               @RequestParam(required = false) String q,
                               @RequestParam(required = false) Integer limit) {
        return service.list(user, since, minDocs, minSources, q, limit);
    }

    /** 이슈 + 소속 문서 (발행일 오름차순). */
    @GetMapping("/{id}")
    public IssueDetail detail(CurrentUser user, @PathVariable long id) {
        return service.detail(user, id);
    }
}
