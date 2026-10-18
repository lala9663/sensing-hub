package io.sensinghub.caidentia;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.web.context.WebServerInitializedEvent;
import org.springframework.context.event.EventListener;
import org.springframework.stereotype.Component;

/**
 * 케이던시아 연동 설정 ({@code sensing.caidentia.*}, 환경 변수 {@code SENSING_CAIDENTIA_*}).
 * 실제 케이던시아가 나오면 바꿀 것은 {@code base-url}(과 키)뿐이다. 기본값은 우리 백엔드 안의 가짜 케이던시아
 * ({@code http://127.0.0.1:<port>/mock/caidentia/api})이고, 포트가 0(테스트의 임의 포트)이면 서버가 뜬 뒤 실제 포트로 바꾼다.
 */
@Component
public class CaidentiaSettings {

    private final String configuredBaseUrl;
    private final String apiKey;
    private final boolean pushEnabled;
    private final String syncCron;
    private volatile int actualPort = -1;

    public CaidentiaSettings(@Value("${sensing.caidentia.base-url}") String baseUrl,
                             @Value("${sensing.caidentia.api-key:mock-key}") String apiKey,
                             @Value("${sensing.caidentia.push-enabled:true}") boolean pushEnabled,
                             @Value("${sensing.caidentia.sync-cron:0 0 6 * * *}") String syncCron) {
        this.configuredBaseUrl = baseUrl == null ? "" : baseUrl.strip().replaceAll("/+$", "");
        this.apiKey = apiKey == null ? "" : apiKey;
        this.pushEnabled = pushEnabled;
        this.syncCron = syncCron;
    }

    @EventListener
    public void onWebServerStarted(WebServerInitializedEvent event) {
        actualPort = event.getWebServer().getPort();
    }

    /** 실제로 부를 주소. 설정의 포트가 0이면 실제 포트로. */
    public String baseUrl() {
        if (actualPort > 0 && configuredBaseUrl.contains(":0/")) {
            return configuredBaseUrl.replace(":0/", ":" + actualPort + "/");
        }
        return configuredBaseUrl;
    }

    public String apiKey() {
        return apiKey;
    }

    public boolean pushEnabled() {
        return pushEnabled;
    }

    public String syncCron() {
        return syncCron;
    }

    /** 가짜 케이던시아를 보고 있는가 (주소 경로로 판단). 화면에 "목업" 표시. */
    public boolean mock() {
        return configuredBaseUrl.contains("/mock/caidentia");
    }
}
