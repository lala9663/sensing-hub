-- M3: 사이트 일괄 등록과 대량 소스 운영.
-- 현업의 핵심 요구는 "아주 많은 곳에서 기사·이슈를 긁어 와 모으는 것"이라, 수십~수백 곳을 한 번에 넣고 그 묶음의 진행을
-- 한눈에 보며, 소스가 수백 개가 되어도 워커가 감당하도록 수집 주기를 자동으로 조정한다.

-- 일괄 등록 묶음 id (POST /api/v1/registrations/bulk). 한 건씩 등록한 요청은 NULL.
ALTER TABLE registration_request ADD COLUMN batch_id VARCHAR(40);
CREATE INDEX idx_registration_batch ON registration_request (batch_id) WHERE batch_id IS NOT NULL;

-- 일괄 등록에서 주소 형식이 틀린 줄도 묶음 안에 남겨 "몇 건이 왜 안 됐는지"를 같은 화면에서 본다.
ALTER TABLE registration_request DROP CONSTRAINT registration_request_status_check;
ALTER TABLE registration_request ADD CONSTRAINT registration_request_status_check
    CHECK (status IN ('RECEIVED', 'AWAITING_APPROVAL', 'PROCESSING', 'COMPLETED', 'DUPLICATE', 'REJECTED', 'REVIEW', 'INVALID'));

-- 주기 자동 조정(워커 SENSING_ADAPTIVE_INTERVAL): 최근 10회 실행의 새 문서 평균을 보고 interval_minutes를 늘리거나(최대 1440) 기본값으로 줄인다.
-- 사람이 PUT /api/v1/sources/{id}로 주기를 정하면 interval_locked = TRUE가 되어 자동 조정이 그 값을 덮지 않는다.
ALTER TABLE source ADD COLUMN interval_locked BOOLEAN NOT NULL DEFAULT FALSE;

-- 실행 한 건에 붙는 짧은 메모 (예: 주기 자동 조정 60→120분). 워커가 쓰고 소스 상세에 보인다.
ALTER TABLE crawl_job ADD COLUMN note VARCHAR(200);
