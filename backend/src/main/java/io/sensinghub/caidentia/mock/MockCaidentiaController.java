package io.sensinghub.caidentia.mock;

import java.time.OffsetDateTime;
import java.util.List;
import java.util.Map;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.http.HttpStatus;
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

import io.sensinghub.caidentia.mock.MockCaidentiaStore.MockSupplier;
import io.sensinghub.common.ApiException;
import io.sensinghub.document.DocumentService;

/**
 * 가짜 케이던시아 API. 우리 연동 코드({@code io.sensinghub.caidentia})는 이것을 외부 시스템처럼 HTTP로 부른다.
 * <pre>
 * GET  /mock/caidentia/api/suppliers?updatedSince=   공급사 마스터 [{supplierId, name, aliases[], bizRegNo, dartCorpCode, tier, category, status, updatedAt}]
 * PUT  /mock/caidentia/api/suppliers/{id}            공급사 한 건 바꾸기 (데모: status INACTIVE 등). 준 필드만 바뀐다
 * POST /mock/caidentia/api/risk-events               우리가 보낸 리스크 이벤트를 저장
 * GET  /mock/caidentia/api/risk-events               받은 이벤트 (데모 화면 "케이던시아가 받은 이벤트")
 * </pre>
 * 인증 흉내: 헤더 {@code X-Caidentia-Key}가 {@code sensing.caidentia.mock.api-key}(기본 = {@code sensing.caidentia.api-key})와 같아야 하고 아니면 401.
 * 이 컨트롤러는 우리 사용자 권한(X-User)과 무관하다.
 */
@RestController
@RequestMapping("/mock/caidentia/api")
@ConditionalOnProperty(name = "sensing.caidentia.mock.enabled", havingValue = "true", matchIfMissing = true)
public class MockCaidentiaController {

    public static final String KEY_HEADER = "X-Caidentia-Key";

    private final MockCaidentiaStore store;
    private final String apiKey;

    /** 가짜 서버가 받아 주는 키. 기본은 우리 쪽 설정과 같은 값이고, 테스트에서 틀린 키를 흉내 낼 때만 따로 준다. */
    public MockCaidentiaController(MockCaidentiaStore store,
                                   @Value("${sensing.caidentia.mock.api-key:${sensing.caidentia.api-key:mock-key}}") String apiKey) {
        this.store = store;
        this.apiKey = apiKey;
    }

    public record SupplierPatch(String name, List<String> aliases, String bizRegNo, String dartCorpCode, String tier,
                                String category, String status) {
    }

    @GetMapping("/suppliers")
    public List<MockSupplier> suppliers(@RequestHeader(value = KEY_HEADER, required = false) String key,
                                        @RequestParam(required = false) String updatedSince) {
        authorize(key);
        OffsetDateTime since = updatedSince == null || updatedSince.isBlank() ? null : DocumentService.parseSince(updatedSince);
        return store.suppliers(since);
    }

    @PutMapping("/suppliers/{id}")
    public MockSupplier update(@RequestHeader(value = KEY_HEADER, required = false) String key, @PathVariable String id,
                               @RequestBody SupplierPatch body) {
        authorize(key);
        if (body.status() != null && !body.status().equals("ACTIVE") && !body.status().equals("INACTIVE")) {
            throw new ApiException(HttpStatus.BAD_REQUEST, "BAD_REQUEST", "status는 ACTIVE 또는 INACTIVE입니다.");
        }
        return store.update(id, body.name(), body.aliases(), body.bizRegNo(), body.dartCorpCode(), body.tier(), body.category(),
                        body.status())
                .orElseThrow(() -> ApiException.notFound("공급사 " + id));
    }

    @PostMapping("/risk-events")
    @ResponseStatus(HttpStatus.CREATED)
    public Map<String, Object> receive(@RequestHeader(value = KEY_HEADER, required = false) String key,
                                       @RequestBody Map<String, Object> event) {
        authorize(key);
        return store.receiveRiskEvent(event);
    }

    @GetMapping("/risk-events")
    public List<Map<String, Object>> received(@RequestHeader(value = KEY_HEADER, required = false) String key) {
        authorize(key);
        return store.riskEvents();
    }

    private void authorize(String key) {
        if (key == null || !key.equals(apiKey)) {
            throw new ApiException(HttpStatus.UNAUTHORIZED, "UNAUTHORIZED", "케이던시아 API 키가 틀립니다 (X-Caidentia-Key).");
        }
    }
}
