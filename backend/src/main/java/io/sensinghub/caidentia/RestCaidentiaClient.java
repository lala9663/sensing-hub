package io.sensinghub.caidentia;

import java.time.Duration;
import java.time.OffsetDateTime;
import java.util.List;
import java.util.Map;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.core.ParameterizedTypeReference;
import org.springframework.http.MediaType;
import org.springframework.http.client.SimpleClientHttpRequestFactory;
import org.springframework.stereotype.Component;
import org.springframework.web.client.RestClient;
import org.springframework.web.client.RestClientException;
import org.springframework.web.client.RestClientResponseException;
import org.springframework.web.util.UriComponentsBuilder;

/**
 * 케이던시아 REST 호출 ({@link CaidentiaClient}). 헤더 {@code X-Caidentia-Key}, 제한 시간 10초, 2xx가 아니면 {@link CaidentiaException}.
 * 주소는 {@link CaidentiaSettings#baseUrl()}이며 호출마다 읽는다 (테스트의 임의 포트는 서버가 뜬 뒤에야 정해진다).
 */
@Component
public class RestCaidentiaClient implements CaidentiaClient {

    private static final Logger log = LoggerFactory.getLogger(RestCaidentiaClient.class);
    private static final ParameterizedTypeReference<List<Supplier>> SUPPLIERS = new ParameterizedTypeReference<>() { };
    private static final ParameterizedTypeReference<List<Map<String, Object>>> EVENTS = new ParameterizedTypeReference<>() { };

    private final CaidentiaSettings settings;
    private final RestClient http;

    /** Boot이 만들어 주는 빌더를 쓴다: 앱과 같은 ObjectMapper라 시각이 ISO 문자열로 나간다 (기본 빌더는 epoch 숫자로 쓴다). */
    public RestCaidentiaClient(CaidentiaSettings settings, RestClient.Builder builder,
                               @Value("${sensing.caidentia.timeout-seconds:10}") int timeoutSeconds) {
        this.settings = settings;
        SimpleClientHttpRequestFactory factory = new SimpleClientHttpRequestFactory();
        factory.setConnectTimeout(Duration.ofSeconds(timeoutSeconds));
        factory.setReadTimeout(Duration.ofSeconds(timeoutSeconds));
        this.http = builder.requestFactory(factory).build();
    }

    @Override
    public List<Supplier> fetchSuppliers(OffsetDateTime updatedSince) {
        UriComponentsBuilder uri = UriComponentsBuilder.fromUriString(settings.baseUrl() + "/suppliers");
        if (updatedSince != null) {
            uri.queryParam("updatedSince", updatedSince.toString());
        }
        try {
            List<Supplier> list = http.get().uri(uri.build(true).toUri())
                    .header(KEY_HEADER, settings.apiKey())
                    .retrieve().body(SUPPLIERS);
            return list == null ? List.of() : list;
        } catch (RestClientResponseException e) {
            throw failed("공급사 조회", e);
        } catch (RestClientException e) {
            throw failed("공급사 조회", e);
        }
    }

    @Override
    public void pushRiskEvent(RiskEvent event) {
        try {
            http.post().uri(settings.baseUrl() + "/risk-events")
                    .header(KEY_HEADER, settings.apiKey())
                    .contentType(MediaType.APPLICATION_JSON)
                    .body(event)
                    .retrieve().toBodilessEntity();
        } catch (RestClientResponseException e) {
            throw failed("리스크 이벤트 전달", e);
        } catch (RestClientException e) {
            throw failed("리스크 이벤트 전달", e);
        }
    }

    /** 데모 전용: 가짜 케이던시아가 받은 이벤트를 그대로 읽는다 (실제 케이던시아에는 없는 API). */
    public List<Map<String, Object>> fetchReceivedEvents() {
        try {
            List<Map<String, Object>> list = http.get().uri(settings.baseUrl() + "/risk-events")
                    .header(KEY_HEADER, settings.apiKey())
                    .retrieve().body(EVENTS);
            return list == null ? List.of() : list;
        } catch (RestClientResponseException e) {
            throw failed("받은 이벤트 조회", e);
        } catch (RestClientException e) {
            throw failed("받은 이벤트 조회", e);
        }
    }

    static final String KEY_HEADER = "X-Caidentia-Key";

    private CaidentiaException failed(String what, RestClientResponseException e) {
        int status = e.getStatusCode().value();
        log.warn("caidentia {} answered {} ({})", what, status, settings.baseUrl());
        String hint = status == 401 || status == 403 ? " — API 키를 확인하세요 (SENSING_CAIDENTIA_API_KEY)" : "";
        return new CaidentiaException(what + " 실패: 케이던시아 응답 " + status + hint);
    }

    private CaidentiaException failed(String what, RestClientException e) {
        log.warn("caidentia {} failed: {} ({})", what, e.getMessage(), settings.baseUrl());
        String m = e.getMessage() == null ? e.getClass().getSimpleName() : e.getMessage();
        return new CaidentiaException(what + " 실패: " + (m.length() > 300 ? m.substring(0, 300) : m));
    }
}
