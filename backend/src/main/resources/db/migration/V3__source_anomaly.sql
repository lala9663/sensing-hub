-- M1: 조용한 실패 감지. 수집량 급감(ANOMALY_DROP)이 연속으로 나온 횟수.
-- 2회 연속이면 워커가 소스를 BROKEN으로 바꾼다. 정상 수집이나 설정 새 버전이 나오면 0으로 돌아간다.
ALTER TABLE source ADD COLUMN consecutive_anomalies INT NOT NULL DEFAULT 0;
