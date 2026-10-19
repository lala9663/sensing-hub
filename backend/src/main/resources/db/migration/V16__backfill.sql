-- 과거 기사 채우기(백필): 등록 전에 나온 기사를 기간(date_from~date_to)을 정해 천천히 가져온다.
-- 관리자·등록 담당자가 요청(backfill)을 만들고, 큐가 빈 워커가 crawl_job(BACKFILL) 한 묶음씩 만들어 진행한다.
-- 한 묶음은 요청 100건 안팎이고, 진행 위치는 cursor(사이트맵 대기열·목록 쪽 번호)와 backfill_url(찾은 주소별 상태)에 남는다.
-- (V14는 챗봇 1단계, V15는 챗봇 2단계 브랜치의 임베딩 모델이 쓴다)

CREATE TABLE backfill (
    id                  BIGSERIAL PRIMARY KEY,
    source_id           BIGINT       NOT NULL REFERENCES source (id),
    date_from           DATE         NOT NULL,
    date_to             DATE         NOT NULL,
    status              VARCHAR(20)  NOT NULL DEFAULT 'QUEUED'
                        CHECK (status IN ('QUEUED', 'RUNNING', 'DONE', 'CANCELLED', 'FAILED')),
    -- 지금 쓰는 방법: SITEMAP(보관 사이트맵) → 모자라면 LIST(목록 페이지를 쪽 번호로 거꾸로)
    method              VARCHAR(20)  CHECK (method IN ('SITEMAP', 'LIST')),
    cursor              JSONB        NOT NULL DEFAULT '{}'::jsonb,
    found_count         INT          NOT NULL DEFAULT 0,   -- 기간 안 기사 주소로 찾은 수 (이미 있던 것 포함)
    saved_count         INT          NOT NULL DEFAULT 0,   -- 새로 저장한 문서
    skipped_count       INT          NOT NULL DEFAULT 0,   -- 이미 있던 문서 (url_hash)
    out_of_range_count  INT          NOT NULL DEFAULT 0,   -- 열어 보니 발행일이 기간 밖이거나 없는 글
    failed_count        INT          NOT NULL DEFAULT 0,   -- 열지 못한 글 (404·robots 등)
    request_count       INT          NOT NULL DEFAULT 0,   -- 보낸 요청 (사이트맵·목록·기사·robots)
    chunk_count         INT          NOT NULL DEFAULT 0,   -- 실행한 묶음 수
    message             VARCHAR(500),
    requested_by        BIGINT       REFERENCES app_user (id),
    cancelled_by        BIGINT       REFERENCES app_user (id),
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),
    started_at          TIMESTAMPTZ,
    finished_at         TIMESTAMPTZ,
    updated_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CHECK (date_from <= date_to)
);
-- 소스당 진행 중인 채우기는 하나
CREATE UNIQUE INDEX uq_backfill_open ON backfill (source_id) WHERE status IN ('QUEUED', 'RUNNING');

-- 찾은 기사 주소. 묶음마다 PENDING을 찾은 순서(seq)대로 연다
CREATE TABLE backfill_url (
    backfill_id  BIGINT        NOT NULL REFERENCES backfill (id) ON DELETE CASCADE,
    url_hash     CHAR(64)      NOT NULL,
    url          VARCHAR(2000) NOT NULL,
    date_hint    VARCHAR(64),                -- 사이트맵·목록에 보인 날짜 (상세에 날짜가 없을 때 씀)
    kind         VARCHAR(100),               -- 기사 종류 이름 (설정 article_patterns가 둘 이상일 때)
    status       VARCHAR(20)   NOT NULL DEFAULT 'PENDING'
                 CHECK (status IN ('PENDING', 'SAVED', 'SKIPPED', 'OUT_OF_RANGE', 'FAILED')),
    seq          BIGSERIAL,
    done_at      TIMESTAMPTZ,
    PRIMARY KEY (backfill_id, url_hash)
);
CREATE INDEX idx_backfill_url_pending ON backfill_url (backfill_id, seq) WHERE status = 'PENDING';

-- 어느 채우기의 묶음인지 (BACKFILL 작업만)
ALTER TABLE crawl_job ADD COLUMN backfill_id BIGINT REFERENCES backfill (id);
