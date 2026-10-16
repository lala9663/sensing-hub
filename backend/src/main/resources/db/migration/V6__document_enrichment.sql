-- M2: 수집 시점 요약·분류 (보강). 워커가 LLM으로 문서마다 한 줄 요약과 분류 태그를 붙여 여기에 둔다.
-- 수집(document)과 분리한다: LLM 장애는 수집을 막지 않고, 보강 실패는 FAILED로 남겨 24시간 뒤 다시 시도한다.
-- document_version을 함께 두어 본문이 바뀌어(새 버전) 다시 보강해야 하는 문서를 찾는다.
-- 언론 기사(본문 미저장)는 제목만으로 분류하고 summary는 NULL이다 (요약도 저장하지 않는다 — 저작권 원칙).
-- topics: 고정 분류 체계 안의 값만 (정책·규제, 입찰·공고, 지원사업, 자사, 경쟁사, 산업동향, 인사·조직, 사건·사고, 기타).
-- tags: 자유 태그 최대 5개 (인명·기관명·제품명 등).
CREATE TABLE document_enrichment (
    document_id       BIGINT       PRIMARY KEY REFERENCES document (id) ON DELETE CASCADE,
    document_version  INT          NOT NULL,
    summary           VARCHAR(600),
    topics            JSONB        NOT NULL DEFAULT '[]'::jsonb,
    tags              JSONB        NOT NULL DEFAULT '[]'::jsonb,
    model             VARCHAR(100),
    status            VARCHAR(20)  NOT NULL DEFAULT 'DONE' CHECK (status IN ('DONE', 'SKIPPED', 'FAILED')),
    error             VARCHAR(500),
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT now()
);
CREATE INDEX idx_document_enrichment_topics ON document_enrichment USING GIN (topics);
CREATE INDEX idx_document_enrichment_created ON document_enrichment (created_at);
