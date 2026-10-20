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

    /**
     * 429 응답에서 읽은 한도 ({@link LlmHttp#rateLimit}). scope는 llm_cooldown.reason과 같은 값이다.
     * @param scope        DAILY(하루 한도, Gemini quotaId에 PerDay) | MINUTE(분당 한도) | RATE_LIMITED(그 밖의 429, retry-after 등)
     * @param quotaId      제공자가 준 한도 이름 (없으면 null)
     * @param retrySeconds 제공자가 알려 준 다시 부를 수 있을 때까지의 초 (없으면 null)
     */
    public record RateLimit(String scope, String quotaId, Integer retrySeconds) {

        public static final String DAILY = "DAILY";
        public static final String MINUTE = "MINUTE";
        public static final String OTHER = "RATE_LIMITED";

        public boolean daily() {
            return DAILY.equals(scope);
        }
    }

    private final Kind kind;
    private final Integer httpStatus;
    private final String detail;
    private final RateLimit rateLimit;

    public LlmException(Kind kind, String userMessage, Integer httpStatus, String detail, RateLimit rateLimit) {
        super(userMessage);
        this.kind = kind;
        this.httpStatus = httpStatus;
        this.detail = detail;
        this.rateLimit = rateLimit;
    }

    public LlmException(Kind kind, String userMessage, Integer httpStatus, String detail) {
        this(kind, userMessage, httpStatus, detail, null);
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

    /** HTTP 429일 때만 있다. */
    public RateLimit rateLimit() {
        return rateLimit;
    }

    /**
     * 스트리밍으로 부르다 이 오류가 나면 같은 요청을 한 번에 받기로 다시 보낼 만한가 ({@link LlmClient#chatStreaming}).
     * 서버 오류·연결 끊김·스트림 형식 오류·요청 거절(스트리밍을 지원하지 않는 게이트웨이의 400)은 바꿔 볼 만하다.
     * 429·키 오류·시간 초과·미설정은 다시 불러도 같거나 한도만 더 쓰므로 그대로 둔다.
     */
    public boolean streamFallback() {
        return kind == Kind.SERVER_ERROR || kind == Kind.NETWORK || kind == Kind.BAD_REQUEST || kind == Kind.BAD_RESPONSE;
    }
}
