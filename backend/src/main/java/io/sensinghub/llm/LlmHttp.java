package io.sensinghub.llm;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.net.ProxySelector;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.net.http.HttpTimeoutException;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;

import io.sensinghub.llm.LlmException.Kind;
import io.sensinghub.llm.LlmException.RateLimit;

/**
 * LLM 제공자에 JSON을 POST하는 공통 코드 (추가 의존성 없이 java.net.http).
 * HTTP 오류를 {@link LlmException}의 종류와 사용자에게 보일 문장으로 바꾼다.
 * <ul>
 *   <li>5xx·529(서버 혼잡)는 잠깐 쉬고 한 번만 다시 보낸다. 429(호출 한도)는 다시 보내지 않는다 — 무료 등급에서 재시도는 한도만 더 쓴다.</li>
 *   <li>키는 헤더로만 보낸다. 로그에는 상태 코드와 응답 본문 앞부분만 남긴다 (요청 본문·헤더는 남기지 않는다).</li>
 * </ul>
 */
final class LlmHttp {

    private static final Logger log = LoggerFactory.getLogger(LlmHttp.class);
    /** Gemini 429 본문의 "Please retry in 41.3s" 또는 RetryInfo "retryDelay": "41s" */
    private static final Pattern RETRY_IN = Pattern.compile("(?i)retry in ([0-9]+(?:\\.[0-9]+)?)s|\"retryDelay\"\\s*:\\s*\"([0-9]+)s\"");
    /** Gemini 429 본문 QuotaFailure.violations[].quotaId */
    private static final Pattern QUOTA_ID = Pattern.compile("\"quotaId\"\\s*:\\s*\"([^\"]+)\"");

    private final HttpClient client;
    private final ObjectMapper json;
    private final Duration timeout;
    private final long retryDelayMillis;

    LlmHttp(ObjectMapper json, int timeoutSeconds, long retryDelayMillis) {
        this.json = json;
        this.timeout = Duration.ofSeconds(Math.max(1, timeoutSeconds));
        this.retryDelayMillis = retryDelayMillis;
        this.client = HttpClient.newBuilder()
                .version(HttpClient.Version.HTTP_1_1)
                .connectTimeout(Duration.ofSeconds(10))
                .proxy(ProxySelector.getDefault())
                .build();
    }

    JsonNode post(String url, Map<String, String> headers, Object body) {
        String payload;
        try {
            payload = json.writeValueAsString(body);
        } catch (JsonProcessingException e) {
            throw new IllegalStateException(e);
        }
        for (int attempt = 0; ; attempt++) {
            HttpRequest.Builder request = HttpRequest.newBuilder(URI.create(url))
                    .timeout(timeout)
                    .header("Content-Type", "application/json")
                    .POST(HttpRequest.BodyPublishers.ofString(payload, StandardCharsets.UTF_8));
            headers.forEach(request::header);
            HttpResponse<String> response;
            try {
                response = client.send(request.build(), HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
            } catch (HttpTimeoutException e) {
                log.warn("llm {} timed out after {}s", url, timeout.toSeconds());
                throw new LlmException(Kind.TIMEOUT, "LLM 응답이 " + timeout.toSeconds()
                        + "초 안에 오지 않았습니다. 잠시 후 다시 시도해 주세요.", null, e.toString());
            } catch (IOException e) {
                log.warn("llm {} connection failed: {}", url, e.toString());
                throw new LlmException(Kind.NETWORK, "LLM 서버에 연결하지 못했습니다. 잠시 후 다시 시도하거나 관리자에게 알려 주세요.",
                        null, e.toString());
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                throw new LlmException(Kind.NETWORK, "LLM 호출이 중단되었습니다.", null, e.toString());
            }
            int status = response.statusCode();
            if (status >= 200 && status < 300) {
                try {
                    return json.readTree(response.body());
                } catch (JsonProcessingException e) {
                    throw new LlmException(Kind.BAD_RESPONSE, "LLM 응답을 해석하지 못했습니다. 잠시 후 다시 시도해 주세요.",
                            status, head(response.body()));
                }
            }
            log.warn("llm {} HTTP {}: {}", url, status, head(response.body()));
            if ((status >= 500 || status == 529) && attempt == 0) {
                sleep();
                continue;
            }
            throw error(status, response.body(), response.headers().firstValue("retry-after").orElse(null));
        }
    }

    /** SSE 이벤트 하나. event: 이벤트 이름 (없으면 null — OpenAI 호환은 이름 없이 data만 보낸다). */
    interface SseHandler {
        void event(String event, String data);
    }

    /** 스트리밍이 형식 문제(400·스트림 해석 실패)로 실패한 뒤 스트리밍을 쉬는 시간. 그동안은 한 번에 받기만 한다 (호출을 두 번 쓰지 않게). */
    static final Duration STREAM_PAUSE = Duration.ofMinutes(10);
    private static final ScheduledExecutorService WATCHDOG = Executors.newSingleThreadScheduledExecutor(r -> {
        Thread t = new Thread(r, "llm-stream-watchdog");
        t.setDaemon(true);
        return t;
    });
    private volatile long streamPausedUntil = System.nanoTime();

    /** 스트리밍을 쉬는 중이 아닌가 ({@link #streamFailed}). */
    boolean streamingAvailable() {
        return System.nanoTime() - streamPausedUntil >= 0;
    }

    /**
     * 스트리밍 실패를 알린다. 요청 자체를 거절했거나(BAD_REQUEST — 스트리밍을 모르는 게이트웨이) 스트림을 해석하지 못했으면(BAD_RESPONSE)
     * {@link #STREAM_PAUSE} 동안 스트리밍을 끈다. 서버 오류·연결 끊김은 일시적이라 끄지 않는다.
     */
    void streamFailed(LlmException e) {
        if (e.kind() == Kind.BAD_REQUEST || e.kind() == Kind.BAD_RESPONSE) {
            streamPausedUntil = System.nanoTime() + STREAM_PAUSE.toNanos();
            log.warn("llm streaming paused for {} min after {}: {}", STREAM_PAUSE.toMinutes(), e.kind(), e.detail());
        }
    }

    /**
     * SSE로 답하는 POST. 이벤트를 받는 대로 handler에 넘긴다 (handler가 던진 LlmException은 그대로 나간다).
     * 2xx가 아니면 {@link #post}와 같은 오류를 던진다 (재시도하지 않는다 — 5xx면 {@link LlmClient#chatStreaming}이 한 번에 받기로 다시 보낸다).
     * 제공자가 stream을 무시하고 JSON으로 답했으면 그 JSON을 돌려주고(호출자가 한 번에 받은 답처럼 해석한다), SSE를 다 읽었으면 null.
     * 제한 시간은 요청부터 스트림 끝까지 전체다: 넘으면 스트림을 닫고 TIMEOUT.
     */
    JsonNode stream(String url, Map<String, String> headers, Object body, SseHandler handler) {
        String payload;
        try {
            payload = json.writeValueAsString(body);
        } catch (JsonProcessingException e) {
            throw new IllegalStateException(e);
        }
        long started = System.nanoTime();
        HttpRequest.Builder request = HttpRequest.newBuilder(URI.create(url))
                .timeout(timeout)
                .header("Content-Type", "application/json")
                .header("Accept", "text/event-stream")
                .POST(HttpRequest.BodyPublishers.ofString(payload, StandardCharsets.UTF_8));
        headers.forEach(request::header);
        HttpResponse<InputStream> response;
        try {
            response = client.send(request.build(), HttpResponse.BodyHandlers.ofInputStream());
        } catch (HttpTimeoutException e) {
            log.warn("llm {} (stream) timed out after {}s", url, timeout.toSeconds());
            throw timeoutError(e.toString());
        } catch (IOException e) {
            log.warn("llm {} (stream) connection failed: {}", url, e.toString());
            throw new LlmException(Kind.NETWORK, "LLM 서버에 연결하지 못했습니다. 잠시 후 다시 시도하거나 관리자에게 알려 주세요.",
                    null, e.toString());
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new LlmException(Kind.NETWORK, "LLM 호출이 중단되었습니다.", null, e.toString());
        }
        int status = response.statusCode();
        InputStream in = response.body();
        if (status < 200 || status >= 300) {
            String text = readAll(in);
            log.warn("llm {} (stream) HTTP {}: {}", url, status, head(text));
            throw error(status, text, response.headers().firstValue("retry-after").orElse(null));
        }
        String type = response.headers().firstValue("content-type").orElse("").toLowerCase(Locale.ROOT);
        if (!type.contains("text/event-stream")) {
            String text = readAll(in);
            try {
                return json.readTree(text);
            } catch (JsonProcessingException e) {
                throw new LlmException(Kind.BAD_RESPONSE, "LLM 응답을 해석하지 못했습니다. 잠시 후 다시 시도해 주세요.",
                        status, head(text));
            }
        }
        long left = timeout.toNanos() - (System.nanoTime() - started);
        AtomicBoolean timedOut = new AtomicBoolean();
        ScheduledFuture<?> watchdog = WATCHDOG.schedule(() -> {
            timedOut.set(true);
            closeQuietly(in);
        }, Math.max(1, left), TimeUnit.NANOSECONDS);
        try (BufferedReader reader = new BufferedReader(new InputStreamReader(in, StandardCharsets.UTF_8))) {
            String event = null;
            StringBuilder data = null;
            String line;
            while ((line = reader.readLine()) != null) {
                if (line.isEmpty()) {
                    if (data != null) {
                        handler.event(event, data.toString());
                    }
                    event = null;
                    data = null;
                    continue;
                }
                if (line.startsWith(":")) {
                    continue;   // 주석 (keep-alive)
                }
                int colon = line.indexOf(':');
                String field = colon < 0 ? line : line.substring(0, colon);
                String value = colon < 0 ? "" : line.substring(colon + 1);
                if (value.startsWith(" ")) {
                    value = value.substring(1);
                }
                if ("event".equals(field)) {
                    event = value;
                } else if ("data".equals(field)) {
                    data = data == null ? new StringBuilder(value) : data.append('\n').append(value);
                }
            }
            if (timedOut.get()) {   // 닫힌 스트림은 끝(EOF)처럼 읽히기도 한다
                log.warn("llm {} (stream) timed out after {}s", url, timeout.toSeconds());
                throw timeoutError("stream closed by watchdog");
            }
            if (data != null) {
                handler.event(event, data.toString());
            }
            return null;
        } catch (IOException e) {
            if (timedOut.get()) {
                log.warn("llm {} (stream) timed out after {}s", url, timeout.toSeconds());
                throw timeoutError(e.toString());
            }
            log.warn("llm {} (stream) broke: {}", url, e.toString());
            throw new LlmException(Kind.NETWORK, "LLM 응답을 받다가 연결이 끊겼습니다. 잠시 후 다시 시도해 주세요.", status, e.toString());
        } finally {
            watchdog.cancel(false);
        }
    }

    private LlmException timeoutError(String detail) {
        return new LlmException(Kind.TIMEOUT, "LLM 응답이 " + timeout.toSeconds()
                + "초 안에 오지 않았습니다. 잠시 후 다시 시도해 주세요.", null, detail);
    }

    /** 오류 본문 (앞 64KB만). */
    private static String readAll(InputStream in) {
        try (InputStream body = in) {
            byte[] bytes = body.readNBytes(64 * 1024);
            return new String(bytes, StandardCharsets.UTF_8);
        } catch (IOException e) {
            return "";
        }
    }

    private static void closeQuietly(InputStream in) {
        try {
            in.close();
        } catch (IOException ignored) {
            // 이미 닫혔다
        }
    }

    private void sleep() {
        if (retryDelayMillis <= 0) {
            return;
        }
        try {
            Thread.sleep(retryDelayMillis);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }

    /** HTTP 오류 → 종류와 사용자 문장. 본문은 상세로만 둔다. */
    static LlmException error(int status, String body, String retryAfter) {
        String detail = head(body);
        if (status == 429) {
            RateLimit limit = rateLimit(body, retryAfter);
            if (limit.daily()) {
                return new LlmException(Kind.RATE_LIMITED, "LLM 하루 사용 한도(무료 등급)를 다 썼습니다. "
                        + "내일 다시 시도하거나 관리자에게 알려 주세요.", status, detail, limit);
            }
            Integer seconds = limit.retrySeconds();
            return new LlmException(Kind.RATE_LIMITED, "LLM 호출 한도를 잠시 넘었습니다. "
                    + (seconds != null ? "약 " + seconds + "초 뒤에" : "1분쯤 뒤에") + " 다시 시도해 주세요.", status, detail,
                    limit);
        }
        if (status == 401 || status == 403) {
            return new LlmException(Kind.AUTH, "LLM API 키가 올바르지 않거나 권한이 없습니다 (HTTP " + status
                    + "). 관리자에게 알려 주세요.", status, detail);
        }
        if (status == 404) {
            return new LlmException(Kind.BAD_REQUEST, "LLM 모델 또는 주소를 찾을 수 없습니다 (HTTP 404). "
                    + "관리자에게 SENSING_LLM_MODEL·SENSING_LLM_BASE_URL 확인을 요청해 주세요.", status, detail);
        }
        if (status >= 500 || status == 529) {
            return new LlmException(Kind.SERVER_ERROR, "LLM 서버에 일시적인 문제가 있습니다 (HTTP " + status
                    + "). 잠시 후 다시 시도해 주세요.", status, detail);
        }
        return new LlmException(Kind.BAD_REQUEST, "LLM이 요청을 받아들이지 않았습니다 (HTTP " + status
                + "). 질문을 조금 바꿔 다시 시도해 주세요.", status, detail);
    }

    /**
     * 429 본문·retry-after → 한도 종류. 워커 llm.parse_rate_limit와 같은 규칙: Gemini는 본문 details에
     * QuotaFailure.violations[].quotaId("GenerateRequestsPerDayPerProjectPerModel-FreeTier" 등)와 RetryInfo.retryDelay를 준다.
     * quotaId에 PerDay가 하나라도 있으면 하루 한도, PerMinute면 분당 한도. quotaId가 없으면 본문의 "per day"·"daily"로 가린다.
     */
    static RateLimit rateLimit(String body, String retryAfter) {
        String text = body == null ? "" : body;
        List<String> ids = new ArrayList<>();
        Matcher m = QUOTA_ID.matcher(text);
        while (m.find()) {
            ids.add(m.group(1));
        }
        String daily = ids.stream().filter(q -> q.toLowerCase(Locale.ROOT).contains("perday")).findFirst().orElse(null);
        String minute = ids.stream().filter(q -> q.toLowerCase(Locale.ROOT).contains("perminute")).findFirst().orElse(null);
        String lower = text.toLowerCase(Locale.ROOT);
        Integer seconds = retrySeconds(body, retryAfter);
        if (daily != null) {
            return new RateLimit(RateLimit.DAILY, daily, seconds);
        }
        if (minute != null) {
            return new RateLimit(RateLimit.MINUTE, minute, seconds);
        }
        if (ids.isEmpty() && (lower.contains("perday") || lower.contains("per day") || lower.contains("daily"))) {
            return new RateLimit(RateLimit.DAILY, null, seconds);
        }
        return new RateLimit(RateLimit.OTHER, ids.isEmpty() ? null : ids.get(0), seconds);
    }

    static Integer retrySeconds(String body, String retryAfter) {
        if (retryAfter != null) {
            try {
                return Math.max(1, Integer.parseInt(retryAfter.trim()));
            } catch (NumberFormatException ignored) {
                // HTTP 날짜 형식은 무시한다
            }
        }
        if (body == null) {
            return null;
        }
        Matcher m = RETRY_IN.matcher(body);
        if (m.find()) {
            String value = m.group(1) != null ? m.group(1) : m.group(2);
            return Math.max(1, (int) Math.ceil(Double.parseDouble(value)));
        }
        return null;
    }

    static String head(String body) {
        if (body == null) {
            return "";
        }
        String flat = body.replaceAll("\\s+", " ").strip();
        return flat.length() > 500 ? flat.substring(0, 500) : flat;
    }
}
