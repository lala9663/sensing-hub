-- M2: 공개 API로 받는 소스의 자격 증명. 키는 사람이 발급하고, 시스템은 검증·암호화 보관·만료 감시만 한다.
-- 비밀은 pgcrypto 대칭키(pgp_sym_encrypt)로 저장하며, 키는 환경 변수 SENSING_SECRET_KEY다
-- (백엔드 sensing.secret-key, 워커 Settings.secret_key). 백엔드가 넣고(pgp_sym_encrypt) 워커가 읽는다(pgp_sym_decrypt).
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE api_credential (
    id                BIGSERIAL PRIMARY KEY,
    provider          VARCHAR(50)  NOT NULL UNIQUE,   -- 워커 apis/catalog.py의 제공자 id (예: dart)
    label             VARCHAR(200),
    secret_enc        BYTEA        NOT NULL,
    status            VARCHAR(20)  NOT NULL DEFAULT 'UNVERIFIED'
                      CHECK (status IN ('UNVERIFIED', 'VALID', 'INVALID')),
    last_verified_at  TIMESTAMPTZ,
    last_error        VARCHAR(500),
    created_by        BIGINT       REFERENCES app_user (id),
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ  NOT NULL DEFAULT now()
);
