"""작업 큐와 저장. Java 백엔드와 같은 PostgreSQL 테이블을 쓴다."""

from __future__ import annotations

import hashlib
import json
import logging
from collections import Counter
from dataclasses import dataclass
from datetime import datetime

import psycopg
from psycopg.rows import dict_row

from .codes import CrawlFailure

log = logging.getLogger(__name__)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class Job:
    id: int
    source_id: int
    job_type: str
    trigger: str
    config_version_id: int | None
    attempt: int


def preferred_job_type(claimed: int, onboard_ratio: int) -> str:
    """이번 claim에서 먼저 집을 작업 유형. 등록(ONBOARD)을 onboard_ratio번 집을 때마다 주기 수집(CRAWL)을 한 번 먼저 집는다.

    기본 3:1 — 일괄 등록 수백 건이 들어와도 주기 수집이 네 번에 한 번은 돌아간다. 원하는 유형이 없으면 다른 유형을 집으므로
    큐가 한쪽뿐이면 그쪽을 계속 처리한다. onboard_ratio가 0 이하이면 예전 규칙(항상 ONBOARD 먼저)이다.
    """
    if onboard_ratio <= 0:
        return "ONBOARD"
    return "CRAWL" if claimed % (onboard_ratio + 1) == onboard_ratio else "ONBOARD"


class Store:
    def __init__(self, dsn: str, onboard_ratio: int = 3):
        self.conn = psycopg.connect(dsn, row_factory=dict_row, autocommit=True)
        self.onboard_ratio = onboard_ratio
        self.claimed = 0   # 이 워커가 집은 작업 수 (교대 규칙의 기준, 빈 polling은 세지 않는다)

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------ 큐
    def claim_job(self, worker_id: str, lease_minutes: int) -> Job | None:
        """대기 작업 하나를 잠그고 가져간다. 여러 워커가 동시에 돌아도 겹치지 않는다.

        ONBOARD(사용자가 기다리는 등록)를 먼저 집되, preferred_job_type대로 몇 번에 한 번은 CRAWL을 먼저 집어
        일괄 등록이 주기 수집을 오래 막지 않게 한다. 같은 유형 안에서는 오래 기다린 순이다.
        과거 기사 채우기(BACKFILL) 묶음은 언제나 맨 뒤다 (다른 작업이 하나라도 대기하면 그것부터).
        """
        prefer = preferred_job_type(self.claimed, self.onboard_ratio)
        row = self.conn.execute(
            """
            UPDATE crawl_job SET status = 'RUNNING', locked_by = %s, attempt = attempt + 1,
                   lease_until = now() + make_interval(mins => %s), started_at = COALESCE(started_at, now())
            WHERE id = (
                SELECT id FROM crawl_job WHERE status = 'QUEUED'
                ORDER BY CASE WHEN job_type = 'BACKFILL' THEN 2 WHEN job_type = %s THEN 0 ELSE 1 END, created_at
                FOR UPDATE SKIP LOCKED LIMIT 1)
            RETURNING id, source_id, job_type, trigger, config_version_id, attempt
            """, (worker_id, lease_minutes, prefer)).fetchone()
        if row is None:
            return None
        self.claimed += 1
        return Job(**row)

    def extend_lease(self, job_id: int, worker_id: str, lease_minutes: int) -> None:
        self.conn.execute(
            "UPDATE crawl_job SET lease_until = now() + make_interval(mins => %s) WHERE id = %s AND locked_by = %s",
            (lease_minutes, job_id, worker_id))

    def finish_job(self, job_id: int, status: str, found: int = 0, new: int = 0, errors: int = 0,
                   config_version_id: int | None = None, note: str | None = None) -> None:
        """작업을 끝낸다. locked_by는 남겨 둔다 — 운영 지표(/api/v1/ops/overview)가 최근 5분 안에 일한 워커 수를 이것으로 센다."""
        self.conn.execute(
            """
            UPDATE crawl_job SET status = %s, found_count = %s, new_count = %s, error_count = %s,
                   config_version_id = COALESCE(%s, config_version_id), note = COALESCE(%s, note),
                   finished_at = now(), lease_until = NULL
            WHERE id = %s
            """, (status, found, new, errors, config_version_id, (note or "")[:200] or None, job_id))

    def record_error(self, job_id: int, failure: CrawlFailure, evidence_path: str | None = None) -> None:
        self.conn.execute(
            """
            INSERT INTO crawl_error (job_id, url, stage, code, http_status, message, retryable, evidence_path)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """, (job_id, (failure.url or "")[:2000] or None, failure.stage, failure.code.value, failure.http_status,
                  str(failure)[:1000], failure.retryable, evidence_path))

    # ------------------------------------------------------------------ 소스
    def load_source(self, source_id: int) -> dict:
        return self.conn.execute(
            """
            SELECT s.*, t.domain, t.base_url, t.site_type, t.ai_restricted, v.config, v.version AS config_version
            FROM source s JOIN site t ON t.id = s.site_id
            LEFT JOIN source_config_version v ON v.id = s.active_config_version_id
            WHERE s.id = %s
            """, (source_id,)).fetchone()

    def update_site(self, site_id: int, **fields) -> None:
        if "risk_flags" in fields:
            fields["risk_flags"] = json.dumps(fields["risk_flags"], ensure_ascii=False)
        sets = ", ".join(f"{k} = %s" + ("::jsonb" if k == "risk_flags" else "") for k in fields)
        self.conn.execute(f"UPDATE site SET {sets} WHERE id = %s", (*fields.values(), site_id))

    def set_source_status(self, source_id: int, status: str, reason: str | None = None) -> None:
        self.conn.execute("UPDATE source SET status = %s, status_reason = %s, updated_at = now() WHERE id = %s",
                          (status, reason, source_id))

    def activate_new_config(self, source_id: int, config: dict, note: str, interval_minutes: int) -> tuple[int, int]:
        with self.conn.transaction():
            version = self.conn.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 AS v FROM source_config_version WHERE source_id = %s",
                (source_id,)).fetchone()["v"]
            self.conn.execute("UPDATE source_config_version SET status = 'RETIRED' WHERE source_id = %s AND status = 'ACTIVE'",
                              (source_id,))
            config_id = self.conn.execute(
                """
                INSERT INTO source_config_version (source_id, version, config, status, change_note)
                VALUES (%s, %s, %s::jsonb, 'ACTIVE', %s) RETURNING id
                """, (source_id, version, json.dumps(config, ensure_ascii=False), note)).fetchone()["id"]
            self.conn.execute(
                """
                UPDATE source SET active_config_version_id = %s, method = %s, interval_minutes = %s,
                       status = 'ACTIVE', status_reason = NULL, consecutive_anomalies = 0, updated_at = now()
                WHERE id = %s
                """, (config_id, config["method"], interval_minutes, source_id))
        return config_id, version

    def add_config_version(self, source_id: int, config: dict, note: str) -> tuple[int, int]:
        """설정 새 버전을 활성화하되 소스 상태·주기·방식은 그대로 둔다 (기존 소스에 기사 종류를 더할 때 — 일시정지 소스를 깨우지 않는다)."""
        with self.conn.transaction():
            version = self.conn.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 AS v FROM source_config_version WHERE source_id = %s",
                (source_id,)).fetchone()["v"]
            self.conn.execute("UPDATE source_config_version SET status = 'RETIRED' WHERE source_id = %s AND status = 'ACTIVE'",
                              (source_id,))
            config_id = self.conn.execute(
                """
                INSERT INTO source_config_version (source_id, version, config, status, change_note)
                VALUES (%s, %s, %s::jsonb, 'ACTIVE', %s) RETURNING id
                """, (source_id, version, json.dumps(config, ensure_ascii=False), note)).fetchone()["id"]
            self.conn.execute("UPDATE source SET active_config_version_id = %s, updated_at = now() WHERE id = %s",
                              (config_id, source_id))
        return config_id, version

    def activate_source(self, source_id: int, method: str, interval_minutes: int) -> None:
        """설정 버전은 그대로 두고 소스만 ACTIVE로 (키워드 감시: 백엔드가 만든 설정을 쓴다)."""
        self.conn.execute(
            """
            UPDATE source SET method = %s, interval_minutes = %s, status = 'ACTIVE', status_reason = NULL,
                   consecutive_anomalies = 0, updated_at = now()
            WHERE id = %s
            """, (method, interval_minutes, source_id))

    def activate_match_watch(self, source_id: int) -> None:
        """수집 기사 대조 모드 감시를 ACTIVE로. next_run_at은 NULL이라 스케줄러가 작업을 넣지 않는다 (백엔드 WatchService.start와 같다)."""
        self.conn.execute(
            """
            UPDATE source SET status = 'ACTIVE', status_reason = NULL, next_run_at = NULL, consecutive_failures = 0,
                   consecutive_anomalies = 0, updated_at = now()
            WHERE id = %s
            """, (source_id,))

    def clear_next_run(self, source_id: int) -> None:
        self.conn.execute("UPDATE source SET next_run_at = NULL, updated_at = now() WHERE id = %s", (source_id,))

    def mark_crawl_result(self, source_id: int, success: bool, interval_minutes: int) -> None:
        if success:
            self.conn.execute(
                """
                UPDATE source SET last_success_at = now(), consecutive_failures = 0,
                       next_run_at = now() + make_interval(mins => %s), updated_at = now()
                WHERE id = %s
                """, (interval_minutes, source_id))
        else:
            self.conn.execute(
                """
                UPDATE source SET consecutive_failures = consecutive_failures + 1,
                       next_run_at = now() + make_interval(mins => %s * LEAST(consecutive_failures + 1, 4)),
                       updated_at = now()
                WHERE id = %s
                """, (interval_minutes, source_id))

    # ------------------------------------------------------------------ 공개 API 자격 증명
    def load_credential(self, provider: str, key: str) -> str | None:
        """api_credential의 비밀을 대칭키로 풀어 돌려준다. 없으면 None.

        백엔드가 pgp_sym_encrypt로 넣은 것을 같은 키로 pgp_sym_decrypt한다. 키가 다르면 복호화가 실패하는데,
        이는 '키가 없는 것'과 같이 다룬다 (관리자가 키를 다시 넣으면 백엔드 쪽 키로 다시 암호화된다).
        """
        try:
            row = self.conn.execute(
                "SELECT pgp_sym_decrypt(secret_enc, %s) AS secret FROM api_credential WHERE provider = %s",
                (key, provider)).fetchone()
        except psycopg.Error as e:
            log.warning("credential decrypt failed provider=%s: %s (SENSING_SECRET_KEY가 백엔드와 다른지 확인)",
                        provider, type(e).__name__)
            return None
        return row["secret"] if row else None

    def mark_credential(self, provider: str, status: str, error: str | None) -> None:
        """시험 호출 결과를 남긴다. 비밀은 건드리지 않는다. error에는 키가 들어 있으면 안 된다 (커넥터가 가린다)."""
        self.conn.execute(
            """
            UPDATE api_credential SET status = %s, last_error = %s, last_verified_at = now(), updated_at = now()
            WHERE provider = %s
            """, (status, (error or "")[:500] or None, provider))

    # ------------------------------------------------------------------ 등록 요청
    def update_registration(self, source_id: int, *, status: str | None = None, step: str | None = None,
                            message: str | None = None) -> None:
        self.conn.execute(
            """
            UPDATE registration_request
            SET status = COALESCE(%s, status), step = COALESCE(%s, step),
                result_message = COALESCE(%s, result_message), updated_at = now()
            WHERE source_id = %s AND status IN ('RECEIVED', 'PROCESSING', 'REVIEW')
            """, (status, step, message, source_id))

    def load_registration(self, source_id: int) -> dict | None:
        """소스를 만든 등록 요청 (가장 최근 것). 요청 없이 만든 소스면 None."""
        return self.conn.execute(
            "SELECT id, user_id, channel, batch_id, input_url FROM registration_request WHERE source_id = %s "
            "ORDER BY id DESC LIMIT 1", (source_id,)).fetchone()

    def open_registrations(self, source_id: int) -> list[dict]:
        """이 소스에 걸린 진행 중 등록 요청들 (이미 수집 중인 소스에 다른 목록 주소를 넣은 요청 — 백엔드가 중복 대신 넣는다)."""
        return self.conn.execute(
            "SELECT id, input_url FROM registration_request WHERE source_id = %s AND status IN ('RECEIVED', 'PROCESSING') "
            "ORDER BY id", (source_id,)).fetchall()

    def finish_registration(self, registration_id: int, status: str, message: str) -> None:
        self.conn.execute(
            "UPDATE registration_request SET status = %s, step = 'DONE', result_message = %s, updated_at = now() WHERE id = %s",
            (status, message[:500], registration_id))

    def find_open_source(self, domain: str) -> int | None:
        """같은 도메인의 열린 사이트 소스 id (백엔드 RegistrationService.findOpenSource와 같은 규칙)."""
        row = self.conn.execute(
            """
            SELECT s.id FROM source s JOIN site t ON t.id = s.site_id
            WHERE t.domain = %s AND s.kind = 'SITE' AND s.status <> 'EXCLUDED'
            ORDER BY s.id LIMIT 1
            """, (domain,)).fetchone()
        return row["id"] if row else None

    def register_outlet(self, *, user_id: int, channel: str, input_url: str, domain: str, base_url: str,
                        name: str | None, batch_id: str) -> int | None:
        """링크 모음 페이지가 가리키는 언론사를 백엔드 RegistrationService.startOnboarding과 같은 행으로 등록한다.

        사이트 upsert → 소스 PENDING → ONBOARD 작업 → 등록 요청 PROCESSING/QUEUED(batch_id) → 감사 로그.
        같은 도메인의 열린 소스가 있으면 아무것도 만들지 않고 None (백엔드의 DUPLICATE 판정과 같은 조건).
        돌려주는 값은 새 소스 id.
        """
        with self.conn.transaction():
            if self.find_open_source(domain) is not None:
                return None
            site_id = self.conn.execute(
                """
                INSERT INTO site (domain, base_url, name) VALUES (%s, %s, %s)
                ON CONFLICT (domain) DO UPDATE SET base_url = EXCLUDED.base_url, name = COALESCE(site.name, EXCLUDED.name)
                RETURNING id
                """, (domain, base_url, name)).fetchone()["id"]
            source_id = self.conn.execute(
                "INSERT INTO source (site_id, name, status, requested_by) VALUES (%s, %s, 'PENDING', %s) RETURNING id",
                (site_id, domain, user_id)).fetchone()["id"]
            self.conn.execute("INSERT INTO crawl_job (source_id, job_type, trigger) VALUES (%s, 'ONBOARD', %s)",
                              (source_id, "CHAT" if channel == "CHAT" else "REGISTRATION"))
            self.conn.execute(
                """
                INSERT INTO registration_request (user_id, input_url, normalized_domain, channel, status, step,
                                                  result_message, source_id, batch_id)
                VALUES (%s, %s, %s, %s, 'PROCESSING', 'QUEUED', %s, %s, %s)
                """, (user_id, input_url[:2000], domain, channel, "등록 요청을 접수했어요. 자동으로 점검과 탐색을 진행합니다.",
                      source_id, batch_id))
            self.conn.execute(
                "INSERT INTO audit_log (actor_id, action, target_type, target_id, detail) VALUES (%s, 'SOURCE_REGISTERED', 'source', %s, %s::jsonb)",
                (user_id, source_id, json.dumps({"domain": domain, "channel": channel, "batchId": batch_id}, ensure_ascii=False)))
        return source_id

    # ------------------------------------------------------------------ 문서
    def known_urls(self, url_hashes: list[str]) -> set[str]:
        if not url_hashes:
            return set()
        rows = self.conn.execute("SELECT url_hash FROM document WHERE url_hash = ANY(%s)", (url_hashes,)).fetchall()
        return {r["url_hash"] for r in rows}

    def document_count(self, source_id: int) -> int:
        return self.conn.execute("SELECT count(*) AS c FROM document WHERE source_id = %s", (source_id,)).fetchone()["c"]

    def found_baseline(self, source_id: int, config_version_id: int | None, runs: int = 5,
                       min_runs: int = 3) -> float | None:
        """같은 설정 버전으로 돈 최근 정상 실행의 평균 기사 후보 수. 이력이 min_runs보다 적으면 None.

        새 기사 수(new_count)는 조용한 시간대에 0이 되기 쉬워서, 허브에 보이는 후보 수(found_count)를 기준으로 삼는다.
        이상으로 기록된 실행은 기준에서 뺀다. 설정을 새로 만들면(재탐색) 기준도 새로 쌓인다.
        """
        if config_version_id is None:
            return None
        row = self.conn.execute(
            """
            SELECT count(*) AS n, AVG(found_count) AS a FROM (
                SELECT j.found_count FROM crawl_job j
                WHERE j.source_id = %s AND j.config_version_id = %s AND j.job_type IN ('CRAWL', 'ONBOARD')
                  AND j.status IN ('SUCCESS', 'PARTIAL')
                  AND NOT EXISTS (SELECT 1 FROM crawl_error e WHERE e.job_id = j.id AND e.code = 'ANOMALY_DROP')
                ORDER BY j.id DESC LIMIT %s) t
            """, (source_id, config_version_id, runs)).fetchone()
        return float(row["a"]) if row["n"] >= min_runs else None

    def recent_new_counts(self, source_id: int, runs: int) -> list[int]:
        """최근 주기 수집(CRAWL, SUCCESS·PARTIAL)의 새 문서 수, 최신순. 주기 자동 조정의 재료.

        ONBOARD의 첫 수집은 허브에 보이는 기사를 모두 새로 받으므로 평균을 왜곡한다 — 뺀다.
        """
        if runs <= 0:
            return []
        rows = self.conn.execute(
            """
            SELECT new_count FROM crawl_job
            WHERE source_id = %s AND job_type = 'CRAWL' AND status IN ('SUCCESS', 'PARTIAL')
            ORDER BY id DESC LIMIT %s
            """, (source_id, runs)).fetchall()
        return [int(r["new_count"]) for r in rows]

    def set_interval(self, source_id: int, interval_minutes: int) -> None:
        """주기 자동 조정: interval_minutes만 바꾼다 (next_run_at은 mark_crawl_result가 새 주기로 잡는다)."""
        self.conn.execute("UPDATE source SET interval_minutes = %s, updated_at = now() WHERE id = %s",
                          (interval_minutes, source_id))

    def mark_anomaly(self, source_id: int, anomalous: bool) -> int:
        """연속 이상 횟수를 갱신하고 돌려준다."""
        return self.conn.execute(
            """
            UPDATE source SET consecutive_anomalies = CASE WHEN %s THEN consecutive_anomalies + 1 ELSE 0 END,
                   updated_at = now()
            WHERE id = %s RETURNING consecutive_anomalies
            """, (anomalous, source_id)).fetchone()["consecutive_anomalies"]

    def upsert_document(self, *, source_id: int, job_id: int, url: str, title: str | None, author: str | None,
                        section: str | None, published_at: datetime | None, body: str | None,
                        collected_at: datetime | None = None) -> tuple[int, str]:
        """저장 결과: (id, 'NEW' | 'UPDATED' | 'SAME'). 본문이 바뀌면 이전본을 document_version에 남긴다.

        collected_at: 새 문서의 수집 시각을 정할 때만 (과거 기사 채우기는 발행 시각을 넣는다 — backfill.py). 없으면 지금.
        """
        url_hash = sha256(url)
        content_hash = sha256((title or "") + "\n" + (body or ""))
        with self.conn.transaction():
            current = self.conn.execute(
                "SELECT id, title, body_text, content_hash, version, collected_at FROM document WHERE url_hash = %s FOR UPDATE",
                (url_hash,)).fetchone()
            if current is None:
                doc_id = self.conn.execute(
                    """
                    INSERT INTO document (source_id, url, url_hash, title, author, section, published_at,
                                          body_text, content_hash, collected_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, COALESCE(%s, now())) RETURNING id
                    """, (source_id, url, url_hash, title, author, section, published_at, body, content_hash,
                          collected_at)
                ).fetchone()["id"]
                return doc_id, "NEW"
            if current["content_hash"] == content_hash:
                return current["id"], "SAME"
            self.conn.execute(
                """
                INSERT INTO document_version (document_id, version, title, body_text, content_hash, job_id, collected_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, (current["id"], current["version"], current["title"], current["body_text"],
                      current["content_hash"], job_id, current["collected_at"]))
            self.conn.execute(
                """
                UPDATE document SET title = %s, body_text = %s, content_hash = %s, version = version + 1,
                       collected_at = now(), indexed_at = NULL, origin_status = 'LIVE'
                WHERE id = %s
                """, (title, body, content_hash, current["id"]))
            return current["id"], "UPDATED"

    def add_watch_hit(self, document_id: int, source_id: int, keyword: str | None) -> None:
        """키워드 감시 기록. 같은 문서·감시 쌍은 처음 걸린 검색어만 남긴다."""
        self.conn.execute(
            """
            INSERT INTO document_watch (document_id, source_id, keyword) VALUES (%s, %s, %s)
            ON CONFLICT (document_id, source_id) DO NOTHING
            """, (document_id, source_id, (keyword or "")[:200] or None))

    def load_watch_keywords(self) -> list[dict]:
        """수집 문서에 대조할 키워드 감시: 제외되지 않은 WATCH 소스의 id와 활성 설정(config: keywords·exclude).

        수집 기사 대조 모드(config naver false, 네이버 키 없음 — 기본)든 네이버 모드든, 키가 없어 REVIEW에 멈춘 감시든 모두 대조한다
        — 네이버에서 못 받아도 우리가 긁어 온 기사에서는 걸릴 수 있다.
        """
        return self.conn.execute(
            """
            SELECT s.id, v.config FROM source s
            JOIN source_config_version v ON v.id = s.active_config_version_id
            WHERE s.kind = 'WATCH' AND s.status <> 'EXCLUDED'
            ORDER BY s.id
            """).fetchall()

    # ------------------------------------------------------------------ 회사 관계 (자사·경쟁사·고객사·협력사 라벨)
    def load_company_relations(self) -> list[dict]:
        """관리자가 정한 회사 관계 목록 (relations.from_rows로 넘긴다)."""
        return self.conn.execute(
            "SELECT id, name, aliases, exclude, relation FROM company_relation ORDER BY id").fetchall()

    def add_relation_hit(self, document_id: int, relation_id: int, matched: str) -> bool:
        """수집 때 걸린 관계 기록. 같은 문서·항목 쌍은 처음 걸린 이름만 남긴다. 새로 남겼으면 True."""
        return self.conn.execute(
            """
            INSERT INTO document_relation (document_id, relation_id, matched) VALUES (%s, %s, %s)
            ON CONFLICT (document_id, relation_id) DO NOTHING RETURNING document_id
            """, (document_id, relation_id, matched[:100])).fetchone() is not None

    def load_relation_hits(self, document_ids: list[int]) -> dict[int, list[tuple[int, str]]]:
        """문서별 수집 때 걸린 (관계 항목 id, 걸린 이름) 목록."""
        if not document_ids:
            return {}
        out: dict[int, list[tuple[int, str]]] = {}
        for r in self.conn.execute(
                "SELECT document_id, relation_id, matched FROM document_relation WHERE document_id = ANY(%s) "
                "ORDER BY document_id, relation_id", (document_ids,)):
            out.setdefault(r["document_id"], []).append((r["relation_id"], r["matched"]))
        return out

    # ------------------------------------------------------------------ 회사 (M3)
    def company_of_watch(self, source_id: int) -> int | None:
        """이 키워드 감시가 어느 회사의 감시(company.watch_source_id)인지. 아니면 None."""
        row = self.conn.execute("SELECT id FROM company WHERE watch_source_id = %s AND status = 'ACTIVE' ORDER BY id LIMIT 1",
                                (source_id,)).fetchone()
        return row["id"] if row else None

    def add_document_company(self, document_id: int, company_id: int, matched_by: str) -> bool:
        """문서 → 회사 연결. 이미 있으면 처음 연결한 방식(matched_by)을 그대로 둔다. 새로 넣었으면 True."""
        row = self.conn.execute(
            """
            INSERT INTO document_company (document_id, company_id, matched_by) VALUES (%s, %s, %s)
            ON CONFLICT (document_id, company_id) DO NOTHING RETURNING document_id
            """, (document_id, company_id, matched_by)).fetchone()
        return row is not None

    def load_company_names(self) -> list[dict]:
        """태그 매칭용: ACTIVE 회사의 id·이름·별칭 (id 순)."""
        return self.conn.execute(
            "SELECT id, name, aliases FROM company WHERE status = 'ACTIVE' ORDER BY id").fetchall()

    def companies_of_documents(self, document_ids: list[int]) -> dict[int, list[tuple[int, str]]]:
        """문서별 [(company_id, matched_by)]."""
        if not document_ids:
            return {}
        rows = self.conn.execute(
            """
            SELECT c.document_id, c.company_id, c.matched_by FROM document_company c
            JOIN company k ON k.id = c.company_id AND k.status = 'ACTIVE'
            WHERE c.document_id = ANY(%s) ORDER BY c.matched_at, c.company_id
            """, (document_ids,)).fetchall()
        out: dict[int, list[tuple[int, str]]] = {}
        for r in rows:
            out.setdefault(r["document_id"], []).append((r["company_id"], r["matched_by"]))
        return out

    def add_company_signal(self, company_id: int, kind: str, severity: str, fingerprint: str, title: str,
                           detail: dict | None, observed_at: datetime, document_id: int | None = None,
                           source_id: int | None = None) -> bool:
        """회사 신호 한 건. 같은 (company_id, kind, fingerprint)가 이미 있으면 넣지 않는다 (False)."""
        row = self.conn.execute(
            """
            INSERT INTO company_signal (company_id, kind, severity, fingerprint, title, detail, observed_at, document_id, source_id)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)
            ON CONFLICT (company_id, kind, fingerprint) DO NOTHING RETURNING id
            """, (company_id, kind, severity, fingerprint[:200], (title or "")[:500] or "(제목 없음)",
                  json.dumps(detail or {}, ensure_ascii=False), observed_at, document_id, source_id)).fetchone()
        return row is not None

    def companies_with_biz_no(self) -> list[dict]:
        """국세청 상태 확인 대상: ACTIVE이고 사업자번호가 있는 회사 (id, name, biz_no, last_biz_status)."""
        return self.conn.execute(
            """
            SELECT id, name, biz_no, last_biz_status FROM company
            WHERE status = 'ACTIVE' AND biz_no IS NOT NULL ORDER BY id
            """).fetchall()

    def save_biz_status(self, company_id: int, detail: dict) -> None:
        self.conn.execute("UPDATE company SET last_biz_status = %s::jsonb, updated_at = now() WHERE id = %s",
                          (json.dumps(detail, ensure_ascii=False), company_id))

    def companies_without_corp_code(self) -> list[dict]:
        """DART 고유번호 매핑 대상: ACTIVE이고 corp_code가 비어 있는 회사 (id, name, aliases)."""
        return self.conn.execute(
            """
            SELECT id, name, aliases FROM company
            WHERE status = 'ACTIVE' AND (corp_code IS NULL OR corp_code = '') ORDER BY id
            """).fetchall()

    def set_corp_code(self, company_id: int, corp_code: str) -> bool:
        """비어 있을 때만 채운다 (사람이 손으로 넣은 값은 덮지 않는다). 채웠으면 True."""
        row = self.conn.execute(
            """
            UPDATE company SET corp_code = %s, updated_at = now()
            WHERE id = %s AND (corp_code IS NULL OR corp_code = '') RETURNING id
            """, (corp_code[:8], company_id)).fetchone()
        return row is not None

    def companies_with_corp_code(self) -> list[dict]:
        """DART 회사별 공시 대상: ACTIVE이고 corp_code가 있는 회사 (id, name, corp_code)."""
        return self.conn.execute(
            """
            SELECT id, name, corp_code FROM company
            WHERE status = 'ACTIVE' AND corp_code IS NOT NULL AND corp_code <> '' ORDER BY id
            """).fetchall()

    def companies_for_debarment(self) -> list[dict]:
        """조달청 부정당업자 대조 대상: 모든 ACTIVE 회사 (id, name, aliases, biz_no)."""
        return self.conn.execute(
            "SELECT id, name, aliases, biz_no FROM company WHERE status = 'ACTIVE' ORDER BY id").fetchall()

    def add_attachment(self, document_id: int, url: str, file_name: str) -> None:
        self.conn.execute(
            """
            INSERT INTO attachment (document_id, url, file_name) VALUES (%s, %s, %s)
            ON CONFLICT (document_id, url) DO NOTHING
            """, (document_id, url, file_name))

    # ------------------------------------------------------------------ 보강 (요약·분류)
    def lock_pending_enrichment(self, limit: int, failed_retry_hours: int = 24) -> list[dict]:
        """보강이 필요한 문서를 최신 수집순으로 limit건 잠그고 돌려준다. 반드시 self.conn.transaction() 안에서 부른다.

        대상: 보강 행이 없거나, 본문이 바뀌어 document_version이 뒤처졌거나, FAILED가 failed_retry_hours 전 것.
        FOR UPDATE SKIP LOCKED라 워커 여러 대가 같은 문서를 잡지 않는다. 잠금은 트랜잭션이 끝날 때 풀리므로
        그 사이 같은 문서를 다시 저장하려는 수집(upsert_document)은 잠깐 기다린다.
        """
        return self.conn.execute(
            """
            SELECT d.id, d.version, d.url, d.title, d.body_text, d.section, d.published_at, t.domain, t.site_type
            FROM document d
            JOIN source s ON s.id = d.source_id
            JOIN site t ON t.id = s.site_id
            LEFT JOIN document_enrichment e ON e.document_id = d.id
            WHERE e.document_id IS NULL
               OR e.document_version < d.version
               OR (e.status = 'FAILED' AND e.created_at < now() - make_interval(hours => %s))
            ORDER BY d.collected_at DESC, d.id DESC
            LIMIT %s
            FOR UPDATE OF d SKIP LOCKED
            """, (failed_retry_hours, limit)).fetchall()

    # ------------------------------------------------------------------ 색인·이슈 (M5 기반)
    def lock_pending_index(self, limit: int) -> list[dict]:
        """색인이 필요한 문서(indexed_at IS NULL)를 최신 수집순으로 limit건 잠그고 돌려준다. self.conn.transaction() 안에서 부른다.

        본문이 바뀌면 upsert_document가 indexed_at을 NULL로 되돌리므로 다시 대상이 된다. FOR UPDATE SKIP LOCKED라
        워커 여러 대가 같은 문서를 잡지 않는다.
        """
        return self.conn.execute(
            """
            SELECT d.id, d.source_id, d.title, d.body_text, d.published_at, d.collected_at, t.site_type
            FROM document d
            JOIN source s ON s.id = d.source_id
            JOIN site t ON t.id = s.site_id
            WHERE d.indexed_at IS NULL
            ORDER BY d.collected_at DESC, d.id DESC
            LIMIT %s
            FOR UPDATE OF d SKIP LOCKED
            """, (limit,)).fetchall()

    def lock_pending_embedding(self, identity: str, limit: int) -> list[dict]:
        """임베딩이 필요한 조각(embed_model이 지금 설정과 다른 것 — 아직 안 했거나 모델을 바꿨다)을 최신 수집 문서부터 limit건
        잠그고 돌려준다. self.conn.transaction() 안에서 부른다. 문서의 제목·사이트 유형·AI 봇 차단 여부를 함께 준다
        (embed.embed_text가 보낼 글을 정한다)."""
        return self.conn.execute(
            """
            SELECT c.id, c.document_id, c.seq, c.text, d.title, t.site_type, t.ai_restricted
            FROM chunk c
            JOIN document d ON d.id = c.document_id
            JOIN source s ON s.id = d.source_id
            JOIN site t ON t.id = s.site_id
            WHERE c.embed_model IS DISTINCT FROM %s
            ORDER BY d.collected_at DESC, c.document_id DESC, c.seq
            LIMIT %s
            FOR UPDATE OF c SKIP LOCKED
            """, (identity, limit)).fetchall()

    def load_watch_hits(self, document_ids: list[int]) -> dict[int, list[tuple[str | None, str | None]]]:
        """문서별 (감시 이름, 걸린 검색어) 목록."""
        if not document_ids:
            return {}
        rows = self.conn.execute(
            """
            SELECT w.document_id, s.watch_name, w.keyword
            FROM document_watch w JOIN source s ON s.id = w.source_id
            WHERE w.document_id = ANY(%s) ORDER BY w.matched_at, w.source_id
            """, (document_ids,)).fetchall()
        out: dict[int, list[tuple[str | None, str | None]]] = {}
        for r in rows:
            out.setdefault(r["document_id"], []).append((r["watch_name"], r["keyword"]))
        return out

    def save_enrichment(self, document_id: int, document_version: int, *, summary: str | None, topics: list[str],
                        tags: list[str], model: str | None, status: str = "DONE", error: str | None = None) -> None:
        self.conn.execute(
            """
            INSERT INTO document_enrichment (document_id, document_version, summary, topics, tags, model, status, error)
            VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s)
            ON CONFLICT (document_id) DO UPDATE SET
                document_version = EXCLUDED.document_version, summary = EXCLUDED.summary,
                topics = EXCLUDED.topics, tags = EXCLUDED.tags, model = EXCLUDED.model,
                status = EXCLUDED.status, error = EXCLUDED.error, created_at = now()
            """, (document_id, document_version, (summary or None) and summary[:600],
                  json.dumps(topics, ensure_ascii=False), json.dumps(tags, ensure_ascii=False),
                  (model or "")[:100] or None, status, (error or "")[:500] or None))

    # ------------------------------------------------------------------ 과거 기사 채우기 (BACKFILL)
    def enqueue_backfill_chunk(self) -> int | None:
        """큐가 비었을 때 진행 중인 채우기 하나의 다음 묶음(crawl_job BACKFILL)을 넣는다. 넣은 작업 id, 넣을 것이 없으면 None.

        대상: QUEUED·RUNNING 채우기 중 소스가 ACTIVE 사이트 소스이고, 그 소스에 열린 작업이 없고, 주기 수집 차례가 아직 오지 않은 것
        (차례가 왔으면 스케줄러가 CRAWL을 넣도록 비워 둔다). 다른 작업이 하나라도 대기 중이면 넣지 않는다.
        여러 채우기가 있으면 가장 오래 쉰 것(updated_at)부터 돌아가며 한다.
        """
        row = self.conn.execute(
            """
            INSERT INTO crawl_job (source_id, job_type, config_version_id, trigger, backfill_id)
            SELECT b.source_id, 'BACKFILL', s.active_config_version_id, 'MANUAL', b.id
            FROM backfill b JOIN source s ON s.id = b.source_id
            WHERE b.status IN ('QUEUED', 'RUNNING') AND s.status = 'ACTIVE' AND s.kind = 'SITE'
              AND (s.next_run_at IS NULL OR s.next_run_at > now())
              AND NOT EXISTS (SELECT 1 FROM crawl_job j WHERE j.source_id = b.source_id AND j.status IN ('QUEUED', 'RUNNING'))
              AND NOT EXISTS (SELECT 1 FROM crawl_job q WHERE q.status = 'QUEUED')
            ORDER BY b.updated_at, b.id
            LIMIT 1
            ON CONFLICT (source_id) WHERE status IN ('QUEUED', 'RUNNING') DO NOTHING
            RETURNING id
            """).fetchone()
        return row["id"] if row else None

    def load_backfill(self, job_id: int, source_id: int) -> dict | None:
        """이 묶음 작업의 채우기 (crawl_job.backfill_id, 없으면 소스의 열린 채우기)."""
        row = self.conn.execute(
            "SELECT b.* FROM backfill b JOIN crawl_job j ON j.backfill_id = b.id WHERE j.id = %s", (job_id,)).fetchone()
        if row is None:
            row = self.conn.execute(
                "SELECT * FROM backfill WHERE source_id = %s AND status IN ('QUEUED', 'RUNNING') ORDER BY id DESC LIMIT 1",
                (source_id,)).fetchone()
        return row

    def backfill_status(self, backfill_id: int) -> str | None:
        row = self.conn.execute("SELECT status FROM backfill WHERE id = %s", (backfill_id,)).fetchone()
        return row["status"] if row else None

    def start_backfill(self, backfill_id: int) -> None:
        self.conn.execute(
            """
            UPDATE backfill SET status = 'RUNNING', started_at = COALESCE(started_at, now()), updated_at = now()
            WHERE id = %s AND status = 'QUEUED'
            """, (backfill_id,))

    def save_backfill(self, backfill_id: int, *, method: str | None, cursor: dict, requests: int,
                      message: str | None) -> None:
        """묶음 끝: 진행 위치(cursor)·방법·요청 수·묶음 수를 남긴다. 상태는 건드리지 않는다 (그 사이 취소됐을 수 있다)."""
        self.conn.execute(
            """
            UPDATE backfill SET method = COALESCE(%s, method), cursor = %s::jsonb, request_count = request_count + %s,
                   chunk_count = chunk_count + 1, message = COALESCE(%s, message), updated_at = now()
            WHERE id = %s
            """, (method, json.dumps(cursor, ensure_ascii=False), requests, (message or "")[:500] or None, backfill_id))

    def finish_backfill(self, backfill_id: int, status: str, message: str) -> None:
        """DONE | FAILED. 사용자가 그 사이 취소했으면 취소로 둔다."""
        self.conn.execute(
            """
            UPDATE backfill SET status = %s, message = %s, finished_at = now(), updated_at = now()
            WHERE id = %s AND status IN ('QUEUED', 'RUNNING')
            """, (status, message[:500], backfill_id))

    def backfill_yield_reason(self, source_id: int) -> str | None:
        """묶음을 멈추고 양보할 이유. 다른 작업이 대기 중이거나, 이 소스의 주기 수집 차례가 왔거나, 소스가 ACTIVE가 아니면."""
        row = self.conn.execute(
            """
            SELECT (SELECT count(*) FROM crawl_job WHERE status = 'QUEUED' AND job_type <> 'BACKFILL') AS queued,
                   s.status, s.next_run_at IS NOT NULL AND s.next_run_at <= now() AS due
            FROM source s WHERE s.id = %s
            """, (source_id,)).fetchone()
        if row is None:
            return "소스가 없습니다"
        if row["status"] != "ACTIVE":
            return f"소스 상태가 {row['status']}라 멈춤"
        if row["queued"]:
            return f"대기 중인 작업 {row['queued']}건에 양보"
        if row["due"]:
            return "이 소스의 주기 수집 차례라 양보"
        return None

    def add_backfill_urls(self, backfill_id: int,
                          rows: list[tuple[str, str | None, str | None, int | None]]) -> tuple[int, int]:
        """찾은 기사 주소 (url, 날짜 힌트, 종류 이름, 기간 묶음 id)를 넣는다. 이미 문서로 있는 주소는 SKIPPED로 넣는다(열지 않는다).
        같은 채우기에서 이미 찾은 주소는 다시 넣지 않는다. 채우기와 묶음의 찾은 수·이미 있음 수를 늘린다. (새로 넣은 수, 그중 이미 있던 문서 수)."""
        if not rows:
            return 0, 0
        unique = {sha256(u): (u, hint, kind, seg) for u, hint, kind, seg in rows}
        hashes = list(unique)
        inserted = skipped = 0
        per_segment: Counter[tuple[int | None, str]] = Counter()
        with self.conn.transaction():
            for i in range(0, len(hashes), 5000):   # 사이트맵 한 파일이 수만 건이어도 한 번에 몇천 건씩
                part = hashes[i:i + 5000]
                known = self.known_urls(part)
                done = self.conn.execute(
                    """
                    INSERT INTO backfill_url (backfill_id, url_hash, url, date_hint, kind, segment_id, status, done_at)
                    SELECT %s, h, u, d, k, g, s, CASE WHEN s = 'SKIPPED' THEN now() END
                    FROM unnest(%s::text[], %s::text[], %s::text[], %s::text[], %s::bigint[], %s::text[])
                         WITH ORDINALITY AS t(h, u, d, k, g, s, n)
                    ORDER BY n
                    ON CONFLICT (backfill_id, url_hash) DO NOTHING
                    RETURNING segment_id, status
                    """, (backfill_id, part, [unique[h][0][:2000] for h in part],
                          [(unique[h][1] or "")[:64] or None for h in part],
                          [(unique[h][2] or "")[:100] or None for h in part],
                          [unique[h][3] for h in part],
                          ["SKIPPED" if h in known else "PENDING" for h in part])).fetchall()
                inserted += len(done)
                skipped += sum(1 for r in done if r["status"] == "SKIPPED")
                per_segment.update((r["segment_id"], r["status"]) for r in done)
            self.conn.execute(
                """
                UPDATE backfill SET found_count = found_count + %s, skipped_count = skipped_count + %s, updated_at = now()
                WHERE id = %s
                """, (inserted, skipped, backfill_id))
            for seg in {s for s, _ in per_segment if s is not None}:
                found = per_segment[(seg, "PENDING")] + per_segment[(seg, "SKIPPED")]
                self.conn.execute(
                    """
                    UPDATE backfill_segment SET found_count = found_count + %s, skipped_count = skipped_count + %s,
                           updated_at = now() WHERE id = %s
                    """, (found, per_segment[(seg, "SKIPPED")], seg))
        return inserted, skipped

    def next_backfill_urls(self, backfill_id: int, max_seq: int, limit: int) -> list[dict]:
        """아직 열지 않은 주소: 최근 묶음부터(묶음 순서 max_seq까지), 묶음 안에서는 찾은 순서대로."""
        return self.conn.execute(
            """
            SELECT u.url, u.url_hash, u.date_hint, u.kind, u.segment_id FROM backfill_url u
            LEFT JOIN backfill_segment g ON g.id = u.segment_id
            WHERE u.backfill_id = %s AND u.status = 'PENDING' AND COALESCE(g.seq, 0) <= %s
            ORDER BY COALESCE(g.seq, 0), u.seq LIMIT %s
            """, (backfill_id, max_seq, limit)).fetchall()

    _BACKFILL_COUNTERS = {"SAVED": "saved_count", "SKIPPED": "skipped_count", "OUT_OF_RANGE": "out_of_range_count",
                          "FAILED": "failed_count"}

    def mark_backfill_url(self, backfill_id: int, url_hash: str, status: str, move_to: int | None = None,
                          published: str | None = None) -> None:
        """주소 하나의 결과를 남기고 채우기·묶음의 해당 수를 늘린다 (진행률이 화면에 바로 보인다).
        move_to: 열어 본 발행일이 다른 묶음이면 그 묶음으로 옮긴다 (찾은 수도 옮긴다).
        published: 기간 밖이었던 글의 실제 발행일 — date_hint에 남겨 두고, 기간을 늘리면 그 안에 든 것만 다시 연다."""
        column = self._BACKFILL_COUNTERS[status]
        with self.conn.transaction():
            row = self.conn.execute(
                """
                SELECT segment_id FROM backfill_url WHERE backfill_id = %s AND url_hash = %s AND status = 'PENDING' FOR UPDATE
                """, (backfill_id, url_hash)).fetchone()
            if row is None:
                return
            old = row["segment_id"]
            new = move_to if move_to is not None else old
            self.conn.execute(
                """
                UPDATE backfill_url SET status = %s, done_at = now(), segment_id = %s, date_hint = COALESCE(%s, date_hint)
                WHERE backfill_id = %s AND url_hash = %s
                """, (status, new, published, backfill_id, url_hash))
            self.conn.execute(f"UPDATE backfill SET {column} = {column} + 1, updated_at = now() WHERE id = %s",
                              (backfill_id,))
            if old is not None and new is not None and old != new:
                self.conn.execute("UPDATE backfill_segment SET found_count = found_count - 1 WHERE id = %s", (old,))
                self.conn.execute("UPDATE backfill_segment SET found_count = found_count + 1 WHERE id = %s", (new,))
            if new is not None:
                self.conn.execute(f"UPDATE backfill_segment SET {column} = {column} + 1, updated_at = now() WHERE id = %s",
                                  (new,))

    def out_of_range_urls(self, backfill_id: int) -> list[dict]:
        """기간 밖이었던 주소와 열어 본 발행일(date_hint), 원래 묶음."""
        return self.conn.execute(
            "SELECT url_hash, date_hint, segment_id FROM backfill_url WHERE backfill_id = %s AND status = 'OUT_OF_RANGE'",
            (backfill_id,)).fetchall()

    def reopen_backfill_urls(self, backfill_id: int, moves: list[tuple[str, int | None, int]]) -> None:
        """(url_hash, 원래 묶음, 새 묶음): 기간을 늘려 기간 안이 된 글을 다시 연다. 기간 밖 수를 되돌리고 찾은 수를 새 묶음으로 옮긴다."""
        if not moves:
            return
        with self.conn.transaction():
            for url_hash, old, new in moves:
                self.conn.execute(
                    """
                    UPDATE backfill_url SET status = 'PENDING', done_at = NULL, segment_id = %s
                    WHERE backfill_id = %s AND url_hash = %s AND status = 'OUT_OF_RANGE'
                    """, (new, backfill_id, url_hash))
                if old is not None:
                    self.conn.execute(
                        """
                        UPDATE backfill_segment SET found_count = found_count - 1, out_of_range_count = out_of_range_count - 1
                        WHERE id = %s
                        """, (old,))
                self.conn.execute("UPDATE backfill_segment SET found_count = found_count + 1 WHERE id = %s", (new,))
            self.conn.execute("UPDATE backfill SET out_of_range_count = out_of_range_count - %s WHERE id = %s",
                              (len(moves), backfill_id))

    # 기간 묶음 ------------------------------------------------------------
    def set_backfill_unit(self, backfill_id: int, unit_months: int, unit_offset: int, date_from) -> None:
        """묶음 단위를 정하고 시작 날짜를 그 묶음의 첫날로 맞춘다 (사이트맵 단위를 알아낸 뒤)."""
        self.conn.execute(
            "UPDATE backfill SET unit_months = %s, unit_offset = %s, date_from = %s, updated_at = now() WHERE id = %s",
            (unit_months, unit_offset, date_from, backfill_id))

    def load_segments(self, backfill_id: int) -> list[dict]:
        return self.conn.execute("SELECT * FROM backfill_segment WHERE backfill_id = %s ORDER BY seq",
                                 (backfill_id,)).fetchall()

    def add_segments(self, backfill_id: int, segments: list[tuple[int, object, object]]) -> None:
        """(seq, date_from, date_to). 이미 있는 묶음(같은 시작 날짜)은 그대로 둔다."""
        for seq, start, end in segments:
            self.conn.execute(
                """
                INSERT INTO backfill_segment (backfill_id, seq, date_from, date_to) VALUES (%s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                """, (backfill_id, seq, start, end))

    def start_segment(self, segment_id: int) -> None:
        self.conn.execute(
            """
            UPDATE backfill_segment SET status = 'RUNNING', started_at = COALESCE(started_at, now()), updated_at = now()
            WHERE id = %s AND status = 'QUEUED'
            """, (segment_id,))

    def finish_segment(self, segment_id: int, status: str, method: str | None, message: str) -> None:
        self.conn.execute(
            """
            UPDATE backfill_segment SET status = %s, method = %s, message = %s, finished_at = now(), updated_at = now()
            WHERE id = %s
            """, (status, method, message[:500], segment_id))

    def segment_message(self, segment_id: int, method: str | None, message: str) -> None:
        self.conn.execute("UPDATE backfill_segment SET method = %s, message = %s, updated_at = now() WHERE id = %s",
                          (method, message[:500], segment_id))

    # 사이트맵 파일 ----------------------------------------------------------
    def add_sitemaps(self, backfill_id: int, rows: list[dict]) -> int:
        """사이트맵 파일 행 (url, depth, span_from, span_to, lastmod, grp, num). 이미 아는 파일은 그대로 둔다. 새로 넣은 수."""
        if not rows:
            return 0
        added = 0
        with self.conn.transaction():
            for i in range(0, len(rows), 2000):
                part = rows[i:i + 2000]
                added += len(self.conn.execute(
                    """
                    INSERT INTO backfill_sitemap (backfill_id, url_hash, url, depth, span_from, span_to, lastmod, grp, num)
                    SELECT %s, h, u, d, sf, st, lm, g, n
                    FROM unnest(%s::text[], %s::text[], %s::int[], %s::date[], %s::date[], %s::date[], %s::text[], %s::int[])
                         AS t(h, u, d, sf, st, lm, g, n)
                    ON CONFLICT DO NOTHING RETURNING url_hash
                    """, (backfill_id, [sha256(r["url"]) for r in part], [r["url"][:2000] for r in part],
                          [r.get("depth", 0) for r in part], [r.get("span_from") for r in part],
                          [r.get("span_to") for r in part], [r.get("lastmod") for r in part],
                          [(r.get("grp") or "")[:300] or None for r in part], [r.get("num") for r in part])).fetchall())
        return added

    def load_sitemaps(self, backfill_id: int) -> list[dict]:
        return self.conn.execute(
            "SELECT * FROM backfill_sitemap WHERE backfill_id = %s ORDER BY depth, url_hash", (backfill_id,)).fetchall()

    def mark_sitemap(self, backfill_id: int, url: str, *, is_index: bool | None, read_from, obs_from=None, obs_to=None,
                     entries: int | None = None, error: str | None = None) -> None:
        self.conn.execute(
            """
            UPDATE backfill_sitemap SET is_index = %s, read_from = %s, obs_from = %s, obs_to = %s, entries = %s,
                   error = %s, read_at = now()
            WHERE backfill_id = %s AND url_hash = %s
            """, (is_index, read_from, obs_from, obs_to, entries, (error or "")[:200] or None, backfill_id, sha256(url)))

    def pending_backfill_count(self, backfill_id: int) -> int:
        return self.conn.execute("SELECT count(*) AS c FROM backfill_url WHERE backfill_id = %s AND status = 'PENDING'",
                                 (backfill_id,)).fetchone()["c"]

    def newest_published(self, urls: list[str]) -> datetime | None:
        """이미 문서로 있는 주소들의 가장 늦은 발행 시각 (목록에 날짜가 없을 때 멈출 곳을 정하는 데 쓴다)."""
        if not urls:
            return None
        row = self.conn.execute("SELECT max(published_at) AS m FROM document WHERE url_hash = ANY(%s)",
                                ([sha256(u) for u in urls],)).fetchone()
        return row["m"] if row else None
