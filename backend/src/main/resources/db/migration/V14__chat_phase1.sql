-- 챗봇 1단계 (도구 호출형): 답의 근거는 문서뿐 아니라 이슈(같은 사건 기사 묶음)일 수도 있다.
-- 이슈는 워커가 다시 묶으며 지우기도 하므로(issues.py) issue_id에는 외래 키를 두지 않고 제목·링크를 스냅숏으로 남긴다.
ALTER TABLE chat_citation
    ADD COLUMN kind VARCHAR(20) NOT NULL DEFAULT 'DOCUMENT' CHECK (kind IN ('DOCUMENT', 'ISSUE')),
    ADD COLUMN issue_id BIGINT,
    -- 표시용 스냅숏: 언론사·기관 이름(없으면 도메인), 발행일(이슈는 마지막 기사 시각)
    ADD COLUMN source_label VARCHAR(300),
    ADD COLUMN published_at TIMESTAMPTZ;

CREATE INDEX idx_chat_citation_message ON chat_citation (message_id, ref_no);
