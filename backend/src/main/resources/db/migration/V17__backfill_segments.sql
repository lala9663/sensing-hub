-- 과거 기사 채우기를 기간 묶음 단위로: 요청 기간을 묶음(기본 2개월, 보관 사이트맵이 월·두 달·석 달 단위면 그 단위)으로 나눠
-- 가장 최근 묶음부터 과거로 채운다. 묶음마다 상태와 수를 남겨 끊겨도 남은 묶음부터 잇고, "두 달 더"로 기간을 뒤로 늘린다.

-- 묶음 단위: unit_months개월, month_index(= 연*12 + 월-1) % unit_months = unit_offset인 달에서 묶음이 시작한다 (동아일보 2/0 → 1~2월, 3~4월 …)
ALTER TABLE backfill ADD COLUMN unit_months INT NOT NULL DEFAULT 2 CHECK (unit_months BETWEEN 1 AND 12),
                     ADD COLUMN unit_offset INT NOT NULL DEFAULT 0;

CREATE TABLE backfill_segment (
    id                  BIGSERIAL PRIMARY KEY,
    backfill_id         BIGINT       NOT NULL REFERENCES backfill (id) ON DELETE CASCADE,
    seq                 INT          NOT NULL,              -- 0이 가장 최근 묶음, 클수록 과거 (이 순서로 채운다)
    date_from           DATE         NOT NULL,
    date_to             DATE         NOT NULL,
    status              VARCHAR(20)  NOT NULL DEFAULT 'QUEUED'
                        CHECK (status IN ('QUEUED', 'RUNNING', 'DONE', 'FAILED')),
    method              VARCHAR(30),                         -- SITEMAP | LIST | SITEMAP+LIST
    found_count         INT          NOT NULL DEFAULT 0,
    saved_count         INT          NOT NULL DEFAULT 0,
    skipped_count       INT          NOT NULL DEFAULT 0,
    out_of_range_count  INT          NOT NULL DEFAULT 0,
    failed_count        INT          NOT NULL DEFAULT 0,
    message             VARCHAR(500),
    started_at          TIMESTAMPTZ,
    finished_at         TIMESTAMPTZ,
    updated_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),
    UNIQUE (backfill_id, seq),
    UNIQUE (backfill_id, date_from),
    CHECK (date_from <= date_to)
);

ALTER TABLE backfill_url ADD COLUMN segment_id BIGINT REFERENCES backfill_segment (id) ON DELETE SET NULL;
CREATE INDEX idx_backfill_url_segment_pending ON backfill_url (backfill_id, segment_id, seq) WHERE status = 'PENDING';

-- 채우기가 아는 사이트맵 파일 (robots의 Sitemap 줄과 색인의 하위). 묶음마다 필요한 것만 읽는다
CREATE TABLE backfill_sitemap (
    backfill_id  BIGINT        NOT NULL REFERENCES backfill (id) ON DELETE CASCADE,
    url_hash     CHAR(64)      NOT NULL,
    url          VARCHAR(2000) NOT NULL,
    depth        INT           NOT NULL DEFAULT 0,      -- 0: robots·설정에서 온 것
    span_from    DATE,                                  -- 파일 이름의 날짜 (donga-sitemap-202607-202608 → 07-01~08-31)
    span_to      DATE,
    lastmod      DATE,                                  -- 색인의 lastmod. 고친 날일 수 있어 "이보다 이른 글뿐"으로만 쓴다
    grp          VARCHAR(300),                          -- 번호만 다른 파일 묶음 (mbn_all_sitemap{n}.xml)
    num          INT,                                   -- 그 번호
    is_index     BOOLEAN,                               -- 읽기 전에는 모른다
    read_from    DATE,                                  -- 이 날짜 이후 글까지 묶음에 나눠 넣었다 (NULL: 아직 안 읽음, 기간을 늘리면 다시 읽는다)
    obs_from     DATE,                                  -- 읽어 보니 든 글의 날짜 범위 (주소 안 날짜·발행일 우선, 없으면 lastmod)
    obs_to       DATE,
    entries      INT,
    error        VARCHAR(200),
    read_at      TIMESTAMPTZ,
    PRIMARY KEY (backfill_id, url_hash)
);
