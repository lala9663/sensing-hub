-- 회사 관계 목록: 문서 분류의 자사·경쟁사·고객사·협력사 라벨을 LLM이 아니라 관리자가 정한 이 목록으로 붙인다 (2026-10-20).
-- 전에는 LLM이 9개 분류에서 자사·경쟁사도 골랐는데, 자사·경쟁사가 누구인지 시스템 어디에도 없어 "자사"를 '기사 속 그 회사 자신',
-- "경쟁사"를 '아무 회사'로 읽었다 ("광동제약, 창립 63주년" → 자사, "이케아, 어린이 컬렉션 출시" → 경쟁사. 운영 DB 경쟁사 726건·
-- 자사 203건 대부분 오분류). 이제 LLM은 나머지 7개 분류만 고르고, 워커가 문서 제목·본문에서 이 목록의 이름·별칭을 키워드 감시와
-- 같은 규칙(한글 부분 일치, 영문 단어 경계 — worker watchmatch)으로 찾아 관계 이름을 분류에 더한다.
-- 관리자 메뉴 "자사·경쟁사"에서 추가·수정·삭제하고, 바꿀 때마다 감사 로그(COMPANY_RELATION_CREATE·UPDATE·DELETE)를 남긴다.
-- 거래처(company) 마스터와는 따로다: company는 공급사 리스크(케이던시아) 대상 회사이고, 이 목록은 고객(삼성전자) 입장의 관계다.
CREATE TABLE company_relation (
    id          BIGSERIAL    PRIMARY KEY,
    name        VARCHAR(100) NOT NULL,
    aliases     JSONB        NOT NULL DEFAULT '[]'::jsonb,   -- 별칭·영문명·약칭 (문자열 배열)
    -- 제외어: 이 말 안에 든 이름·별칭은 세지 않는다 (예: '인텔'의 제외어 '인텔리전스'). 감시의 제외어(제목에 있으면 감시 전체를
    -- 건너뜀)와 달리 그 자리만 지우고 본다 — "인텔리전스 시대, 인텔의 반격"은 여전히 인텔에 걸린다.
    exclude     JSONB        NOT NULL DEFAULT '[]'::jsonb,
    relation    VARCHAR(20)  NOT NULL CHECK (relation IN ('OWN', 'COMPETITOR', 'CUSTOMER', 'PARTNER')),   -- 자사·경쟁사·고객사·협력사
    note        VARCHAR(300),
    created_by  BIGINT       REFERENCES app_user (id),
    updated_by  BIGINT       REFERENCES app_user (id),
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX uq_company_relation_name ON company_relation (lower(name));

-- 수집 때 걸린 관계. 감시 대조(document_watch)와 같은 시점에 제목과 본문을 보고 남긴다. 언론 기사는 본문을 저장하지 않으므로
-- 보강·다시 라벨링 때는 이 기록으로 "수집 때 본문에서 걸렸다"를 안다. 관계 항목을 지우면 기록도 지워지고,
-- 그 뒤 별칭을 빼면 그 별칭으로 걸린 기록은 라벨에 쓰지 않는다 (matched가 지금 이름·별칭에 있을 때만 쓴다).
CREATE TABLE document_relation (
    document_id  BIGINT       NOT NULL REFERENCES document (id) ON DELETE CASCADE,
    relation_id  BIGINT       NOT NULL REFERENCES company_relation (id) ON DELETE CASCADE,
    matched      VARCHAR(100) NOT NULL,   -- 걸린 이름·별칭
    matched_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (document_id, relation_id)
);
CREATE INDEX idx_document_relation_relation ON document_relation (relation_id);

-- 기본값: 고객(삼성 DS)의 회사인 삼성전자를 자사로 둔다 (최종 업무 정의서 2026-10-19). 경쟁사·고객사·협력사는 비워 두고 현업이 정한다.
-- '삼성'·'Samsung' 단독은 넣지 않는다: 제목에 '삼성'이 든 기사의 상당수가 삼성전기·삼성물산·삼성바이오·삼성생명 같은 계열사와
-- 프로야구 삼성 라이온즈, 지명(삼성역)이다 (2026-10-20 운영 DB 제목 292건 중 '삼성전자'는 79건). 필요하면 관리자가 더한다.
INSERT INTO company_relation (name, aliases, relation, note) VALUES
    ('삼성전자', '["Samsung Electronics", "삼성 DS", "삼성 DX", "DS부문", "DX부문"]', 'OWN',
     '고객(삼성 DS)의 회사. ''삼성'' 단독은 계열사·프로야구까지 걸려 기본에서 뺐다');
