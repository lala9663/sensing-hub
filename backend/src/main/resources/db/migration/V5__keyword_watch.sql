-- M2: 키워드 감시. 현업(대외협력·커뮤니케이션)은 사이트가 아니라 키워드(자사·경쟁사·이슈) 단위로 보도를 본다.
-- 감시 하나는 source 한 행(kind WATCH)이고, 키워드는 설정 버전 config에 둔다:
--   {"method":"API","provider":"naver_news","keywords":[...],"exclude":[...]}
-- site는 제공자 도메인 한 행(news.naver.com)이며 여러 감시가 같은 site를 공유한다.
ALTER TABLE source ADD COLUMN kind VARCHAR(20) NOT NULL DEFAULT 'SITE' CHECK (kind IN ('SITE', 'WATCH'));
ALTER TABLE source ADD COLUMN watch_name VARCHAR(200);

-- 같은 기사가 여러 감시에 걸려도 문서는 하나(url_hash)이고, 어느 감시의 어느 검색어로 걸렸는지는 여기에 남긴다.
-- 다이제스트(M2-4)가 이 표를 읽는다.
CREATE TABLE document_watch (
    document_id  BIGINT       NOT NULL REFERENCES document (id) ON DELETE CASCADE,
    source_id    BIGINT       NOT NULL REFERENCES source (id),
    keyword      VARCHAR(200),
    matched_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (document_id, source_id)
);
CREATE INDEX idx_document_watch_source ON document_watch (source_id, matched_at DESC);
