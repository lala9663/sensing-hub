"""워커 생존 신호 (worker_heartbeat).

운영 지표(/api/v1/ops/overview)는 최근 2분 안에 신호를 남긴 워커를 "살아 있는 워커"로 센다. 작업 기록(crawl_job.locked_by)으로
세면 큐가 빈 동안에는 쉬고 있는 워커가 0대로 보여 "워커가 죽었다"로 오해하기 때문이다.

- 시작할 때 한 번 등록하고(started_at을 지금으로), 그 뒤 interval초마다 last_seen_at을 갱신한다 (별도 스레드·별도 연결).
- 작업을 잡거나 끝내면 set_job으로 바로 갱신해 화면의 "현재 작업"이 30초 늦지 않게 한다.
- DB 오류는 경고만 남긴다. 생존 신호가 실패했다고 수집을 멈추지 않는다 (다음 주기에 다시 연결한다).
"""

from __future__ import annotations

import logging
import socket
import threading

import psycopg

from . import __version__

log = logging.getLogger(__name__)

# 이보다 오래 신호가 없는 행은 시작할 때 지운다 (워커 아이디가 호스트-pid라 재시작마다 행이 늘어난다)
PRUNE_DAYS = 7
CONNECT_TIMEOUT_SECONDS = 5


class WorkerHeartbeat:
    def __init__(self, dsn: str, worker_id: str, interval_seconds: float = 30.0, host: str | None = None,
                 version: str = __version__):
        self.dsn = dsn
        self.worker_id = worker_id
        self.interval = max(1.0, interval_seconds)
        self.host = host or socket.gethostname()
        self.version = version
        self.current_job_id: int | None = None
        self._conn: psycopg.Connection | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ 수명
    def start(self) -> None:
        """등록하고 주기 갱신 스레드를 띄운다."""
        self._run(self._register, prune=True)
        self._thread = threading.Thread(target=self._loop, name="worker-heartbeat", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self.current_job_id = None
        self._run(self._touch)
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def set_job(self, job_id: int | None) -> None:
        """처리 중인 작업을 바꾸고 바로 신호를 남긴다 (None = 쉬는 중)."""
        self.current_job_id = job_id
        self._run(self._touch)

    def beat(self) -> None:
        self._run(self._touch)

    # ------------------------------------------------------------------ 내부
    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            self.beat()

    def _run(self, action, **kwargs) -> None:
        with self._lock:
            try:
                if self._conn is None or self._conn.closed:
                    # set_job은 작업 루프에서 부르므로 DB가 닿지 않을 때 오래 막히지 않게 연결 제한 시간을 짧게 둔다
                    self._conn = psycopg.connect(self.dsn, autocommit=True, connect_timeout=CONNECT_TIMEOUT_SECONDS)
                action(self._conn, **kwargs)
            except psycopg.Error as e:
                log.warning("worker heartbeat failed worker=%s: %s", self.worker_id, e)
                if self._conn is not None:
                    self._conn.close()
                self._conn = None

    def _register(self, conn: psycopg.Connection, prune: bool = False) -> None:
        conn.execute("""
            INSERT INTO worker_heartbeat (worker_id, host, started_at, last_seen_at, version, current_job_id)
            VALUES (%s, %s, now(), now(), %s, %s)
            ON CONFLICT (worker_id) DO UPDATE SET host = EXCLUDED.host, started_at = now(), last_seen_at = now(),
                version = EXCLUDED.version, current_job_id = EXCLUDED.current_job_id
            """, (self.worker_id, self.host[:255], self.version[:40], self.current_job_id))
        if prune:
            conn.execute("DELETE FROM worker_heartbeat WHERE last_seen_at < now() - make_interval(days => %s)",
                         (PRUNE_DAYS,))

    def _touch(self, conn: psycopg.Connection) -> None:
        cur = conn.execute("UPDATE worker_heartbeat SET last_seen_at = now(), current_job_id = %s WHERE worker_id = %s",
                           (self.current_job_id, self.worker_id))
        if cur.rowcount == 0:   # 다른 워커의 정리로 행이 지워졌다: 다시 등록한다
            self._register(conn)
