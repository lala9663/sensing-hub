"""도메인 단위 요청 간격.

- LocalThrottle: 프로세스 안에서만 간격을 지킨다 (테스트·단독 실행용).
- DbThrottle: domain_throttle 테이블로 워커 여러 대가 같은 간격을 공유한다.
  요청마다 "다음에 요청해도 되는 시각"을 원자적으로 예약하고, 잠금 없이 그 시각까지 기다린다.
  예약 순서대로 슬롯이 배정되므로 워커가 늘어도 도메인당 간격은 지켜진다.
"""

from __future__ import annotations

import threading
import time

import psycopg


def throttle_key(host: str) -> str:
    """www.example.com과 example.com은 같은 서버로 보고 간격을 함께 센다."""
    host = (host or "").lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


class LocalThrottle:
    def __init__(self, interval_seconds: float):
        self.interval = interval_seconds
        self._next: dict[str, float] = {}
        self._lock = threading.Lock()

    def reserve(self, host: str) -> float:
        """슬롯을 예약하고 기다려야 할 초를 돌려준다."""
        key = throttle_key(host)
        with self._lock:
            now = time.monotonic()
            slot = max(self._next.get(key, 0.0), now)
            self._next[key] = slot + self.interval
            return slot - now

    def wait(self, host: str) -> None:
        delay = self.reserve(host)
        if delay > 0:
            time.sleep(delay)

    def close(self) -> None:
        pass


class DbThrottle:
    _SQL = """
        INSERT INTO domain_throttle AS t (host, next_allowed_at)
        VALUES (%(h)s, clock_timestamp() + make_interval(secs => %(i)s::float8))
        ON CONFLICT (host) DO UPDATE
            SET next_allowed_at = GREATEST(t.next_allowed_at, clock_timestamp()) + make_interval(secs => %(i)s::float8)
        RETURNING GREATEST(0, EXTRACT(EPOCH FROM (t.next_allowed_at - clock_timestamp())) - %(i)s::float8)::float8 AS wait
    """

    def __init__(self, dsn: str, interval_seconds: float):
        self.interval = interval_seconds
        self.conn = psycopg.connect(dsn, autocommit=True)
        self._lock = threading.Lock()

    def reserve(self, host: str) -> float:
        with self._lock:
            row = self.conn.execute(self._SQL, {"h": throttle_key(host), "i": self.interval}).fetchone()
        return float(row[0])

    def wait(self, host: str) -> None:
        delay = self.reserve(host)
        if delay > 0:
            time.sleep(delay)

    def close(self) -> None:
        self.conn.close()
