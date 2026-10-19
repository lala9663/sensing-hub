-- M5 챗봇 2단계: 의미 검색(임베딩). 워커(embed.py)가 chunk.embedding(V1, VECTOR(1024))을 채우고, 어떤 모델로 만든 벡터인지
-- embed_model에 남긴다 ('모델 이름/차원', 예: gemini-embedding-001/1024). 백엔드는 질의를 같은 설정으로 임베딩하고
-- embed_model이 같은 조각끼리만 비교한다 — 다른 모델의 벡터는 서로 비교할 수 없다.
--   embed_model IS NULL                  : 아직 임베딩하지 않은 조각 (색인이 조각을 다시 만들면 다시 NULL)
--   embed_model = 현재, embedding 있음   : 의미 검색 대상
--   embed_model = 현재, embedding NULL   : 외부로 보내지 않기로 한 조각 (AI 이용을 막은 사이트의 본문 조각)
--   embed_model ≠ 현재                   : 모델을 바꾼 뒤라 다시 임베딩할 조각

ALTER TABLE chunk ADD COLUMN embed_model VARCHAR(150);
ALTER TABLE chunk ADD COLUMN embedded_at TIMESTAMPTZ;

-- 문서의 조각을 지우고 다시 넣는 색인(DELETE ... WHERE document_id = ANY)과 의미 검색의 조각→문서 결합에 쓴다
CREATE INDEX idx_chunk_document ON chunk (document_id);
