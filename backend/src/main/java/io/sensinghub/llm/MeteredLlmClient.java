package io.sensinghub.llm;

import java.time.Duration;
import java.time.LocalDate;
import java.time.OffsetDateTime;
import java.time.ZoneId;
import java.time.ZonedDateTime;
import java.time.format.DateTimeFormatter;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.function.Supplier;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.dao.DataAccessException;

import io.sensinghub.llm.LlmException.Kind;
import io.sensinghub.llm.LlmLedger.Pause;

/**
 * 챗봇 LLM을 사용량 장부(V19, {@link LlmLedger})에 묶는 감싸기. 워커 {@code llm_budget.MeteredLlm}과 같은 규칙이다.
 * <ul>
 *   <li>부르기 전에 모델이 쉬는 중(llm_cooldown)인지 본다. 쉬는 중이면 부르지 않고 "언제 풀리는지"를 RATE_LIMITED 안내로 던진다.
 *       ChatService는 다른 LLM 오류처럼 안내 답(meta.error)으로 저장하고, 그 턴은 다음 턴에 LLM에 다시 보여 주지 않는다.</li>
 *   <li>부를 때 llm_usage(chat)의 calls를 늘리고 끝나면 토큰을 더한다. 429면 calls에서 빼 rate_limited로 옮기고 모델을 쉬는 중으로 적는다
 *       (워커 요약도 그동안 부르지 않는다).</li>
 *   <li>챗봇에는 하루 상한이 없다: 워커 요약이 상한(SENSING_ENRICH_DAILY_CALLS)으로 남긴 몫을 쓴다.</li>
 *   <li>장부를 못 읽거나 못 써도 챗봇은 막지 않는다 (경고만 남긴다).</li>
 * </ul>
 */
public class MeteredLlmClient implements LlmClient {

    static final ZoneId KST = ZoneId.of("Asia/Seoul");
    private static final Logger log = LoggerFactory.getLogger(MeteredLlmClient.class);
    private static final DateTimeFormatter HM = DateTimeFormatter.ofPattern("HH:mm");
    private static final DateTimeFormatter MD_HM = DateTimeFormatter.ofPattern("M월 d일 HH:mm");

    private final LlmClient inner;
    private final LlmLedger ledger;
    private final String model;
    private final String purpose;

    public MeteredLlmClient(LlmClient inner, LlmLedger ledger, String model, String purpose) {
        this.inner = inner;
        this.ledger = ledger;
        this.model = model;
        this.purpose = purpose;
    }

    /** 장부에 쓰는 모델 이름 (워커와 같은 값이어야 한도를 함께 센다). */
    public String model() {
        return model;
    }

    @Override
    public Response chat(String system, List<Message> messages, List<Tool> tools, boolean allowTools) {
        Optional<Pause> pause = safely(() -> ledger.cooldown(model)).flatMap(p -> p);
        if (pause.isPresent()) {
            throw new LlmException(Kind.RATE_LIMITED, message(pause.get(), ZonedDateTime.now(KST)), null,
                    "paused: " + pause.get());
        }
        LocalDate day = safely(() -> ledger.reserve(model, purpose)).orElse(null);
        Response r;
        try {
            r = inner.chat(system, messages, tools, allowTools);
        } catch (LlmException e) {
            if (e.kind() == Kind.RATE_LIMITED && e.rateLimit() != null) {
                write(day, () -> ledger.rateLimited(day, model, purpose));
                Optional<Pause> now = safely(() -> ledger.coolDown(model, purpose, e.rateLimit(), e.detail())).flatMap(p -> p);
                now.ifPresent(p -> log.warn("llm {} paused until {} ({} {}) after HTTP 429 from {}", model, p.until(),
                        p.reason(), p.quotaId() == null ? "" : p.quotaId(), purpose));
                if (now.isPresent() && now.get().daily()) {
                    throw new LlmException(Kind.RATE_LIMITED, message(now.get(), ZonedDateTime.now(KST)), e.httpStatus(),
                            e.detail(), e.rateLimit());
                }
            } else {
                write(day, () -> ledger.failed(day, model, purpose));
            }
            throw e;
        }
        Map<String, Object> usage = r.usage() == null ? Map.of() : r.usage();
        write(day, () -> ledger.succeeded(day, model, purpose, number(usage.get("inputTokens")),
                number(usage.get("outputTokens"))));
        return r;
    }

    /**
     * 쉬는 중 안내. 하루 한도면 "한국 시간 16:00쯤 풀리니"처럼 풀리는 시각을, 짧은 한도면 남은 초를 알려 준다.
     * 하루 한도 문장에는 예전 안내와 같은 "하루 사용 한도"를 남긴다.
     */
    static String message(Pause p, ZonedDateTime now) {
        if (p.daily()) {
            return "LLM 하루 사용 한도(무료 등급)를 오늘 다 썼습니다. 한국 시간 " + when(p.until(), now)
                    + "쯤 풀리니 그때 다시 시도해 주세요.";
        }
        long seconds = Math.max(1, (long) Math.ceil(Duration.between(now.toInstant(), p.until().toInstant()).toMillis() / 1000.0));
        if (seconds <= 120) {
            return "LLM 호출 한도를 잠시 넘었습니다. 약 " + seconds + "초 뒤에 다시 시도해 주세요.";
        }
        return "LLM 호출 한도를 넘었습니다. 한국 시간 " + when(p.until(), now) + "쯤 다시 시도해 주세요.";
    }

    /** 오늘이면 "16:00", 내일이면 "내일 16:00", 그 뒤면 "10월 21일 16:00" (한국 시간). */
    static String when(OffsetDateTime until, ZonedDateTime now) {
        ZonedDateTime at = until.atZoneSameInstant(KST);
        LocalDate today = now.withZoneSameInstant(KST).toLocalDate();
        if (at.toLocalDate().equals(today)) {
            return at.format(HM);
        }
        if (at.toLocalDate().equals(today.plusDays(1))) {
            return "내일 " + at.format(HM);
        }
        return at.format(MD_HM);
    }

    private <T> Optional<T> safely(Supplier<T> action) {
        try {
            return Optional.ofNullable(action.get());
        } catch (DataAccessException e) {
            log.warn("llm usage ledger unavailable: {}", e.getMessage());
            return Optional.empty();
        }
    }

    private void write(LocalDate day, Runnable action) {
        if (day == null) {
            return;   // 예약을 못 적었다 (장부 오류)
        }
        safely(() -> {
            action.run();
            return Boolean.TRUE;
        });
    }

    private static long number(Object value) {
        return value instanceof Number n ? n.longValue() : 0L;
    }

    @Override
    public boolean configured() {
        return inner.configured();
    }

    @Override
    public String describe() {
        return inner.describe();
    }
}
