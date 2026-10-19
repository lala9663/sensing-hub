-- 주요 키워드: 네이버 뉴스 탭의 빠른 검색 칩과 챗봇 시작 화면의 칩. 관리자가 추가·삭제·순서 변경한다 (감사 로그 FEATURED_KEYWORDS_UPDATE).
-- 키워드 감시(source WATCH)·회사 감시와는 따로다: 감시 키워드 전체를 칩으로 쓰면 너무 많아(목업 회사 키워드까지) 짧은 목록을 둔다.
-- label은 칩에 보이는 이름, query는 실제로 찾는 검색어다. 짧은 영문(DS·DX·DSR)은 다른 뜻 기사(닌텐도 DS, 대출 규제 DSR)가 섞이므로
-- label은 그대로 두고 query만 '삼성전자 DS'처럼 바꿀 수 있게 나눴다 (기본값은 label과 같다).
CREATE TABLE featured_keyword (
    id          BIGSERIAL PRIMARY KEY,
    label       VARCHAR(30)  NOT NULL,
    query       VARCHAR(100) NOT NULL,
    position    INT          NOT NULL,              -- 칩 순서 (0부터)
    updated_by  BIGINT       REFERENCES app_user (id),
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX uq_featured_keyword_label ON featured_keyword (lower(label));

-- 기본값: 사용자가 준 순서 그대로 (영문은 대문자로 표시)
INSERT INTO featured_keyword (label, query, position) VALUES
    ('반도체', '반도체', 0),
    ('메타넷', '메타넷', 1),
    ('메타넷 글로벌', '메타넷 글로벌', 2),
    ('삼성전자', '삼성전자', 3),
    ('DS', 'DS', 4),
    ('DX', 'DX', 5),
    ('DSR', 'DSR', 6),
    ('테슬라', '테슬라', 7),
    ('하이닉스', '하이닉스', 8);
