-- LLM 사용량 장부와 "쉬는 중" 기록. 챗봇(백엔드)·자동 요약(워커 enrich)·등록 레시피(워커 recipe)가 같은 모델·같은 키의
-- 무료 한도(모델별·프로젝트별 하루 요청 수, 예: Gemini Flash-Lite 하루 500번)를 나눠 쓴다. 2026-10-19 워커 2대가 요약으로
-- 하루 한도를 다 써 챗봇이 막혔고, 워커는 한도 초과 뒤에도 루프마다 다시 불러 429를 1,293번 받았다 — docs/decisions.md.
-- (V18은 다른 작업이 쓰고 있어 V19를 쓴다)

-- 날짜·모델·용도별 호출 수와 토큰. 백엔드 io.sensinghub.llm.LlmLedger와 워커 sensing_worker.llm_budget.LlmLedger가
-- 호출 전에 calls를 원자적으로 하나 늘리고(하루 상한이 있는 용도는 상한 미만일 때만), 호출 뒤 결과를 적는다.
CREATE TABLE llm_usage (
    quota_day     DATE         NOT NULL,           -- 쿼터 기준 시간대(SENSING_LLM_QUOTA_TZ, 기본 America/Los_Angeles)의 날짜. DB 시계 기준
    model         VARCHAR(200) NOT NULL,           -- SENSING_LLM_MODEL(챗봇은 SENSING_CHAT_LLM_MODEL) 값 그대로
    purpose       VARCHAR(20)  NOT NULL,           -- chat | enrich | recipe | embed
    calls         INT          NOT NULL DEFAULT 0, -- 제공자에 보낸 요청 수. 429로 거절된 요청은 빼서 rate_limited로 옮긴다
    rate_limited  INT          NOT NULL DEFAULT 0, -- 429(호출 한도) 응답 수
    errors        INT          NOT NULL DEFAULT 0, -- 429가 아닌 실패 (연결·5xx·형식). calls에도 들어 있다
    input_tokens  BIGINT       NOT NULL DEFAULT 0,
    output_tokens BIGINT       NOT NULL DEFAULT 0,
    daily_cap     INT,                             -- 이 용도에 건 하루 상한 (enrich: SENSING_ENRICH_DAILY_CALLS). 없으면 NULL
    updated_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (quota_day, model, purpose)
);

-- 모델별 "쉬는 중": 429를 받으면 응답의 quotaId·retryDelay(또는 retry-after 헤더)를 읽어 이 시각까지 그 모델을 부르지 않는다.
-- 하루 한도(quotaId에 PerDay)면 쿼터 기준 시간대의 다음 자정과 retryDelay 중 늦은 쪽, 그 밖(분당 한도 등)이면 retryDelay(없으면 60초).
-- 워커는 이 시각까지 요약 호출을 하지 않고, 챗봇은 LLM을 부르지 않고 "언제 풀리는지"를 바로 답한다.
CREATE TABLE llm_cooldown (
    model         VARCHAR(200) PRIMARY KEY,
    until_at      TIMESTAMPTZ  NOT NULL,
    reason        VARCHAR(20)  NOT NULL,           -- DAILY(하루 한도) | MINUTE(분당 한도) | RATE_LIMITED(그 밖의 429)
    quota_id      VARCHAR(200),                    -- 제공자가 준 한도 이름 (Gemini quotaId)
    retry_seconds INT,                             -- 제공자가 알려 준 대기 시간 (retryDelay·retry-after)
    purpose       VARCHAR(20),                     -- 한도를 만난 용도
    detail        TEXT,                            -- 429 응답 본문 앞부분 (키 없음)
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT now()
);
