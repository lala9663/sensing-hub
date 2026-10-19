package io.sensinghub.llm;

import java.time.DateTimeException;
import java.time.LocalDate;
import java.time.OffsetDateTime;
import java.time.ZoneId;
import java.util.List;
import java.util.Optional;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Component;

import io.sensinghub.llm.LlmException.RateLimit;

/**
 * LLM 사용량 장부(llm_usage)와 모델별 "쉬는 중"(llm_cooldown) — V19. 챗봇·자동 요약·등록 레시피가 같은 모델·같은 키의
 * 무료 한도(하루 요청 수)를 나눠 쓴다. 워커 {@code sensing_worker.llm_budget.LlmLedger}와 같은 테이블·같은 규칙이다.
 * <ul>
 *   <li>날짜는 쿼터 기준 시간대({@code SENSING_LLM_QUOTA_TZ}, 기본 America/Los_Angeles — Gemini 무료 한도는 태평양 시간 자정에
 *       초기화)의 날짜이고 DB 시계로 센다. 워커와 백엔드 시계가 조금 달라도 같은 날짜·같은 쉬는 끝 시각을 쓴다.</li>
 *   <li>호출 전에 calls를 하나 늘리고(예약), 429면 calls에서 빼 rate_limited로 옮긴다 (거절된 요청은 제공자 한도에 들어가지 않는다).</li>
 *   <li>429의 쉬는 끝 시각: 하루 한도면 쿼터 기준 다음 자정과 retryDelay 중 늦은 쪽, 그 밖이면 retryDelay(없으면 60초).
 *       이미 더 늦게까지 쉬는 중이면 줄이지 않는다.</li>
 * </ul>
 * 모든 쓰기는 한 문장짜리 자동 커밋이다 (LLM 호출 동안 트랜잭션을 잡지 않는다).
 */
@Component
public class LlmLedger {

    public static final String CHAT = "chat";
    public static final String ENRICH = "enrich";
    public static final String RECIPE = "recipe";
    public static final String EMBED = "embed";
    static final String DEFAULT_TZ = "America/Los_Angeles";
    static final int DEFAULT_RETRY_SECONDS = 60;

    private static final Logger log = LoggerFactory.getLogger(LlmLedger.class);
    private static final String TODAY = "CAST(now() AT TIME ZONE :tz AS date)";
    private static final String NEXT_RESET = "(CAST(CAST(now() AT TIME ZONE :tz AS date) + 1 AS timestamp) AT TIME ZONE :tz)";

    /** 지금 이 모델을 부르지 않는다. reason: DAILY | MINUTE | RATE_LIMITED. */
    public record Pause(String reason, OffsetDateTime until, String quotaId) {

        public boolean daily() {
            return RateLimit.DAILY.equals(reason);
        }
    }

    /** 오늘(쿼터 기준) 모델·용도 한 줄. dailyCap: 워커가 건 하루 상한 (enrich만, 없으면 null). */
    public record UsageRow(String model, String purpose, long calls, long rateLimited, long errors, long inputTokens,
                           long outputTokens, Integer dailyCap, OffsetDateTime updatedAt) {
    }

    /** 지금 쉬는 중인 모델. secondsLeft: 풀릴 때까지 남은 초. */
    public record CooldownRow(String model, String reason, OffsetDateTime until, long secondsLeft, String quotaId,
                              Integer retrySeconds, String purpose, OffsetDateTime since) {
    }

    /**
     * 운영 지표용 오늘 사용량.
     * @param resetAt        다음 초기화 시각 (쿼터 기준 시간대의 자정)
     * @param enrichCalls    오늘 요약(enrich) 호출 수 (모델 합)
     * @param enrichDailyCap 요약 하루 상한: 워커가 장부에 남긴 값, 아직 없으면 백엔드 설정(SENSING_ENRICH_DAILY_CALLS). 0이면 상한 없음
     */
    public record Today(LocalDate quotaDay, String quotaTimeZone, OffsetDateTime resetAt, long enrichCalls,
                        int enrichDailyCap, List<UsageRow> usage, List<CooldownRow> cooldowns) {
    }

    private final JdbcClient jdbc;
    private final String tz;

    public LlmLedger(JdbcClient jdbc, @Value("${sensing.llm.quota-tz:" + DEFAULT_TZ + "}") String quotaTimeZone) {
        this.jdbc = jdbc;
        this.tz = zone(quotaTimeZone);
    }

    static String zone(String value) {
        String v = value == null || value.isBlank() ? DEFAULT_TZ : value.strip();
        try {
            return ZoneId.of(v).getId();
        } catch (DateTimeException e) {
            log.warn("unknown SENSING_LLM_QUOTA_TZ {}, using {}", v, DEFAULT_TZ);
            return DEFAULT_TZ;
        }
    }

    public String quotaTimeZone() {
        return tz;
    }

    // ------------------------------------------------------------------ 사용량

    /** 호출 하나를 예약한다 (calls + 1, 상한 없음). 쿼터 기준 날짜를 돌려준다. */
    LocalDate reserve(String model, String purpose) {
        return jdbc.sql("""
                INSERT INTO llm_usage AS u (quota_day, model, purpose, calls)
                VALUES (%s, :model, :purpose, 1)
                ON CONFLICT (quota_day, model, purpose) DO UPDATE SET calls = u.calls + 1, updated_at = now()
                RETURNING u.quota_day
                """.formatted(TODAY))
                .param("tz", tz).param("model", model).param("purpose", purpose)
                .query(LocalDate.class).single();
    }

    void succeeded(LocalDate day, String model, String purpose, long inputTokens, long outputTokens) {
        jdbc.sql("""
                UPDATE llm_usage SET input_tokens = input_tokens + ?, output_tokens = output_tokens + ?, updated_at = now()
                WHERE quota_day = ? AND model = ? AND purpose = ?
                """).params(inputTokens, outputTokens, day, model, purpose).update();
    }

    /** 429로 거절된 요청: calls에서 빼 rate_limited로 옮긴다. */
    void rateLimited(LocalDate day, String model, String purpose) {
        jdbc.sql("""
                UPDATE llm_usage SET calls = GREATEST(calls - 1, 0), rate_limited = rate_limited + 1, updated_at = now()
                WHERE quota_day = ? AND model = ? AND purpose = ?
                """).params(day, model, purpose).update();
    }

    void failed(LocalDate day, String model, String purpose) {
        jdbc.sql("UPDATE llm_usage SET errors = errors + 1, updated_at = now() WHERE quota_day = ? AND model = ? AND purpose = ?")
                .params(day, model, purpose).update();
    }

    // ------------------------------------------------------------------ 쉬는 중

    public Optional<Pause> cooldown(String model) {
        return jdbc.sql("SELECT reason, until_at, quota_id FROM llm_cooldown WHERE model = ? AND until_at > now()")
                .param(model)
                .query((rs, n) -> new Pause(rs.getString("reason"), rs.getObject("until_at", OffsetDateTime.class),
                        rs.getString("quota_id")))
                .optional();
    }

    /** 429를 받았다: 한도 종류에 맞춰 쉬는 끝 시각을 적고(더 늦게까지 쉬는 중이면 그대로), 지금의 쉬는 중을 돌려준다. */
    Optional<Pause> coolDown(String model, String purpose, RateLimit limit, String detail) {
        String quotaId = limit.quotaId() == null ? null : limit.quotaId().substring(0, Math.min(200, limit.quotaId().length()));
        jdbc.sql("""
                INSERT INTO llm_cooldown AS c (model, until_at, reason, quota_id, retry_seconds, purpose, detail, created_at)
                VALUES (:model,
                        CASE WHEN :reason = 'DAILY'
                             THEN GREATEST(now() + make_interval(secs => COALESCE(CAST(:retry AS int), 0)), %s)
                             ELSE now() + make_interval(secs => COALESCE(CAST(:retry AS int), :fallback)) END,
                        :reason, :quotaId, CAST(:retry AS int), :purpose, :detail, now())
                ON CONFLICT (model) DO UPDATE
                    SET until_at = EXCLUDED.until_at, reason = EXCLUDED.reason, quota_id = EXCLUDED.quota_id,
                        retry_seconds = EXCLUDED.retry_seconds, purpose = EXCLUDED.purpose, detail = EXCLUDED.detail,
                        created_at = now()
                    WHERE c.until_at < EXCLUDED.until_at
                """.formatted(NEXT_RESET))
                .param("tz", tz).param("model", model).param("reason", limit.scope()).param("quotaId", quotaId)
                .param("retry", limit.retrySeconds()).param("fallback", DEFAULT_RETRY_SECONDS).param("purpose", purpose)
                .param("detail", detail == null ? null : detail.substring(0, Math.min(1000, detail.length())))
                .update();
        return cooldown(model);
    }

    // ------------------------------------------------------------------ 운영 지표

    public Today today(int configuredEnrichCap) {
        record Day(LocalDate day, OffsetDateTime resetAt) {
        }
        Day day = jdbc.sql("SELECT " + TODAY + " AS day, " + NEXT_RESET + " AS reset_at").param("tz", tz)
                .query((rs, n) -> new Day(rs.getObject("day", LocalDate.class), rs.getObject("reset_at", OffsetDateTime.class)))
                .single();
        List<UsageRow> usage = jdbc.sql("""
                SELECT model, purpose, calls, rate_limited, errors, input_tokens, output_tokens, daily_cap, updated_at
                FROM llm_usage WHERE quota_day = ? ORDER BY model, purpose
                """).param(day.day()).query((rs, n) -> new UsageRow(rs.getString("model"), rs.getString("purpose"),
                        rs.getLong("calls"), rs.getLong("rate_limited"), rs.getLong("errors"), rs.getLong("input_tokens"),
                        rs.getLong("output_tokens"), rs.getObject("daily_cap", Integer.class),
                        rs.getObject("updated_at", OffsetDateTime.class))).list();
        List<CooldownRow> cooldowns = jdbc.sql("""
                SELECT model, reason, until_at, CEIL(EXTRACT(EPOCH FROM until_at - now()))::bigint AS seconds_left, quota_id,
                       retry_seconds, purpose, created_at
                FROM llm_cooldown WHERE until_at > now() ORDER BY until_at DESC
                """).query((rs, n) -> new CooldownRow(rs.getString("model"), rs.getString("reason"),
                        rs.getObject("until_at", OffsetDateTime.class), Math.max(0, rs.getLong("seconds_left")),
                        rs.getString("quota_id"), rs.getObject("retry_seconds", Integer.class), rs.getString("purpose"),
                        rs.getObject("created_at", OffsetDateTime.class))).list();
        long enrichCalls = usage.stream().filter(u -> ENRICH.equals(u.purpose())).mapToLong(UsageRow::calls).sum();
        int cap = usage.stream().filter(u -> ENRICH.equals(u.purpose()) && u.dailyCap() != null)
                .mapToInt(UsageRow::dailyCap).max().orElse(Math.max(0, configuredEnrichCap));
        return new Today(day.day(), tz, day.resetAt(), enrichCalls, cap, usage, cooldowns);
    }
}
