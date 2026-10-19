package io.sensinghub.llm;

import java.io.IOException;
import java.net.ProxySelector;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.net.http.HttpTimeoutException;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.Locale;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;

import io.sensinghub.llm.LlmException.Kind;

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
            String lower = body == null ? "" : body.toLowerCase(Locale.ROOT);
            if (lower.contains("perday") || lower.contains("per day") || lower.contains("daily")) {
                return new LlmException(Kind.RATE_LIMITED, "LLM 하루 사용 한도(무료 등급)를 다 썼습니다. "
                        + "내일 다시 시도하거나 관리자에게 알려 주세요.", status, detail);
            }
            Integer seconds = retrySeconds(body, retryAfter);
            return new LlmException(Kind.RATE_LIMITED, "LLM 호출 한도를 잠시 넘었습니다. "
                    + (seconds != null ? "약 " + seconds + "초 뒤에" : "1분쯤 뒤에") + " 다시 시도해 주세요.", status, detail);
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
