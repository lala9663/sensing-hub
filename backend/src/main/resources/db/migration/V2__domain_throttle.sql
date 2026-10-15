-- M1: 도메인 단위 요청 간격을 워커 여러 대가 공유한다.
-- 워커가 요청 직전에 next_allowed_at을 원자적으로 밀어 슬롯을 예약한다 (sensing_worker/throttle.py).
CREATE TABLE domain_throttle (
    host             VARCHAR(255) PRIMARY KEY,   -- 소문자, www. 제거
    next_allowed_at  TIMESTAMPTZ  NOT NULL
);
