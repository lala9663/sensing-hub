-- 누락 점검 (2026-10-21): 소스마다 하루 한 번, 사이트가 공개한 기사 목록(허브 + robots가 허용한 사이트맵 하나)에 보이는 기사 주소 중
-- 켜진 기사 종류(패턴)에 맞지 않는 비율. 등록 때 본 주소로 패턴을 고정하면 다른 주소 모양(작은 분류가 붙은 서울신문, 영숫자 번호의
-- 한국경제, 사회면만 잡던 동아일보 v1 설정)을 조용히 놓치므로, 워커가 주기 수집 중에 재고 관리자 운영 지표가 재탐색 후보로 보여 준다.
-- (V20은 회사 관계 브랜치가 쓴다)
CREATE TABLE pattern_check (
    id                BIGSERIAL    PRIMARY KEY,
    source_id         BIGINT       NOT NULL REFERENCES source (id) ON DELETE CASCADE,
    config_version_id BIGINT       REFERENCES source_config_version (id) ON DELETE SET NULL,
    checked_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
    reference         VARCHAR(20)  NOT NULL,                -- HUB | HUB+SITEMAP: 비교한 공개 목록
    reference_urls    JSONB        NOT NULL DEFAULT '[]',   -- 읽은 허브·사이트맵 주소
    article_count     INT          NOT NULL,                -- 기사로 보이는 주소 수 (아래 셋의 합)
    matched_count     INT          NOT NULL,                -- 켜진 종류에 맞음
    off_count         INT          NOT NULL,                -- 꺼 둔 종류에 맞음 (사진·영상 등, 누락 아님)
    unmatched_count   INT          NOT NULL,                -- 어느 종류에도 맞지 않음 (누락 후보)
    unmatched_ratio   NUMERIC(5,4) NOT NULL,                -- unmatched / article (기사 주소가 없으면 0)
    top_unmatched     JSONB        NOT NULL DEFAULT '[]'    -- [{signature, count, example}] 많은 순 5개
);
CREATE INDEX idx_pattern_check_source ON pattern_check (source_id, checked_at DESC);
