package io.sensinghub.backfill;

import java.time.LocalDate;
import java.time.format.DateTimeParseException;
import java.util.List;

import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.ResponseStatus;
import org.springframework.web.bind.annotation.RestController;

import io.sensinghub.auth.CurrentUser;
import io.sensinghub.backfill.BackfillService.BackfillView;
import io.sensinghub.common.ApiException;

/**
 * 과거 기사 채우기: 관리자·등록 담당자가 소스와 기간(기본 최근 3개월)을 정해 요청한다. 기간 묶음(기본 2개월) 단위로 최근 것부터 채우고,
 * "두 달 더"로 뒤로 늘린다. 길이 상한(기본 1년)은 등록 담당자에게만 적용한다.
 */
@RestController
@RequestMapping("/api/v1/sources/{sourceId}/backfills")
public class BackfillController {

    private final BackfillService service;

    public BackfillController(BackfillService service) {
        this.service = service;
    }

    /** from·to: "2026-07-19" 형식 (비우면 최근 3개월). */
    public record CreateRequest(String from, String to) {
    }

    @GetMapping
    public List<BackfillView> list(CurrentUser user, @PathVariable long sourceId) {
        return service.list(user, sourceId);
    }

    @PostMapping
    @ResponseStatus(HttpStatus.ACCEPTED)
    public BackfillView create(CurrentUser user, @PathVariable long sourceId, @RequestBody(required = false) CreateRequest body) {
        return service.request(user, sourceId, date(body == null ? null : body.from(), "from"),
                date(body == null ? null : body.to(), "to"));
    }

    /** segments: 가장 오래된 묶음 앞에 더할 묶음 수 (기본 1 = "두 달 더"). */
    public record ExtendRequest(Integer segments) {
    }

    /** 기간을 뒤로 늘린다. 끝났거나 취소한 채우기는 다시 열어 더한 묶음을 채운다. */
    @PostMapping("/{backfillId}/extend")
    public BackfillView extend(CurrentUser user, @PathVariable long sourceId, @PathVariable long backfillId,
                               @RequestBody(required = false) ExtendRequest body) {
        int segments = body == null || body.segments() == null ? 1 : body.segments();
        return service.extend(user, sourceId, backfillId, segments);
    }

    @PostMapping("/{backfillId}/cancel")
    public BackfillView cancel(CurrentUser user, @PathVariable long sourceId, @PathVariable long backfillId) {
        return service.cancel(user, sourceId, backfillId);
    }

    private static LocalDate date(String value, String field) {
        if (value == null || value.isBlank()) {
            return null;
        }
        try {
            return LocalDate.parse(value.trim());
        } catch (DateTimeParseException e) {
            throw new ApiException(HttpStatus.BAD_REQUEST, "BAD_REQUEST", field + "는 2026-07-19 형식의 날짜여야 합니다.");
        }
    }
}
