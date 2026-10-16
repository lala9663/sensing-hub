-- M3: 회사(거래처) 레이어. 문서 중심(오늘 들어온 문서) 위에 회사 중심(거래처별 외부 신호)을 한 층 얹는다.
-- 현업이 케이던시아(구매 SaaS)를 함께 쓰므로, 고객·엔진은 그대로 두고 데이터 소스를 공급사 리스크 쪽으로 넓힌다
-- (docs/requirements-mapping.md "공급사 리스크 소스").

-- 회사 마스터. 이름·별칭은 네이버 뉴스 키워드 감시(watch_source_id)의 검색어가 되고, 사업자번호(biz_no)는 국세청 상태 조회,
-- DART 고유번호(corp_code)는 공시·재무 조회(후속)의 열쇠다. external_id는 케이던시아 등 외부 시스템의 ID.
CREATE TABLE company (
    id               BIGSERIAL    PRIMARY KEY,
    name             VARCHAR(200) NOT NULL,
    aliases          JSONB        NOT NULL DEFAULT '[]'::jsonb,   -- 별칭·영문명·약칭 (문자열 배열)
    biz_no           VARCHAR(10)  UNIQUE,                        -- 사업자등록번호, 숫자 10자리 (하이픈 없음)
    corp_code        VARCHAR(8),                                 -- DART 고유번호
    external_id      VARCHAR(100),                               -- 케이던시아 등 외부 ID
    tags             JSONB        NOT NULL DEFAULT '[]'::jsonb,   -- 예: 1차 공급사, 핵심
    watch_source_id  BIGINT       REFERENCES source (id),        -- 이 회사 이름·별칭으로 만든 키워드 감시 (kind WATCH)
    -- 국세청 사업자등록 상태 조회의 마지막 결과 {"b_stt":..., "b_stt_cd":..., "tax_type":..., "tax_type_cd":..., "end_dt":..., "checked_at":...}
    last_biz_status  JSONB,
    status           VARCHAR(20)  NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'ARCHIVED')),
    created_by       BIGINT       REFERENCES app_user (id),
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ  NOT NULL DEFAULT now()
);
CREATE INDEX idx_company_status_name ON company (status, name);
CREATE INDEX idx_company_watch_source ON company (watch_source_id);

-- 문서 → 회사 연결. matched_by: WATCH(회사 감시의 검색어에 걸림) | TAG(보강 태그가 회사명·별칭과 정확히 일치) | API(사업자번호·고유번호로 조회).
CREATE TABLE document_company (
    document_id  BIGINT       NOT NULL REFERENCES document (id) ON DELETE CASCADE,
    company_id   BIGINT       NOT NULL REFERENCES company (id) ON DELETE CASCADE,
    matched_by   VARCHAR(20)  NOT NULL CHECK (matched_by IN ('WATCH', 'TAG', 'API')),
    matched_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (document_id, company_id)
);
CREATE INDEX idx_document_company_company ON document_company (company_id, matched_at DESC);

-- 회사 단위 위험 신호. kind는 토픽과 별도의 두 번째 체계(BIZ_STATUS 휴·폐업, DEBARMENT 입찰 제한, DISCLOSURE 공시, NEWS 보도, FINANCIAL 재무).
-- fingerprint로 같은 신호(예: 같은 폐업 상태)를 매일 중복 기록하지 않는다 (unique(company_id, kind, fingerprint)).
CREATE TABLE company_signal (
    id           BIGSERIAL    PRIMARY KEY,
    company_id   BIGINT       NOT NULL REFERENCES company (id) ON DELETE CASCADE,
    kind         VARCHAR(30)  NOT NULL CHECK (kind IN ('BIZ_STATUS', 'DEBARMENT', 'DISCLOSURE', 'NEWS', 'FINANCIAL')),
    severity     VARCHAR(10)  NOT NULL CHECK (severity IN ('HIGH', 'MEDIUM', 'LOW')),
    fingerprint  VARCHAR(200) NOT NULL,
    title        VARCHAR(500) NOT NULL,
    detail       JSONB        NOT NULL DEFAULT '{}'::jsonb,
    observed_at  TIMESTAMPTZ  NOT NULL,
    document_id  BIGINT       REFERENCES document (id) ON DELETE SET NULL,
    source_id    BIGINT       REFERENCES source (id),
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    UNIQUE (company_id, kind, fingerprint)
);
CREATE INDEX idx_company_signal_company ON company_signal (company_id, observed_at DESC);
CREATE INDEX idx_company_signal_observed ON company_signal (observed_at DESC);

-- 회사 단위 점검 소스 (kind CHECK): 사이트도 키워드도 아니고 "모든 회사에 대해 주기적으로 외부 API로 상태를 확인"하는 시스템 소스.
-- 첫 항목은 국세청 사업자등록 상태 조회 (site api.odcloud.kr, config {"provider":"data_go_kr","check":"nts_status"}, 1440분).
ALTER TABLE source DROP CONSTRAINT IF EXISTS source_kind_check;
ALTER TABLE source ADD CONSTRAINT source_kind_check CHECK (kind IN ('SITE', 'WATCH', 'CHECK'));
