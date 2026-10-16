package io.sensinghub.review;

import java.util.List;
import java.util.Map;

import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import io.sensinghub.auth.CurrentUser;
import io.sensinghub.review.ReviewQueueService.ReviewItem;

@RestController
@RequestMapping("/api/v1/review-queue")
public class ReviewQueueController {

    private final ReviewQueueService service;

    public ReviewQueueController(ReviewQueueService service) {
        this.service = service;
    }

    /** kind: APPROVAL | API_KEY | REVIEW | BROKEN | BLOCKED (없으면 전체) */
    @GetMapping
    public List<ReviewItem> list(CurrentUser user, @RequestParam(required = false) String kind) {
        return service.list(user, kind);
    }

    @GetMapping("/counts")
    public Map<String, Long> counts(CurrentUser user) {
        return service.counts(user);
    }
}
