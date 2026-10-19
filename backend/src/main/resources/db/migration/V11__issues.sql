-- M5 기반: 이슈 묶기. 여러 언론사가 같은 사건을 보도한 기사를 하나의 이슈로 묶는다 (워커 issues.py, LLM·임베딩 없이 제목 낱말 겹침).
-- 전문 검색 색인은 V1의 chunk(tokens tsvector, GIN)를 그대로 쓴다: 워커가 미리 낱말을 뽑아 to_tsvector('simple', ...)로 넣고
-- document.indexed_at을 채운다. 본문이 바뀌면 upsert_document가 indexed_at을 NULL로 되돌려 다시 색인된다.

CREATE TABLE issue (
    id                         BIGSERIAL     PRIMARY KEY,
    title                      VARCHAR(1000) NOT NULL DEFAULT '',     -- 대표 제목 (서명과 가장 많이 겹치는 문서, 같으면 먼저 발행된 문서)
    representative_document_id BIGINT        REFERENCES document (id) ON DELETE SET NULL,
    first_seen_at              TIMESTAMPTZ   NOT NULL,                -- 소속 문서 시각(발행일, 없으면 수집 시각)의 최소
    last_seen_at               TIMESTAMPTZ   NOT NULL,                -- 〃 최대. 이슈 창(기본 72시간) 비교·닫기 기준
    doc_count                  INT           NOT NULL DEFAULT 0,
    source_count               INT           NOT NULL DEFAULT 0,      -- 서로 다른 소스(언론사) 수
    top_terms                  JSONB         NOT NULL DEFAULT '[]'::jsonb,  -- 서명: 소속 문서 제목 낱말 빈도순 최대 15개
    status                     VARCHAR(10)   NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN', 'CLOSED')),
    created_at                 TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at                 TIMESTAMPTZ   NOT NULL DEFAULT now()
);
CREATE INDEX idx_issue_last_seen ON issue (last_seen_at DESC);
-- 새 문서와 비교할 후보 이슈를 낱말 하나라도 겹치는 것으로 좁힌다 (top_terms ?| 낱말들)
CREATE INDEX idx_issue_top_terms ON issue USING GIN (top_terms);

-- 문서는 이슈 하나에만 속한다
CREATE TABLE issue_document (
    document_id BIGINT      PRIMARY KEY REFERENCES document (id) ON DELETE CASCADE,
    issue_id    BIGINT      NOT NULL REFERENCES issue (id) ON DELETE CASCADE,
    similarity  REAL        NOT NULL,                     -- 붙일 때의 겹침 계수 (이슈를 만든 문서는 1)
    terms       JSONB       NOT NULL DEFAULT '[]'::jsonb, -- 비교에 쓴 제목 낱말 (서명 재계산용)
    added_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_issue_document_issue ON issue_document (issue_id);
