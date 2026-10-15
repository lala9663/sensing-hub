package io.sensinghub.source;

import java.util.List;
import java.util.Locale;
import java.util.Map;

import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import io.sensinghub.auth.CurrentUser;
import io.sensinghub.common.ApiException;
import io.sensinghub.source.SourceService.SourceDetail;
import io.sensinghub.source.SourceService.SourceSummary;

@RestController
@RequestMapping("/api/v1/sources")
public class SourceController {

    private final SourceService service;

    public SourceController(SourceService service) {
        this.service = service;
    }

    @GetMapping
    public List<SourceSummary> list(CurrentUser user, @RequestParam(required = false) String status) {
        return service.list(user, status);
    }

    @GetMapping("/status-counts")
    public Map<String, Long> statusCounts(CurrentUser user) {
        return service.statusCounts(user);
    }

    @GetMapping("/{id}")
    public SourceDetail detail(CurrentUser user, @PathVariable long id) {
        return service.detail(user, id);
    }

    /** pause | resume | run | recheck | rediscover | exclude */
    @PostMapping("/{id}/actions/{action}")
    public SourceSummary act(CurrentUser user, @PathVariable long id, @PathVariable String action) {
        SourceService.Action parsed;
        try {
            parsed = SourceService.Action.valueOf(action.toUpperCase(Locale.ROOT));
        } catch (IllegalArgumentException e) {
            throw ApiException.notFound("조치");
        }
        return service.act(user, id, parsed);
    }

    @PostMapping("/{id}/configs/{version}/activate")
    public SourceSummary activate(CurrentUser user, @PathVariable long id, @PathVariable int version) {
        return service.activateConfig(user, id, version);
    }
}
