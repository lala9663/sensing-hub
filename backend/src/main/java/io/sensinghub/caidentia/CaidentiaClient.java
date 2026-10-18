package io.sensinghub.caidentia;

import java.time.OffsetDateTime;
import java.util.List;
import java.util.Map;

/**
 * 케이던시아(삼성SDS 구매 SaaS)와 주고받는 것. 지금은 우리 안의 가짜 케이던시아를 보지만, 연동 코드는 외부 시스템처럼 쓴다.
 * 실제 API가 나오면 {@link RestCaidentiaClient}의 주소·필드 매핑만 바꾸고 {@link CaidentiaSyncService}·{@link CaidentiaPushService}는 그대로 둔다.
 */
public interface CaidentiaClient {

    /** 케이던시아의 공급사 한 건 (케이던시아 필드명). */
    record Supplier(String supplierId, String name, List<String> aliases, String bizRegNo, String dartCorpCode, String tier,
                    String category, String status, OffsetDateTime updatedAt) {
    }

    /** 케이던시아로 보내는 리스크 이벤트 한 건 (회사 신호 하나). */
    record RiskEvent(String supplierId, String companyName, String kind, String severity, String title,
                     OffsetDateTime observedAt, Map<String, Object> detail) {
    }

    /** 부르지 못했거나 2xx가 아니었다. 메시지에 비밀은 없다. */
    class CaidentiaException extends RuntimeException {
        public CaidentiaException(String message) {
            super(message);
        }
    }

    /** 공급사 마스터. updatedSince가 null이면 전체. */
    List<Supplier> fetchSuppliers(OffsetDateTime updatedSince);

    void pushRiskEvent(RiskEvent event);
}
