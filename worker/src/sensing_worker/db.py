"""작업 큐와 저장. Java 백엔드와 같은 PostgreSQL 테이블을 쓴다."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime

import psycopg
from psycopg.rows import dict_row

from .codes import CrawlFailure


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


class Store:
    def __init__(self, dsn: str):
        self.conn = psycopg.connect(dsn, row_factory=dict_row, autocommit=True)

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------ 큐
    def claim_job(self, worker_id: str, lease_minutes: int) -> Job | None:
        """대기 작업 하나를 잠그고 가져간다. 여러 워커가 동시에 돌아도 겹치지 않는다."""
        row = self.conn.execute(
            """
            UPDATE crawl_job SET status = 'RUNNING', locked_by = %s, attempt = attempt + 1,
                   lease_until = now() + make_interval(mins => %s), started_at = COALESCE(started_at, now())
            WHERE id = (
                SELECT id FROM crawl_job WHERE status = 'QUEUED'
                ORDER BY CASE job_type WHEN 'ONBOARD' THEN 0 ELSE 1 END, created_at
                FOR UPDATE SKIP LOCKED LIMIT 1)
            RETURNING id, source_id, job_type, trigger, config_version_id, attempt
            """, (worker_id, lease_minutes)).fetchone()
        return Job(**row) if row else None

    def extend_lease(self, job_id: int, worker_id: str, lease_minutes: int) -> None:
        self.conn.execute(
            "UPDATE crawl_job SET lease_until = now() + make_interval(mins => %s) WHERE id = %s AND locked_by = %s",
            (lease_minutes, job_id, worker_id))

    def finish_job(self, job_id: int, status: str, found: int = 0, new: int = 0, errors: int = 0,
                   config_version_id: int | None = None) -> None:
        self.conn.execute(
            """
            UPDATE crawl_job SET status = %s, found_count = %s, new_count = %s, error_count = %s,
                   config_version_id = COALESCE(%s, config_version_id),
                   finished_at = now(), locked_by = NULL, lease_until = NULL
            WHERE id = %s
            """, (status, found, new, errors, config_version_id, job_id))

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

    def mark_anomaly(self, source_id: int, anomalous: bool) -> int:
        """연속 이상 횟수를 갱신하고 돌려준다."""
        return self.conn.execute(
            """
            UPDATE source SET consecutive_anomalies = CASE WHEN %s THEN consecutive_anomalies + 1 ELSE 0 END,
                   updated_at = now()
            WHERE id = %s RETURNING consecutive_anomalies
            """, (anomalous, source_id)).fetchone()["consecutive_anomalies"]

    def upsert_document(self, *, source_id: int, job_id: int, url: str, title: str | None, author: str | None,
                        section: str | None, published_at: datetime | None, body: str | None) -> tuple[int, str]:
        """저장 결과: (id, 'NEW' | 'UPDATED' | 'SAME'). 본문이 바뀌면 이전본을 document_version에 남긴다."""
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
                                          body_text, content_hash)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
                    """, (source_id, url, url_hash, title, author, section, published_at, body, content_hash)
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

    def add_attachment(self, document_id: int, url: str, file_name: str) -> None:
        self.conn.execute(
            """
            INSERT INTO attachment (document_id, url, file_name) VALUES (%s, %s, %s)
            ON CONFLICT (document_id, url) DO NOTHING
            """, (document_id, url, file_name))
