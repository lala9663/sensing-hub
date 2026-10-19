package io.sensinghub.llm;

import java.util.Locale;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

import com.fasterxml.jackson.databind.ObjectMapper;

/**
 * 챗봇 LLM 제공자 선택. 워커(worker/src/sensing_worker/llm.py make_llm)와 같은 환경 변수와 같은 값을 쓴다:
 * <ul>
 *   <li>{@code SENSING_LLM_PROVIDER}: none(기본) | openai(OpenAI 호환, 별칭 openai-compat) | anthropic</li>
 *   <li>openai: {@code SENSING_LLM_BASE_URL}, {@code SENSING_LLM_MODEL}(필수), {@code SENSING_LLM_API_KEY}(비우면 인증 헤더 없음)</li>
 *   <li>anthropic: {@code ANTHROPIC_API_KEY}(필수), {@code ANTHROPIC_BASE_URL}, {@code SENSING_LLM_MODEL}(기본 claude-opus-5-5)</li>
 * </ul>
 * 설정이 모자라면 기동을 막지 않고 {@link NotConfiguredLlmClient}로 둔다 (챗봇만 "설정되지 않았습니다"로 답한다).
 * <p>
 * 챗봇은 {@code SENSING_CHAT_LLM_MODEL}(비우면 SENSING_LLM_MODEL)을 쓴다. 무료 한도가 모델별이므로 챗봇만 다른 무료 모델로 돌려
 * 요약과 한도를 따로 쓰게 할 수 있다. 설정된 클라이언트는 {@link MeteredLlmClient}로 감싸 워커와 같은 사용량 장부·쉬는 중 기록을 쓴다.
 */
@Configuration
public class LlmConfig {

    static final String ANTHROPIC_DEFAULT_MODEL = "claude-opus-5-5";
    private static final Logger log = LoggerFactory.getLogger(LlmConfig.class);

    @Bean
    public LlmClient chatLlmClient(ObjectMapper json, LlmLedger ledger,
                                   @Value("${sensing.llm.provider:none}") String provider,
                                   @Value("${sensing.llm.model:}") String model,
                                   @Value("${sensing.llm.chat-model:}") String chatModel,
                                   @Value("${sensing.llm.base-url:}") String baseUrl,
                                   @Value("${sensing.llm.api-key:}") String apiKey,
                                   @Value("${sensing.llm.anthropic-api-key:}") String anthropicKey,
                                   @Value("${sensing.llm.anthropic-base-url:https://api.anthropic.com}") String anthropicBaseUrl,
                                   @Value("${sensing.llm.timeout-seconds:60}") String timeout,
                                   @Value("${sensing.llm.max-tokens:0}") int maxTokens,
                                   @Value("${sensing.llm.retry-delay-ms:1500}") long retryDelayMillis) {
        String effective = isBlank(chatModel) ? model : chatModel.strip();
        LlmClient client = llmClient(json, provider, effective, baseUrl, apiKey, anthropicKey, anthropicBaseUrl, timeout,
                maxTokens, retryDelayMillis);
        if (client instanceof NotConfiguredLlmClient) {
            return client;
        }
        String ledgerModel = isBlank(effective) ? ANTHROPIC_DEFAULT_MODEL : effective.strip();
        log.info("chat LLM usage ledger: model {}, quota day in {}", ledgerModel, ledger.quotaTimeZone());
        return new MeteredLlmClient(client, ledger, ledgerModel, LlmLedger.CHAT);
    }

    /** 제공자 클라이언트 그대로 (장부 없이). model은 이미 고른 값이다. */
    public LlmClient llmClient(ObjectMapper json, String provider, String model, String baseUrl, String apiKey,
                               String anthropicKey, String anthropicBaseUrl, String timeout, int maxTokens,
                               long retryDelayMillis) {
        String p = provider == null ? "" : provider.strip().toLowerCase(Locale.ROOT);
        int timeoutSeconds = seconds(timeout);
        LlmClient client = switch (p) {
            case "", "none", "off", "false" -> new NotConfiguredLlmClient();
            case "openai", "openai-compat", "openai_compat" -> {
                if (isBlank(baseUrl)) {
                    yield new NotConfiguredLlmClient("SENSING_LLM_BASE_URL이 없습니다");
                }
                if (isBlank(model)) {
                    yield new NotConfiguredLlmClient("SENSING_LLM_MODEL이 없습니다");
                }
                yield new OpenAiCompatLlmClient(baseUrl.strip(), model.strip(), apiKey, timeoutSeconds, maxTokens,
                        retryDelayMillis, json);
            }
            case "anthropic" -> {
                if (isBlank(anthropicKey)) {
                    yield new NotConfiguredLlmClient("ANTHROPIC_API_KEY가 없습니다");
                }
                yield new AnthropicLlmClient(isBlank(anthropicBaseUrl) ? "https://api.anthropic.com" : anthropicBaseUrl.strip(),
                        isBlank(model) ? ANTHROPIC_DEFAULT_MODEL : model.strip(), anthropicKey, timeoutSeconds, maxTokens,
                        retryDelayMillis, json);
            }
            default -> new NotConfiguredLlmClient("알 수 없는 SENSING_LLM_PROVIDER: " + provider.strip());
        };
        if (client instanceof NotConfiguredLlmClient nc) {
            log.info("chat LLM not configured{}", nc.reason() == null ? "" : ": " + nc.reason());
        } else {
            log.info("chat LLM: {} (timeout {}s)", client.describe(), timeoutSeconds);
        }
        return client;
    }

    /** 워커는 SENSING_LLM_TIMEOUT을 실수로 읽는다 ("120", "90.5"). */
    static int seconds(String value) {
        try {
            return Math.max(1, (int) Math.ceil(Double.parseDouble(value.strip())));
        } catch (RuntimeException e) {
            return 60;
        }
    }

    private static boolean isBlank(String s) {
        return s == null || s.isBlank();
    }
}
