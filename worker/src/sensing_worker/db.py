"""작업 큐와 저장. Java 백엔드와 같은 PostgreSQL 테이블을 쓴다."""

from __future__ import annotations

import hashlib
import json
import logging
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

    def activate_source(self, source_id: int, method: str, interval_minutes: int) -> None:
        """설정 버전은 그대로 두고 소스만 ACTIVE로 (키워드 감시: 백엔드가 만든 설정을 쓴다)."""
        self.conn.execute(
            """
            UPDATE source SET method = %s, interval_minutes = %s, status = 'ACTIVE', status_reason = NULL,
                   consecutive_anomalies = 0, updated_at = now()
            WHERE id = %s
            """, (method, interval_minutes, source_id))

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

    def add_watch_hit(self, document_id: int, source_id: int, keyword: str | None) -> None:
        """키워드 감시 기록. 같은 문서·감시 쌍은 처음 걸린 검색어만 남긴다."""
        self.conn.execute(
            """
            INSERT INTO document_watch (document_id, source_id, keyword) VALUES (%s, %s, %s)
            ON CONFLICT (document_id, source_id) DO NOTHING
            """, (document_id, source_id, (keyword or "")[:200] or None))

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
