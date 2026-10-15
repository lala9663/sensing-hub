-- sensing-hub 초기 스키마
-- Java(Spring Boot)가 Flyway로 관리한다. Python 워커는 같은 테이블을 읽고 쓴다.

CREATE EXTENSION IF NOT EXISTS vector;

-- 사용자·권한 -------------------------------------------------------------
CREATE TABLE app_user (
    id           BIGSERIAL PRIMARY KEY,
    login_id     VARCHAR(100) NOT NULL UNIQUE,
    sso_subject  VARCHAR(200) UNIQUE,
    name         VARCHAR(100) NOT NULL,
    dept         VARCHAR(200),
    grade        VARCHAR(50),
    role         VARCHAR(20)  NOT NULL DEFAULT 'VIEWER'
                 CHECK (role IN ('VIEWER', 'REGISTRAR', 'ADMIN')),
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- 사이트·소스 -------------------------------------------------------------
CREATE TABLE site (
    id             BIGSERIAL PRIMARY KEY,
    domain         VARCHAR(255) NOT NULL UNIQUE,   -- 중복 판정 키 (www. 제거, 소문자)
    base_url       VARCHAR(500) NOT NULL,          -- 실제 접속 주소 (https://www.example.com)
    name           VARCHAR(200),
    site_type      VARCHAR(20)  NOT NULL DEFAULT 'UNKNOWN'
                   CHECK (site_type IN ('NEWS', 'PUBLIC', 'ETC', 'UNKNOWN')),
    robots_status  VARCHAR(20)  NOT NULL DEFAULT 'UNKNOWN'
                   CHECK (robots_status IN ('ALLOWED', 'PARTIAL', 'DISALLOWED', 'MISSING', 'UNKNOWN')),
    ai_restricted  BOOLEAN      NOT NULL DEFAULT FALSE,
    risk_flags     JSONB        NOT NULL DEFAULT '{}'::jsonb,
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE TABLE source (
    id                       BIGSERIAL PRIMARY KEY,
    site_id                  BIGINT      NOT NULL REFERENCES site (id),
    name                     VARCHAR(200),
    method                   VARCHAR(20) NOT NULL DEFAULT 'AUTO'
                             CHECK (method IN ('AUTO', 'RSS', 'SITEMAP', 'STATIC', 'DYNAMIC', 'API')),
    status                   VARCHAR(20) NOT NULL DEFAULT 'PENDING'
                             CHECK (status IN ('PENDING', 'REVIEW', 'ACTIVE', 'PAUSED', 'BROKEN', 'BLOCKED', 'EXCLUDED')),
    status_reason            VARCHAR(100),
    active_config_version_id BIGINT,
    interval_minutes         INT         NOT NULL DEFAULT 60,
    next_run_at              TIMESTAMPTZ,
    last_success_at          TIMESTAMPTZ,
    consecutive_failures     INT         NOT NULL DEFAULT 0,
    requested_by             BIGINT      REFERENCES app_user (id),
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_source_due ON source (next_run_at) WHERE status = 'ACTIVE';

-- 설정은 덮어쓰지 않고 버전을 쌓는다. source.active_config_version_id가 현재 버전을 가리킨다.
CREATE TABLE source_config_version (
    id           BIGSERIAL PRIMARY KEY,
    source_id    BIGINT      NOT NULL REFERENCES source (id),
    version      INT         NOT NULL,
    config       JSONB       NOT NULL,
    status       VARCHAR(20) NOT NULL DEFAULT 'DRAFT'
                 CHECK (status IN ('DRAFT', 'ACTIVE', 'RETIRED')),
    change_note  VARCHAR(500),
    created_by   BIGINT      REFERENCES app_user (id),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source_id, version)
);
ALTER TABLE source
    ADD CONSTRAINT fk_source_active_config
    FOREIGN KEY (active_config_version_id) REFERENCES source_config_version (id);

-- 등록 요청: 사용자는 URL만 넣고 나머지는 자동 처리된다.
CREATE TABLE registration_request (
    id                 BIGSERIAL PRIMARY KEY,
    user_id            BIGINT       NOT NULL REFERENCES app_user (id),
    input_url          VARCHAR(2000) NOT NULL,
    normalized_domain  VARCHAR(255) NOT NULL,
    channel            VARCHAR(10)  NOT NULL DEFAULT 'WEB' CHECK (channel IN ('WEB', 'CHAT')),
    status             VARCHAR(20)  NOT NULL DEFAULT 'RECEIVED'
                       CHECK (status IN ('RECEIVED', 'AWAITING_APPROVAL', 'PROCESSING', 'COMPLETED',
                                         'DUPLICATE', 'REJECTED', 'REVIEW')),
    step               VARCHAR(30),
    result_message     VARCHAR(500),
    source_id          BIGINT       REFERENCES source (id),
    created_at         TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ  NOT NULL DEFAULT now()
);
CREATE INDEX idx_registration_user ON registration_request (user_id, created_at DESC);

-- 작업 큐 (Java가 넣고 Python 워커가 FOR UPDATE SKIP LOCKED로 가져간다) ------------
CREATE TABLE crawl_job (
    id                 BIGSERIAL PRIMARY KEY,
    source_id          BIGINT      NOT NULL REFERENCES source (id),
    job_type           VARCHAR(20) NOT NULL DEFAULT 'CRAWL'
                       CHECK (job_type IN ('ONBOARD', 'CRAWL', 'BACKFILL', 'INDEX')),
    config_version_id  BIGINT      REFERENCES source_config_version (id),
    trigger            VARCHAR(20) NOT NULL DEFAULT 'SCHEDULE'
                       CHECK (trigger IN ('SCHEDULE', 'MANUAL', 'CHAT', 'REGISTRATION')),
    status             VARCHAR(20) NOT NULL DEFAULT 'QUEUED'
                       CHECK (status IN ('QUEUED', 'RUNNING', 'SUCCESS', 'PARTIAL', 'FAILED')),
    locked_by          VARCHAR(100),
    lease_until        TIMESTAMPTZ,
    attempt            INT         NOT NULL DEFAULT 0,
    found_count        INT         NOT NULL DEFAULT 0,
    new_count          INT         NOT NULL DEFAULT 0,
    error_count        INT         NOT NULL DEFAULT 0,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at         TIMESTAMPTZ,
    finished_at        TIMESTAMPTZ
);
CREATE INDEX idx_crawl_job_queue ON crawl_job (created_at) WHERE status = 'QUEUED';
-- 소스당 실행 중(대기 포함) 작업은 1개만
CREATE UNIQUE INDEX uq_crawl_job_one_open ON crawl_job (source_id) WHERE status IN ('QUEUED', 'RUNNING');

CREATE TABLE crawl_error (
    id             BIGSERIAL PRIMARY KEY,
    job_id         BIGINT       NOT NULL REFERENCES crawl_job (id),
    url            VARCHAR(2000),
    stage          VARCHAR(20)  NOT NULL
                   CHECK (stage IN ('PROBE', 'DISCOVERY', 'LIST', 'DETAIL', 'ATTACHMENT', 'EXTRACT', 'INDEX')),
    code           VARCHAR(40)  NOT NULL,
    http_status    INT,
    message        VARCHAR(1000),
    retryable      BOOLEAN      NOT NULL DEFAULT FALSE,
    evidence_path  VARCHAR(500),
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- 수집 문서 ----------------------------------------------------------------
CREATE TABLE document (
    id             BIGSERIAL PRIMARY KEY,
    source_id      BIGINT       NOT NULL REFERENCES source (id),
    url            VARCHAR(2000) NOT NULL,
    url_hash       CHAR(64)     NOT NULL UNIQUE,
    title          VARCHAR(1000),
    author         VARCHAR(200),
    section        VARCHAR(200),
    published_at   TIMESTAMPTZ,
    body_text      TEXT,
    content_hash   CHAR(64),
    version        INT          NOT NULL DEFAULT 1,
    language       VARCHAR(10),
    origin_status  VARCHAR(10)  NOT NULL DEFAULT 'LIVE' CHECK (origin_status IN ('LIVE', 'GONE')),
    collected_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    indexed_at     TIMESTAMPTZ
);
CREATE INDEX idx_document_source_published ON document (source_id, published_at DESC);
CREATE INDEX idx_document_not_indexed ON document (collected_at) WHERE indexed_at IS NULL;

CREATE TABLE document_version (
    id            BIGSERIAL PRIMARY KEY,
    document_id   BIGINT      NOT NULL REFERENCES document (id),
    version       INT         NOT NULL,
    title         VARCHAR(1000),
    body_text     TEXT,
    content_hash  CHAR(64),
    job_id        BIGINT      REFERENCES crawl_job (id),
    collected_at  TIMESTAMPTZ NOT NULL,
    UNIQUE (document_id, version)
);

CREATE TABLE attachment (
    id              BIGSERIAL PRIMARY KEY,
    document_id     BIGINT       NOT NULL REFERENCES document (id),
    url             VARCHAR(2000) NOT NULL,
    file_name       VARCHAR(500),
    mime_type       VARCHAR(100),
    size_bytes      BIGINT,
    sha256          CHAR(64),
    storage_path    VARCHAR(500),
    extract_status  VARCHAR(20)  NOT NULL DEFAULT 'PENDING'
                    CHECK (extract_status IN ('PENDING', 'DONE', 'FAILED', 'SKIPPED')),
    extracted_text  TEXT,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    UNIQUE (document_id, url)
);

-- RAG 검색 단위. index_version으로 임베딩 모델·청크 설정을 구분한다.
CREATE TABLE chunk (
    id             BIGSERIAL PRIMARY KEY,
    document_id    BIGINT      NOT NULL REFERENCES document (id) ON DELETE CASCADE,
    attachment_id  BIGINT      REFERENCES attachment (id) ON DELETE CASCADE,
    seq            INT         NOT NULL,
    text           TEXT        NOT NULL,
    tokens         TSVECTOR,
    embedding      VECTOR(1024),
    index_version  VARCHAR(50) NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_chunk_tokens ON chunk USING GIN (tokens);
CREATE INDEX idx_chunk_embedding ON chunk USING hnsw (embedding vector_cosine_ops);

-- 챗봇 ---------------------------------------------------------------------
CREATE TABLE chat_session (
    id               BIGSERIAL PRIMARY KEY,
    user_id          BIGINT       NOT NULL REFERENCES app_user (id),
    title            VARCHAR(200),
    summary          TEXT,
    status           VARCHAR(20)  NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'ARCHIVED', 'DELETED')),
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    last_message_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);
CREATE INDEX idx_chat_session_user ON chat_session (user_id, last_message_at DESC);

CREATE TABLE chat_message (
    id          BIGSERIAL PRIMARY KEY,
    session_id  BIGINT      NOT NULL REFERENCES chat_session (id),
    seq         INT         NOT NULL,
    role        VARCHAR(10) NOT NULL CHECK (role IN ('USER', 'ASSISTANT', 'TOOL')),
    content     TEXT        NOT NULL,
    tool_calls  JSONB,
    meta        JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (session_id, seq)
);

CREATE TABLE chat_citation (
    id                BIGSERIAL PRIMARY KEY,
    message_id        BIGINT        NOT NULL REFERENCES chat_message (id),
    ref_no            INT           NOT NULL,
    document_id       BIGINT        REFERENCES document (id),
    document_version  INT,
    chunk_id          BIGINT,
    title_snapshot    VARCHAR(1000),
    url_snapshot      VARCHAR(2000),
    score             REAL
);

CREATE TABLE chat_feedback (
    id          BIGSERIAL PRIMARY KEY,
    message_id  BIGINT      NOT NULL REFERENCES chat_message (id),
    user_id     BIGINT      NOT NULL REFERENCES app_user (id),
    rating      VARCHAR(10) NOT NULL CHECK (rating IN ('GOOD', 'BAD')),
    comment     VARCHAR(1000),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (message_id, user_id)
);

-- 감사 로그 -----------------------------------------------------------------
CREATE TABLE audit_log (
    id           BIGSERIAL PRIMARY KEY,
    actor_id     BIGINT       REFERENCES app_user (id),
    action       VARCHAR(50)  NOT NULL,
    target_type  VARCHAR(50)  NOT NULL,
    target_id    BIGINT,
    detail       JSONB,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- PoC 기본 계정 (SSO 연동 전)
INSERT INTO app_user (login_id, name, role) VALUES
    ('admin', '운영 관리자', 'ADMIN'),
    ('registrar', '등록 담당자', 'REGISTRAR'),
    ('viewer', '일반 사용자', 'VIEWER');
