"""워커 생존 신호(worker_heartbeat) DB 테스트. SENSING_TEST_DSN이 있을 때만 실행한다 (test_flow_db.py와 같은 방식)."""

from __future__ import annotations

import os
import pathlib
import time

import psycopg
import pytest
from psycopg.rows import dict_row

from fakesite import Site, news_site_routes
from sensing_worker.config import Settings
from sensing_worker.db import Store
from sensing_worker.fetcher import PoliteFetcher
from sensing_worker.heartbeat import WorkerHeartbeat
from sensing_worker.jobs import JobRunner
from sensing_worker.main import process_one

DSN = os.getenv("SENSING_TEST_DSN")
MIGRATIONS = pathlib.Path(__file__).resolve().parents[2] / "backend/src/main/resources/db/migration"
pytestmark = pytest.mark.skipif(not DSN, reason="SENSING_TEST_DSN not set")


@pytest.fixture()
def conn():
    with psycopg.connect(DSN, autocommit=True, row_factory=dict_row) as c:
        c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        for path in sorted(MIGRATIONS.glob("V*__*.sql"), key=lambda p: int(p.name[1:].split("__")[0])):
            c.execute(path.read_text(encoding="utf-8"))
        yield c


def row(c, worker_id: str):
    return c.execute("SELECT * FROM worker_heartbeat WHERE worker_id = %s", (worker_id,)).fetchone()


def test_heartbeat_registers_and_keeps_last_seen_fresh(conn):
    # 오래된 다른 워커 행: 시작할 때 정리된다 (7일 넘게 신호 없음)
    conn.execute("INSERT INTO worker_heartbeat (worker_id, last_seen_at) VALUES ('gone-1', now() - interval '8 days'), "
                 "('idle-2', now() - interval '1 hour')")
    hb = WorkerHeartbeat(DSN, "w-1", interval_seconds=1, host="host-a", version="9.9.9")
    hb.start()
    try:
        first = row(conn, "w-1")
        assert first["host"] == "host-a" and first["version"] == "9.9.9" and first["current_job_id"] is None
        assert row(conn, "gone-1") is None and row(conn, "idle-2") is not None
        time.sleep(1.6)   # 주기 갱신 한 번 이상
        assert row(conn, "w-1")["last_seen_at"] > first["last_seen_at"]
        assert row(conn, "w-1")["started_at"] == first["started_at"]   # 갱신은 시작 시각을 바꾸지 않는다
    finally:
        hb.stop()


def test_restart_with_same_id_resets_started_at_and_survives_pruned_row(conn):
    conn.execute("INSERT INTO worker_heartbeat (worker_id, started_at, last_seen_at) "
                 "VALUES ('w-1', now() - interval '3 days', now() - interval '3 days')")
    hb = WorkerHeartbeat(DSN, "w-1", interval_seconds=60)
    hb.start()
    try:
        fresh = conn.execute("SELECT started_at > now() - interval '1 minute' AS ok FROM worker_heartbeat "
                             "WHERE worker_id = 'w-1'").fetchone()
        assert fresh["ok"]   # 같은 아이디로 다시 시작하면 시작 시각이 지금으로 바뀐다
        conn.execute("DELETE FROM worker_heartbeat")   # 다른 워커가 지웠다
        hb.beat()
        assert row(conn, "w-1") is not None
    finally:
        hb.stop()


def test_db_errors_do_not_raise(conn):
    missing_db = DSN.rsplit("/", 1)[0] + "/no_such_database_for_heartbeat"
    hb = WorkerHeartbeat(missing_db, "w-x", interval_seconds=60)
    hb.start()   # 연결 실패는 경고만 남긴다
    hb.set_job(None)
    hb.stop()
    conn.execute("DROP TABLE worker_heartbeat")   # 테이블이 없는 DB(마이그레이션 전)도 마찬가지
    hb = WorkerHeartbeat(DSN, "w-y", interval_seconds=60)
    hb.start()
    hb.beat()
    hb.stop()


def test_current_job_is_recorded_while_processing(conn, tmp_path):
    site = Site(news_site_routes())
    try:
        settings = Settings(db_url=DSN, min_interval_seconds=0, max_retries=1, allow_private_hosts=True,
                            worker_id="w-job", evidence_dir=str(tmp_path / "evidence"))
        store = Store(DSN)
        fetcher = PoliteFetcher(settings)
        domain = site.base.split("://", 1)[1].split(":")[0]
        site_id = conn.execute("INSERT INTO site (domain, base_url) VALUES (%s, %s) RETURNING id",
                               (domain, site.base)).fetchone()["id"]
        source_id = conn.execute("INSERT INTO source (site_id, name, requested_by) VALUES (%s, %s, 2) RETURNING id",
                                 (site_id, domain)).fetchone()["id"]
        job_id = conn.execute("INSERT INTO crawl_job (source_id, job_type, trigger) VALUES (%s, 'ONBOARD', 'MANUAL') "
                              "RETURNING id", (source_id,)).fetchone()["id"]

        hb = WorkerHeartbeat(DSN, "w-job", interval_seconds=60)
        hb.start()
        seen: list[int | None] = []

        class Spy(JobRunner):
            def run(self, job):
                seen.append(row(conn, "w-job")["current_job_id"])   # 작업 중에 화면이 보는 값
                return super().run(job)

        try:
            process_one(store, Spy(store, fetcher, settings), settings, hb)
            assert seen == [job_id]
            assert row(conn, "w-job")["current_job_id"] is None   # 끝나면 쉬는 중
        finally:
            hb.stop()
            fetcher.close()
            store.close()
    finally:
        site.close()
