-- 키워드 감시의 기본 동작을 "네이버 뉴스 검색 API 수집"에서 "수집 기사 대조"로 바꾼다.
-- 네이버는 2026-07-31부터 검색 API 신규 발급을 중단해 키를 받을 수 없다. 키가 없어 REVIEW(API_KEY_REQUIRED/API_KEY_INVALID)에
-- 멈춘 감시를 수집 기사 대조 모드로 옮긴다:
--   설정 새 버전(config에 "naver": false, 이전 ACTIVE는 RETIRED) → source ACTIVE, next_run_at NULL(스케줄러가 잡지 않는다)
--   → 열린 등록 요청은 COMPLETED/DONE. 그러면 관리자 키 큐(API_KEY)에서 빠진다.
-- 대조 자체는 워커가 원래 모든 감시(EXCLUDED 제외)에 하고 있었으므로 그대로 이어진다.
-- 이미 네이버로 잘 돌던 감시(ACTIVE 등)와, 쓸 수 있는 네이버 키(INVALID가 아닌 naver_news)가 있는 경우는 건드리지 않는다
-- (키가 있으면 키 등록이 이미 ONBOARD를 다시 넣었다).

CREATE TEMP TABLE v13_watch ON COMMIT DROP AS
SELECT s.id AS source_id, s.active_config_version_id AS old_config_id
FROM source s
JOIN source_config_version v ON v.id = s.active_config_version_id
WHERE s.kind = 'WATCH'
  AND s.status = 'REVIEW'
  AND s.status_reason IN ('API_KEY_REQUIRED', 'API_KEY_INVALID')
  AND NOT EXISTS (SELECT 1 FROM api_credential c WHERE c.provider = 'naver_news' AND c.status <> 'INVALID');

UPDATE source_config_version v SET status = 'RETIRED'
FROM v13_watch w
WHERE v.source_id = w.source_id AND v.status = 'ACTIVE';

INSERT INTO source_config_version (source_id, version, config, status, change_note)
SELECT w.source_id,
       (SELECT max(version) + 1 FROM source_config_version x WHERE x.source_id = w.source_id),
       old.config || '{"naver": false}'::jsonb,
       'ACTIVE',
       '네이버 키 없음 — 수집 기사 대조 모드로 전환 (V13)'
FROM v13_watch w
JOIN source_config_version old ON old.id = w.old_config_id;

UPDATE source s
SET active_config_version_id = v.id, status = 'ACTIVE', status_reason = NULL, next_run_at = NULL,
    consecutive_failures = 0, updated_at = now()
FROM v13_watch w
JOIN source_config_version v ON v.source_id = w.source_id AND v.status = 'ACTIVE'
WHERE s.id = w.source_id;

UPDATE registration_request r
SET status = 'COMPLETED', step = 'DONE', updated_at = now(),
    result_message = '네이버 뉴스 검색 API 키가 없어 등록된 언론사에서 수집한 기사에 키워드를 대조합니다.'
FROM v13_watch w
WHERE r.source_id = w.source_id AND r.status IN ('RECEIVED', 'PROCESSING', 'REVIEW');
