package io.sensinghub.caidentia.mock;

import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;

import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.stereotype.Component;

/**
 * 가짜 케이던시아의 데이터 (메모리). 실제 케이던시아 API가 아직 없어 연동 형태를 먼저 만들기 위한 것이며, 재시작하면 씨앗 데이터로 돌아간다.
 * 공급사 이름은 모두 가공의 것이다. {@code sensing.caidentia.mock.enabled=false}로 끄면 이 저장소와 컨트롤러가 뜨지 않는다.
 */
@Component
@ConditionalOnProperty(name = "sensing.caidentia.mock.enabled", havingValue = "true", matchIfMissing = true)
public class MockCaidentiaStore {

    /** 케이던시아 쪽 공급사 한 건 (케이던시아 필드명 그대로: supplierId, bizRegNo, dartCorpCode, tier …). */
    public record MockSupplier(String supplierId, String name, List<String> aliases, String bizRegNo, String dartCorpCode,
                               String tier, String category, String status, OffsetDateTime updatedAt) {
    }

    private static final OffsetDateTime SEED_AT = OffsetDateTime.of(2026, 10, 1, 9, 0, 0, 0, ZoneOffset.ofHours(9));

    private final Map<String, MockSupplier> suppliers = new LinkedHashMap<>();
    private final List<Map<String, Object>> riskEvents = new ArrayList<>();

    public MockCaidentiaStore() {
        reset();
    }

    /** 씨앗 상태로 되돌린다 (받은 이벤트도 비운다). 테스트·데모용. */
    public synchronized void reset() {
        suppliers.clear();
        riskEvents.clear();
        seed("SUP-0001", "나부품", List.of("NABU", "나부품(주)"), "123-45-67890", "00123456", "1차", "전장부품");
        seed("SUP-0002", "가전자", List.of("GA Electronics"), "101-81-12345", "00234567", "1차", "반도체");
        seed("SUP-0003", "다정밀", List.of("다정밀공업"), "201-81-00001", null, "2차", "정밀가공");
        seed("SUP-0004", "라물류", List.of("RA Logis"), "301-81-00002", null, "1차", "물류");
        seed("SUP-0005", "마소재", List.of(), "401-81-00003", "00345678", "2차", "소재");
        seed("SUP-0006", "바금속", List.of("바메탈"), "501-81-00004", null, "2차", "금속");
        seed("SUP-0007", "사화학", List.of("SA Chem"), "601-81-00005", "00456789", "1차", "화학");
        seed("SUP-0008", "아전기", List.of(), "701-81-00006", null, "2차", "전기");
        seed("SUP-0009", "자포장", List.of("자패키징"), "801-81-00007", null, "2차", "포장");
    }

    private void seed(String id, String name, List<String> aliases, String bizRegNo, String corpCode, String tier, String category) {
        suppliers.put(id, new MockSupplier(id, name, aliases, bizRegNo, corpCode, tier, category, "ACTIVE", SEED_AT));
    }

    /** updatedSince가 있으면 그 뒤에 바뀐 것만. */
    public synchronized List<MockSupplier> suppliers(OffsetDateTime updatedSince) {
        List<MockSupplier> out = new ArrayList<>();
        for (MockSupplier s : suppliers.values()) {
            if (updatedSince == null || s.updatedAt().isAfter(updatedSince)) {
                out.add(s);
            }
        }
        return out;
    }

    public synchronized Optional<MockSupplier> supplier(String id) {
        return Optional.ofNullable(suppliers.get(id));
    }

    /** null이 아닌 필드만 바꾸고 updatedAt을 지금으로. 없는 id면 empty. */
    public synchronized Optional<MockSupplier> update(String id, String name, List<String> aliases, String bizRegNo,
                                                      String dartCorpCode, String tier, String category, String status) {
        MockSupplier cur = suppliers.get(id);
        if (cur == null) {
            return Optional.empty();
        }
        MockSupplier next = new MockSupplier(id,
                name == null ? cur.name() : name,
                aliases == null ? cur.aliases() : List.copyOf(aliases),
                bizRegNo == null ? cur.bizRegNo() : bizRegNo,
                dartCorpCode == null ? cur.dartCorpCode() : dartCorpCode,
                tier == null ? cur.tier() : tier,
                category == null ? cur.category() : category,
                status == null ? cur.status() : status,
                OffsetDateTime.now());
        suppliers.put(id, next);
        return Optional.of(next);
    }

    /** 우리가 보낸 리스크 이벤트를 받은 순서대로 둔다 (receivedAt·eventId를 붙인다). */
    public synchronized Map<String, Object> receiveRiskEvent(Map<String, Object> event) {
        Map<String, Object> stored = new LinkedHashMap<>();
        stored.put("eventId", "EVT-" + (riskEvents.size() + 1));
        stored.put("receivedAt", OffsetDateTime.now());
        stored.putAll(event == null ? Map.of() : event);
        riskEvents.add(stored);
        return stored;
    }

    public synchronized List<Map<String, Object>> riskEvents() {
        return List.copyOf(riskEvents);
    }
}
