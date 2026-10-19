package io.sensinghub.llm;

/**
 * LLM을 부를 수 없거나 응답을 쓸 수 없다. {@link #getMessage()}는 사용자에게 그대로 보일 한국어 문장이고,
 * 원인 상세(HTTP 본문 앞부분 등)는 {@link #detail()}에 두어 로그에만 남긴다 (키 값은 어디에도 넣지 않는다).
 */
public class LlmException extends RuntimeException {

    public enum Kind {
        /** 제공자 설정이 없다 (SENSING_LLM_PROVIDER=none, 키·주소 없음) */
        NOT_CONFIGURED,
        /** HTTP 429: 분당·하루 호출 한도 (Gemini 무료 등급에서 흔하다) */
        RATE_LIMITED,
        /** HTTP 5xx·529: 제공자 서버 문제 */
        SERVER_ERROR,
        /** 응답이 제한 시간 안에 오지 않았다 */
        TIMEOUT,
        /** 연결 실패 (주소·네트워크) */
        NETWORK,
        /** HTTP 401·403: 키가 틀렸거나 권한이 없다 */
        AUTH,
        /** HTTP 400·404 등 요청 자체를 거절 (모델 이름 오타, 형식) */
        BAD_REQUEST,
        /** 응답 형식을 해석할 수 없다, 또는 모델이 답을 거절했다 */
        BAD_RESPONSE
    }

    private final Kind kind;
    private final Integer httpStatus;
    private final String detail;

    public LlmException(Kind kind, String userMessage, Integer httpStatus, String detail) {
        super(userMessage);
        this.kind = kind;
        this.httpStatus = httpStatus;
        this.detail = detail;
    }

    public LlmException(Kind kind, String userMessage) {
        this(kind, userMessage, null, null);
    }

    public Kind kind() {
        return kind;
    }

    public Integer httpStatus() {
        return httpStatus;
    }

    public String detail() {
        return detail;
    }
}
