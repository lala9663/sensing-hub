package io.sensinghub.company;

import java.time.OffsetDateTime;
import java.util.List;

import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.PutMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.ResponseStatus;
import org.springframework.web.bind.annotation.RestController;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;

import io.sensinghub.auth.CurrentUser;
import io.sensinghub.company.CompanyService.CompanyDetail;
import io.sensinghub.company.CompanyService.CompanyInput;
import io.sensinghub.company.CompanyService.CompanyView;
import io.sensinghub.company.CompanyService.ImportResult;
import io.sensinghub.company.CompanyService.SignalView;
import io.sensinghub.document.DocumentService;
import jakarta.validation.Valid;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;

/**
 * 회사(거래처) 마스터와 회사 신호 (M3).
 * <pre>
 * POST   /api/v1/companies            {name, aliases[], bizNo?, corpCode?, externalId?, tags[]}   REGISTRAR 이상
 * POST   /api/v1/companies/import     JSON 배열 또는 CSV 텍스트(name,aliases(;구분),biz_no,corp_code,external_id,tags) — 사업자번호 같으면 갱신
 * GET    /api/v1/companies            목록 (신호 수·최근 신호·연결 문서 수). ?includeArchived=true
 * GET    /api/v1/companies/{id}       상세 (최근 신호 20건, 최근 문서 20건)
 * PUT    /api/v1/companies/{id}       생략한 필드는 그대로. 이름·별칭이 바뀌면 감시 키워드도 새 버전으로
 * DELETE /api/v1/companies/{id}       ARCHIVED 처리 (감시는 EXCLUDED)
 * GET    /api/v1/companies/{id}/signals?since=     회사 하나의 신호
 * GET    /api/v1/company-signals?since=&kind=&severity=   전체 신호 (심각도 → 시각 내림차순)
 * </pre>
 */
@RestController
@RequestMapping("/api/v1")
public class CompanyController {

    private final CompanyService service;
    private final ObjectMapper json;

    public CompanyController(CompanyService service, ObjectMapper json) {
        this.service = service;
        this.json = json;
    }

    public record CreateRequest(@NotBlank @Size(max = 200) String name,
                                @Size(max = 49) List<@Size(max = 200) String> aliases,
                                @Size(max = 20) String bizNo,
                                @Size(max = 20) String corpCode,
                                @Size(max = 100) String externalId,
                                @Size(max = 20) List<@Size(max = 50) String> tags) {
        CompanyInput toInput() {
            return new CompanyInput(name, aliases, bizNo, corpCode, externalId, tags);
        }
    }

    public record UpdateRequest(@Size(max = 200) String name,
                                @Size(max = 49) List<@Size(max = 200) String> aliases,
                                @Size(max = 20) String bizNo,
                                @Size(max = 20) String corpCode,
                                @Size(max = 100) String externalId,
                                @Size(max = 20) List<@Size(max = 50) String> tags) {
        CompanyInput toInput() {
            return new CompanyInput(name, aliases, bizNo, corpCode, externalId, tags);
        }
    }

    @PostMapping("/companies")
    @ResponseStatus(HttpStatus.CREATED)
    public CompanyView create(CurrentUser user, @Valid @RequestBody CreateRequest body) {
        return service.create(user, body.toInput());
    }

    /** JSON 배열(Content-Type application/json) 또는 CSV 텍스트(text/csv, text/plain). 한 건이라도 형식이 틀리면 전체 400. */
    @PostMapping(value = "/companies/import", consumes = MediaType.ALL_VALUE)
    public ImportResult importAll(CurrentUser user, @RequestBody String body,
                                  @RequestHeader(value = "Content-Type", required = false) String contentType) {
        List<CompanyInput> inputs;
        String trimmed = body == null ? "" : body.strip();
        boolean isJson = (contentType != null && contentType.toLowerCase().contains("json")) || trimmed.startsWith("[");
        if (isJson) {
            try {
                inputs = json.readValue(trimmed, new TypeReference<List<CreateRequest>>() { }).stream()
                        .map(CreateRequest::toInput).toList();
            } catch (java.io.IOException e) {
                String m = e.getMessage() == null ? "" : e.getMessage();
                throw CompanyService.bad("JSON 배열을 읽을 수 없습니다: " + (m.length() > 200 ? m.substring(0, 200) : m));
            }
        } else {
            inputs = CompanyService.parseCsv(trimmed);
        }
        return service.importAll(user, inputs);
    }

    @GetMapping("/companies")
    public List<CompanyView> list(CurrentUser user,
                                  @RequestParam(required = false, defaultValue = "false") boolean includeArchived) {
        return service.list(user, includeArchived);
    }

    @GetMapping("/companies/{id}")
    public CompanyDetail get(CurrentUser user, @PathVariable long id) {
        return service.detail(user, id);
    }

    @PutMapping("/companies/{id}")
    public CompanyView update(CurrentUser user, @PathVariable long id, @Valid @RequestBody UpdateRequest body) {
        return service.update(user, id, body.toInput());
    }

    @DeleteMapping("/companies/{id}")
    @ResponseStatus(HttpStatus.NO_CONTENT)
    public void archive(CurrentUser user, @PathVariable long id) {
        service.archive(user, id);
    }

    /** since는 ISO 시각·날짜 (observed_at 기준, 초과). */
    @GetMapping("/companies/{id}/signals")
    public List<SignalView> signals(CurrentUser user, @PathVariable long id,
                                    @RequestParam(required = false) String since,
                                    @RequestParam(required = false, defaultValue = "100") int limit) {
        return service.signals(user, id, parseSince(since), limit);
    }

    @GetMapping("/company-signals")
    public List<SignalView> allSignals(CurrentUser user,
                                       @RequestParam(required = false) String since,
                                       @RequestParam(required = false) String kind,
                                       @RequestParam(required = false) String severity,
                                       @RequestParam(required = false, defaultValue = "100") int limit) {
        return service.allSignals(user, parseSince(since), kind, severity, limit);
    }

    private static OffsetDateTime parseSince(String since) {
        return DocumentService.parseSince(since);
    }
}
