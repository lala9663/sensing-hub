-- 워커 생존 신호. 운영 지표의 워커 수를 crawl_job.locked_by(최근 5분 안에 작업을 잡은 워커)로 세면 큐가 빈 동안에는
-- 워커가 살아 있어도 0으로 보인다. 워커가 30초(SENSING_HEARTBEAT_SECONDS)마다 이 행의 last_seen_at을 갱신하고,
-- /api/v1/ops/overview는 최근 2분 안에 신호를 남긴 워커를 센다.
-- (V11은 다른 작업이 쓰고 있어 V12를 쓴다)
CREATE TABLE worker_heartbeat (
    worker_id      VARCHAR(100) PRIMARY KEY,               -- crawl_job.locked_by와 같은 값 (SENSING_WORKER_ID, 기본 호스트-pid)
    host           VARCHAR(255),
    started_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),    -- 이 워커 프로세스가 시작한 시각
    last_seen_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    version        VARCHAR(40),                            -- 워커 패키지 버전
    current_job_id BIGINT REFERENCES crawl_job (id) ON DELETE SET NULL  -- 처리 중인 작업 (쉬는 중이면 NULL)
);
CREATE INDEX idx_worker_heartbeat_seen ON worker_heartbeat (last_seen_at DESC);
