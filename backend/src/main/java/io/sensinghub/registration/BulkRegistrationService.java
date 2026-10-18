package io.sensinghub.registration;

import java.util.ArrayList;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.UUID;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpStatus;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Service;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.TransactionDefinition;
import org.springframework.transaction.support.TransactionTemplate;

import io.sensinghub.audit.AuditLogger;
import io.sensinghub.auth.CurrentUser;
import io.sensinghub.auth.Role;
import io.sensinghub.common.ApiException;
import io.sensinghub.common.UrlNormalizer;
import io.sensinghub.registration.RegistrationService.Channel;
import io.sensinghub.registration.RegistrationService.Registration;

/**
 * 사이트 일괄 등록 (M3). 현업의 핵심 요구는 "아주 많은 곳에서 기사·이슈를 긁어 와 모으는 것"이라, 수십~수백 곳의 주소를
 * 한 번에 넣고(JSON 배열 또는 한 줄에 하나) 몇 분 뒤 묶음(batch) 단위로 "몇 곳이 자동 등록됐고 몇 곳이 관리자 큐에 갔는지"를 본다.
 *
 * <ul>
 *   <li>URL마다 {@link RegistrationService#register}와 같은 규칙(정규화·중복·ONBOARD 작업)을 적용한다. 한 건은 한 트랜잭션이라
 *       형식 오류나 DB 오류가 다른 건을 막지 않는다.</li>
 *   <li>같은 요청 안에 같은 도메인이 여러 번 있으면 첫 건만 등록하고 나머지는 DUPLICATE로 남긴다.</li>
 *   <li>주소 형식이 틀린 줄도 INVALID로 같은 묶음에 남겨 결과 화면에서 함께 본다 (소스·작업은 만들지 않는다).</li>
 * </ul>
 * REGISTRAR 이상만 쓴다 (VIEWER의 승인 대기 흐름은 한 건 등록 그대로).
 */
@Service
public class BulkRegistrationService {

    private static final Logger log = LoggerFactory.getLogger(BulkRegistrationService.class);
    public static final int MAX_URLS = 500;

    private final RegistrationService registrations;
    private final JdbcClient jdbc;
    private final AuditLogger audit;
    private final TransactionTemplate eachInItsOwnTransaction;

    public BulkRegistrationService(RegistrationService registrations, JdbcClient jdbc, AuditLogger audit,
                                   PlatformTransactionManager tx) {
        this.registrations = registrations;
        this.jdbc = jdbc;
        this.audit = audit;
        this.eachInItsOwnTransaction = new TransactionTemplate(tx);
        this.eachInItsOwnTransaction.setPropagationBehavior(TransactionDefinition.PROPAGATION_REQUIRES_NEW);
    }

    /** status: PROCESSING(접수, 워커가 진행) | DUPLICATE | INVALID. registrationId·domain은 INVALID면 null일 수 있다. */
    public record Item(String url, Long registrationId, String domain, String status, String message) {
    }

    public record BulkResult(String batchId, int total, int accepted, int duplicate, int invalid, List<Item> items) {
    }

    /**
     * @param byStatus  등록 요청 상태별 건수 (PROCESSING·COMPLETED·REVIEW·REJECTED·DUPLICATE·INVALID …)
     * @param byStep    진행 단계별 건수 (QUEUED·PROBE·DISCOVERY·FIRST_CRAWL·DONE·REVIEW·REJECTED; 단계가 없는 건은 NONE)
     * @param completed 자동 등록이 끝나 수집 중인 건수, review 관리자 큐로 간 건수, rejected 차단·반려된 건수
     */
    public record BatchSummary(String batchId, int total, Map<String, Long> byStatus, Map<String, Long> byStep,
                               long completed, long review, long rejected, long processing, long duplicate, long invalid,
                               List<Registration> items) {
    }

    /** 한 줄에 URL 하나. {@code #} 뒤는 메모, 빈 줄은 건너뛴다. 공백으로 나뉜 첫 토큰만 주소로 본다. */
    public static List<String> parseLines(String text) {
        List<String> urls = new ArrayList<>();
        if (text == null) {
            return urls;
        }
        for (String raw : text.split("\\r?\\n")) {
            String line = raw;
            int hash = line.indexOf('#');
            if (hash >= 0) {
                line = line.substring(0, hash);
            }
            line = line.strip();
            if (line.isEmpty()) {
                continue;
            }
            int space = line.indexOf(' ');
            urls.add(space > 0 ? line.substring(0, space) : line);
        }
        return urls;
    }

    public BulkResult register(CurrentUser user, List<String> urls) {
        user.require(Role.REGISTRAR);
        if (urls == null || urls.isEmpty()) {
            throw new ApiException(HttpStatus.BAD_REQUEST, "BAD_REQUEST", "등록할 주소가 없습니다.");
        }
        if (urls.size() > MAX_URLS) {
            throw new ApiException(HttpStatus.BAD_REQUEST, "BAD_REQUEST",
                    "한 번에 최대 " + MAX_URLS + "건까지 등록할 수 있습니다 (요청: " + urls.size() + "건).");
        }
        String batchId = UUID.randomUUID().toString().replace("-", "");
        Set<String> seenDomains = new HashSet<>();
        List<Item> items = new ArrayList<>(urls.size());
        int accepted = 0;
        int duplicate = 0;
        int invalid = 0;
        for (String url : urls) {
            Item item = registerOne(user, batchId, url == null ? "" : url, seenDomains);
            items.add(item);
            switch (item.status()) {
                case "DUPLICATE" -> duplicate++;
                case "INVALID" -> invalid++;
                default -> accepted++;
            }
        }
        audit.log(user.id(), "REGISTRATION_BULK", "registration_batch", null,
                "{\"batchId\":\"" + batchId + "\",\"total\":" + items.size() + ",\"accepted\":" + accepted
                        + ",\"duplicate\":" + duplicate + ",\"invalid\":" + invalid + "}");
        log.info("bulk registration batch={} by={} total={} accepted={} duplicate={} invalid={}", batchId, user.loginId(),
                items.size(), accepted, duplicate, invalid);
        return new BulkResult(batchId, items.size(), accepted, duplicate, invalid, items);
    }

    private Item registerOne(CurrentUser user, String batchId, String url, Set<String> seenDomains) {
        String input = url.strip();
        UrlNormalizer.Normalized normalized;
        try {
            normalized = UrlNormalizer.normalize(input);
        } catch (ApiException e) {
            return invalidItem(user, batchId, input, e.getMessage());
        }
        if (!seenDomains.add(normalized.domain())) {
            String message = "같은 묶음 안에 같은 도메인이 있어 첫 건만 등록합니다.";
            try {
                Long id = eachInItsOwnTransaction.execute(s -> insertRow(user, batchId, input, normalized.domain(),
                        "DUPLICATE", message));
                return new Item(input, id, normalized.domain(), "DUPLICATE", message);
            } catch (RuntimeException e) {
                log.warn("bulk registration batch={} url={} duplicate row failed: {}", batchId, input, e.toString());
                return new Item(input, null, normalized.domain(), "DUPLICATE", message);
            }
        }
        try {
            Registration reg = eachInItsOwnTransaction.execute(s -> {
                Registration r = registrations.register(user, input, Channel.WEB);
                jdbc.sql("UPDATE registration_request SET batch_id = ? WHERE id = ?").params(batchId, r.id()).update();
                return r;
            });
            return new Item(input, reg.id(), reg.domain(), reg.status(), reg.resultMessage());
        } catch (ApiException e) {
            return invalidItem(user, batchId, input, e.getMessage());
        } catch (RuntimeException e) {
            // DB 오류 등 한 건의 실패는 그 건만 INVALID로 남기고 다음 건으로 간다
            log.warn("bulk registration batch={} url={} failed: {}", batchId, input, e.toString());
            return invalidItem(user, batchId, input, "등록 처리 중 오류가 났습니다: " + e.getClass().getSimpleName());
        }
    }

    private Item invalidItem(CurrentUser user, String batchId, String input, String message) {
        Long id = null;
        try {
            id = eachInItsOwnTransaction.execute(s -> insertRow(user, batchId, input, truncate(input, 255), "INVALID", message));
        } catch (RuntimeException e) {
            log.warn("bulk registration batch={} url={} invalid row failed: {}", batchId, input, e.toString());
        }
        return new Item(input, id, null, "INVALID", message);
    }

    private long insertRow(CurrentUser user, String batchId, String input, String domain, String status, String message) {
        return jdbc.sql("""
                INSERT INTO registration_request (user_id, input_url, normalized_domain, channel, status, step, result_message, batch_id)
                VALUES (?, ?, ?, 'WEB', ?, ?, ?, ?) RETURNING id
                """)
                .params(user.id(), truncate(input.isEmpty() ? "(빈 줄)" : input, 2000), domain.isEmpty() ? "-" : domain,
                        status, status, truncate(message, 500), batchId)
                .query(Long.class).single();
    }

    private static String truncate(String s, int max) {
        return s == null ? null : s.length() <= max ? s : s.substring(0, max);
    }

    // ------------------------------------------------------------------ 묶음 진행 요약

    /**
     * 요청자 본인 또는 ADMIN만 본다. 묶음이 없으면 404.
     * batchId는 일괄 등록의 UUID이거나, 워커가 링크 모음(포털) 페이지 대신 등록한 언론사들의 {@code portal:<소스 id>}다 — 형식을 따로
     * 검사하지 않고 {@code registration_request.batch_id}로 그대로 찾는다.
     */
    public BatchSummary summary(CurrentUser user, String batchId) {
        List<Registration> items = jdbc.sql(RegistrationService.SELECT + " WHERE batch_id = ? ORDER BY id")
                .param(batchId).query(registrations::map).list();
        if (items.isEmpty()) {
            throw ApiException.notFound("등록 묶음");
        }
        if (!user.role().atLeast(Role.ADMIN)) {
            long owner = jdbc.sql("SELECT user_id FROM registration_request WHERE batch_id = ? ORDER BY id LIMIT 1")
                    .param(batchId).query(Long.class).single();
            if (owner != user.id()) {
                throw ApiException.notFound("등록 묶음");
            }
        }
        Map<String, Long> byStatus = new LinkedHashMap<>();
        Map<String, Long> byStep = new LinkedHashMap<>();
        for (Registration r : items) {
            byStatus.merge(r.status(), 1L, Long::sum);
            byStep.merge(r.step() == null ? "NONE" : r.step(), 1L, Long::sum);
        }
        long completed = byStatus.getOrDefault("COMPLETED", 0L);
        long review = byStatus.getOrDefault("REVIEW", 0L);
        long rejected = byStatus.getOrDefault("REJECTED", 0L);
        long processing = byStatus.getOrDefault("PROCESSING", 0L) + byStatus.getOrDefault("RECEIVED", 0L)
                + byStatus.getOrDefault("AWAITING_APPROVAL", 0L);
        return new BatchSummary(batchId, items.size(), byStatus, byStep, completed, review, rejected, processing,
                byStatus.getOrDefault("DUPLICATE", 0L), byStatus.getOrDefault("INVALID", 0L), items);
    }
}
