-- M3: 케이던시아(삼성SDS 구매 SaaS) 연동 — 목업 단계.
-- 실제 API가 아직 없으므로 우리 백엔드 안의 가짜 케이던시아(/mock/caidentia/api)와 연동 형태(공급사 마스터 동기화 → company upsert,
-- 회사 신호 → 리스크 이벤트 전달)를 먼저 만든다. 실제 시스템이 나오면 base-url과 필드 매핑만 바꾼다.

-- 동기화 실행 기록 한 건 = 공급사 마스터를 한 번 받아 회사 마스터에 반영한 결과. 실패(인증·연결)는 FAILED와 error로 남고 다음 스케줄에 다시 돈다.
CREATE TABLE caidentia_sync_run (
    id           BIGSERIAL    PRIMARY KEY,
    started_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    finished_at  TIMESTAMPTZ,
    status       VARCHAR(20)  NOT NULL CHECK (status IN ('RUNNING', 'SUCCESS', 'FAILED')),
    trigger      VARCHAR(20)  NOT NULL DEFAULT 'SCHEDULE' CHECK (trigger IN ('SCHEDULE', 'MANUAL')),
    fetched      INT          NOT NULL DEFAULT 0,   -- 케이던시아가 준 공급사 수
    created      INT          NOT NULL DEFAULT 0,   -- 새로 만든 회사
    updated      INT          NOT NULL DEFAULT 0,   -- 바뀐 필드가 있어 갱신한 회사 (보관 → 복구 포함)
    archived     INT          NOT NULL DEFAULT 0,   -- INACTIVE 공급사라 보관한 회사
    unchanged    INT          NOT NULL DEFAULT 0,   -- 그대로인 회사
    error        VARCHAR(500)
);
CREATE INDEX idx_caidentia_sync_run_started ON caidentia_sync_run (started_at DESC);

-- 회사 신호를 케이던시아 리스크 이벤트로 전달한 시각. HIGH·MEDIUM만 보내고, 실패하면 NULL로 남아 다음 틱에 다시 보낸다.
ALTER TABLE company_signal ADD COLUMN pushed_at TIMESTAMPTZ;
CREATE INDEX idx_company_signal_unpushed ON company_signal (id) WHERE pushed_at IS NULL AND severity IN ('HIGH', 'MEDIUM');

-- 스케줄러가 동기화할 때의 행위자 (감사 로그·created_by). 화면 사용자 선택에는 넣지 않는다.
INSERT INTO app_user (login_id, name, role) VALUES ('system', '시스템 연동', 'ADMIN') ON CONFLICT (login_id) DO NOTHING;
