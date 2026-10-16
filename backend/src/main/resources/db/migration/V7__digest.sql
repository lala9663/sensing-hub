-- M2: 일간 다이제스트 알림. 현업(대외협력·커뮤니케이션)은 아침에 정리된 보고를 받아 윗선에 전달한다.
-- 사용자가 구독(어떤 감시·토픽·소스를 몇 시에 어디로)을 등록하면, 매일 그 시각(KST)에
-- "지난 다이제스트 이후(last_sent_at, collected_at 기준) 새로 수집된 문서"를 감시별·토픽별로 묶어 메일이나 웹훅으로 보낸다.
CREATE TABLE digest_subscription (
    id            BIGSERIAL    PRIMARY KEY,
    user_id       BIGINT       NOT NULL REFERENCES app_user (id),
    name          VARCHAR(200) NOT NULL,
    channel       VARCHAR(20)  NOT NULL CHECK (channel IN ('EMAIL', 'WEBHOOK')),
    target        VARCHAR(500) NOT NULL,              -- 메일 주소 또는 웹훅 URL (내부망 주소는 등록 시 거부)
    hour_kst      INT          NOT NULL DEFAULT 8 CHECK (hour_kst BETWEEN 0 AND 23),
    -- {"watch_ids":[...],"source_ids":[...],"topics":[...]} 세 목록 중 하나라도 맞으면 포함, 모두 비면 전체
    filters       JSONB        NOT NULL DEFAULT '{}'::jsonb,
    enabled       BOOLEAN      NOT NULL DEFAULT TRUE,
    last_sent_at  TIMESTAMPTZ,                        -- 마지막 다이제스트 기간의 끝 (EMPTY도 갱신, FAILED는 그대로 두어 재시도)
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ  NOT NULL DEFAULT now()
);
CREATE INDEX idx_digest_subscription_user ON digest_subscription (user_id, id);
CREATE INDEX idx_digest_subscription_due ON digest_subscription (hour_kst) WHERE enabled;

-- 보낸(또는 보내려 한) 기록. body_text에 보낸 본문을 남겨 화면·감사에서 다시 볼 수 있게 한다.
CREATE TABLE digest_run (
    id               BIGSERIAL   PRIMARY KEY,
    subscription_id  BIGINT      NOT NULL REFERENCES digest_subscription (id) ON DELETE CASCADE,
    period_from      TIMESTAMPTZ NOT NULL,
    period_to        TIMESTAMPTZ NOT NULL,
    document_count   INT         NOT NULL DEFAULT 0,
    status           VARCHAR(20) NOT NULL CHECK (status IN ('SENT', 'EMPTY', 'FAILED')),
    error            VARCHAR(500),
    body_text        TEXT,
    sent_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_digest_run_subscription ON digest_run (subscription_id, id DESC);
